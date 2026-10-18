import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.services.scheduler import redelivery_task as module
from common.models.ytx_order import YtxOrder


class RedeliveryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        module._order_cooldown_cache.clear()
        self.task = module.RedeliveryTask()
        self.order = YtxOrder(
            order_no="order", account_id="seller", item_id="item", buyer_id="buyer",
            chat_id=None, status="pending_ship", amount=Decimal("0.01"), quantity=1,
            spec_name="type", spec_value="standard", card_only_delivered=False,
        )
        self.session = AsyncMock()
        result = Mock()
        result.scalars.return_value.first.return_value = self.order
        result.scalar_one_or_none.return_value = 1
        self.session.execute.return_value = result
        self.http = SimpleNamespace(post=AsyncMock())
        self.patches = [
            patch.object(module, "get_http_client", return_value=self.http),
            patch.object(module, "get_settings", return_value=SimpleNamespace(websocket_service_url="http://websocket")),
            patch("common.services.order_service.OrderService"),
            patch("common.services.order_service.check_can_ship", new_callable=AsyncMock),
            patch("common.db.redis_client.try_acquire_delivery_lock", new_callable=AsyncMock),
        ]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)
        self.order_svc = self.mocks[2].return_value
        self.order_svc.update_order_chat_id = AsyncMock(return_value=True)
        self.order_svc.update_order_delivery_fail_reason = AsyncMock(return_value=True)
        self.mocks[3].return_value = {"success": True, "can_ship": True, "reason": "订单已付款"}
        self.mocks[4].return_value = SimpleNamespace(success=False, is_locked_by_other=False, has_error=False)

    async def test_chat_failure_does_not_write_poison_placeholder(self):
        self.http.post.return_value = {"success": False, "message": "创建会话失败（400）"}
        ok, error = await self.task._ensure_chat_id(self.session, self.order)
        self.assertFalse(ok)
        self.assertIsNone(self.order.chat_id)
        self.order_svc.update_order_chat_id.assert_not_called()
        self.assertTrue(module.should_add_to_cooldown(error))

    async def test_invalid_chat_response_does_not_get_persisted(self):
        for chat_id in (None, "", "FAILED_buyer"):
            self.http.post.return_value = {"success": True, "data": {"chat_id": chat_id}}
            ok, error = await self.task._ensure_chat_id(self.session, self.order)
            self.assertFalse(ok)
            self.assertTrue(module.should_add_to_cooldown(error))
        self.order_svc.update_order_chat_id.assert_not_called()

    async def test_legacy_failed_marker_recovers_and_delivers(self):
        self.order.chat_id = "FAILED_buyer"
        self.task._get_matching_card = AsyncMock(return_value=SimpleNamespace(id=1))
        self.http.post.side_effect = [
            {"success": True, "data": {"chat_id": "real-chat"}},
            {"success": True, "data": {"content": "test-card"}},
        ]
        ok, error, _ = await self.task._process_order(
            self.session, SimpleNamespace(cookie="cookie"), self.order,
        )
        self.assertTrue(ok, error)
        self.assertEqual(self.order.chat_id, "real-chat")
        self.assertEqual(self.order.status, "shipped")
        self.order_svc.update_order_chat_id.assert_awaited_once_with("order", "real-chat")
        self.assertEqual(self.http.post.await_args_list[-1].kwargs["json"]["chat_id"], "real-chat")

    async def test_missing_specs_use_order_detail_service(self):
        self.order.spec_name = self.order.spec_value = None
        self.order.chat_id = "real-chat"
        self.task._get_matching_card = AsyncMock(return_value=None)
        with patch("common.services.order_service.OrderDetailService") as cls:
            detail = cls.return_value
            detail.cookies_str = "cookie"
            detail._fetch_order_detail = AsyncMock(return_value={"amount": "4.99", "spec_name": "type", "spec_value": "standard"})
            await self.task._process_order(self.session, SimpleNamespace(cookie="cookie"), self.order)
            detail._fetch_order_detail.assert_awaited_once_with("order")
            self.assertEqual(self.order.spec_name, "type")
            self.assertEqual(self.order.amount, Decimal("0.01"))

    async def test_shipped_or_card_only_order_is_not_sent_twice(self):
        for status, card_sent in (("shipped", False), ("pending_ship", True)):
            self.order.status, self.order.card_only_delivered = status, card_sent
            ok, _, _ = await self.task._process_order(self.session, SimpleNamespace(cookie="cookie"), self.order)
            self.assertTrue(ok)
        self.http.post.assert_not_called()
        self.mocks[3].assert_not_called()

    async def test_failed_chat_creation_is_cooled_then_retryable(self):
        self.task._get_pending_orders = AsyncMock(return_value=[self.order])
        self.task._process_order = AsyncMock(return_value=(False, "创建会话失败（400）", "cookie"))
        self.task._log_result = AsyncMock()
        account = SimpleNamespace(account_id="seller", cookie="cookie")
        with patch("common.utils.cookie_refresh.is_account_session_cooled", return_value=False), patch.object(module.asyncio, "sleep", new_callable=AsyncMock):
            await self.task._process_account(self.session, "batch", account)
            self.assertTrue(module.is_order_in_cooldown("order"))
            await self.task._process_account(self.session, "batch", account)
            self.assertEqual(self.task._process_order.await_count, 1)
            module._order_cooldown_cache["order"] = module.datetime.now() - module.timedelta(seconds=1)
            await self.task._process_account(self.session, "batch", account)
            self.assertEqual(self.task._process_order.await_count, 2)
