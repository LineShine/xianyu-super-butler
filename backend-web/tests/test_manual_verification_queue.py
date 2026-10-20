import asyncio
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from common.models.user import UserRole
from common.models.ytx_account import YtxAccount
from common.services import manual_verification_queue as queue
from common.services.im_token_api import ImTokenApiResult, request_im_token_with_fallback
from app.services.manual_captcha_service import ManualCaptchaManager, ManualSession

URL = "https://h5api.m.goofish.com/h5/test/punish?x5secdata=test"


def account(aid, owner=1):
    return SimpleNamespace(id=int(aid), account_id=aid, owner_id=owner, status="active",
        cookie=f"unb={aid}", metadata_json={}, remark=aid, display_name=aid, disable_reason=None)


class MemoryDatabase:
    """Database I/O double; queue transitions still run the production functions."""
    def __init__(self, accounts):
        self.accounts, self.logs = accounts, {}
        self.transaction = asyncio.Lock()

    @asynccontextmanager
    async def session(self):
        async with self.transaction:
            yield self

    async def execute(self, statement):
        params = statement.compile().params
        accounts = self.accounts
        if "account_id_1" in params:
            accounts = [a for a in accounts if a.account_id == params["account_id_1"]]
        else:
            accounts = [a for a in accounts if queue.state_of(a).get("status") in queue.PENDING]
        result = Mock()
        result.scalar_one_or_none.return_value = accounts[0] if accounts else None
        result.scalars.return_value.all.return_value = accounts
        return result

    def add(self, log):
        log.id = len(self.logs) + 1
        self.logs[log.id] = log

    async def get(self, model, log_id):
        return self.logs.get(log_id)

    async def flush(self): pass
    async def commit(self): pass


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.a, self.b, self.other = account("11"), account("22", 2), account("33")
        self.db = MemoryDatabase([self.a, self.b, self.other])
        self.user = SimpleNamespace(id=1, role=UserRole.MEMBER)
        self.admin = SimpleNamespace(id=1, role=UserRole.ADMIN)
        self.factory = patch.object(queue, "async_session_maker", self.db.session)
        self.lock = patch.object(queue, "_lock", AsyncMock())
        self.factory.start(); self.lock.start()
        self.addCleanup(self.factory.stop); self.addCleanup(self.lock.stop)

    async def add(self, account):
        return await queue.enqueue(account.account_id, url=URL, device_id="device",
                                   expected_cookie=account.cookie)

    async def test_duplicate_concurrent_triggers_have_one_queue_item(self):
        ids = await asyncio.gather(*(self.add(self.a) for _ in range(5)))
        self.assertEqual(ids, [1] * 5)
        self.assertEqual(len(self.db.logs), 1)
        self.assertEqual(self.a.status, "disabled")
        self.assertEqual(self.other.status, "active")
        self.assertEqual(self.other.metadata_json, {})

    async def test_fifo_starts_sixty_seconds_only_for_current_head(self):
        await self.add(self.a); await self.add(self.b)
        with patch.object(queue.time, "time", return_value=100):
            result = await queue.advance(user=self.admin)
        self.assertEqual(result["account_id"], "11")
        self.assertEqual(queue.state_of(self.a)["expires_at"], 160)
        self.assertEqual(queue.state_of(self.b)["expires_at"], 0)
        self.assertEqual(queue.state_of(self.b)["status"], "queued")

    async def test_timeout_without_browser_releases_head_and_preserves_account_pause(self):
        await self.add(self.a); await self.add(self.b)
        with patch.object(queue.time, "time", return_value=100): await queue.advance()
        with patch.object(queue.time, "time", return_value=160):
            result = await queue.advance(user=self.admin)
        self.assertEqual(result["account_id"], "22")
        self.assertEqual(result["expires_at"], 220)
        self.assertEqual(self.db.logs[1].processing_status, "cancelled")
        self.assertEqual(self.a.status, "disabled")
        self.assertTrue(self.a.metadata_json[queue.HOLD_KEY])
        self.assertNotIn("url", queue.state_of(self.a))
        self.assertNotIn("device_id", queue.state_of(self.a))

    async def test_two_tabs_cannot_claim_the_same_challenge(self):
        await self.add(self.a)
        first = await queue.advance(user=self.user, client_id="tab-one", claim_request=True, worker_id="w1")
        second = await queue.advance(user=self.user, client_id="tab-two", claim_request=True, worker_id="w1")
        self.assertIn("claim", first)
        self.assertNotIn("claim", second)
        self.assertFalse(second["available"])
        resumed = await queue.advance(user=self.user, client_id="tab-one", claim_request=True, worker_id="w1")
        self.assertEqual(first["claim"]["id"], resumed["claim"]["id"])
        other_worker = await queue.advance(user=self.user, client_id="tab-one", claim_request=True, worker_id="w2")
        self.assertNotIn("claim", other_worker)

    async def test_member_never_sees_or_claims_another_owners_head(self):
        await self.add(self.b); await self.add(self.a)
        result = await queue.advance(user=self.user, client_id="tab-one", claim_request=True)
        self.assertFalse(result["available"])
        self.assertEqual(result["pending_count"], 1)
        self.assertNotIn("account_id", result)
        self.assertNotIn("claim", result)

    async def test_explicit_start_checks_owner_and_status(self):
        self.b.status = "disabled"
        with self.assertRaises(HTTPException) as error:
            await queue.enqueue("22", user=self.user)
        self.assertEqual(error.exception.status_code, 404)
        with self.assertRaises(HTTPException): await queue.enqueue("11", user=self.user)
        self.assertEqual(len(self.db.logs), 0)

    async def test_cookie_change_cancels_old_request_and_does_not_overwrite_login(self):
        await self.add(self.a); await self.add(self.b)
        self.a.cookie = "unb=11; new_login=true"
        result = await queue.advance(user=self.admin)
        self.assertEqual(result["account_id"], "22")
        self.assertEqual(queue.state_of(self.a)["status"], "cancelled")
        self.assertEqual(self.a.cookie, "unb=11; new_login=true")

    async def test_only_official_challenge_is_enqueued_and_late_response_cannot_restart(self):
        for url in ["https://evil.test/punish", "http://taobao.com/punish", "https://taobao.com.evil.test/punish"]:
            self.assertIsNone(await queue.enqueue("11", url=url))
        self.assertIsNone(await queue.enqueue("11", url=URL, expected_cookie="old"))
        await self.add(self.a)
        sid = queue.state_of(self.a)["id"]
        await queue.finish("11", sid, "cancelled", "timeout")
        self.assertIsNone(await self.add(self.a))
        self.assertEqual(len(self.db.logs), 1)

    async def test_close_only_releases_its_own_session(self):
        await self.add(self.a); await self.add(self.b)
        await queue.advance(user=self.admin)
        sid = queue.state_of(self.a)["id"]
        await queue.finish("11", "wrong-id", "cancelled", "close")
        self.assertEqual(queue.state_of(self.a)["status"], "processing")
        await queue.finish("11", sid, "success", "passed")
        result = await queue.advance(user=self.admin)
        self.assertEqual(result["account_id"], "22")
        self.assertEqual(self.a.status, "disabled")
        self.assertEqual(queue.state_of(self.a)["status"], "success")


class ClaimTests(unittest.IsolatedAsyncioTestCase):
    def claim_data(self):
        return {"pending_count": 1, "available": True, "account_id": "11", "claim": {
            "id": "queue-session", "owner_id": 1, "account_pk": 11, "account_id": "11",
            "cookie": "unb=11", "device_id": "d", "log_id": 1, "expires_at": time.time() + 60,
            "url": URL, "requested_at": time.time(),
        }}

    async def test_claim_never_exposes_challenge_or_cookie_and_reuses_browser(self):
        manager = ManualCaptchaManager()
        manager._open, manager._expire = AsyncMock(), AsyncMock()
        user = SimpleNamespace(id=1, role=UserRole.MEMBER)
        db = AsyncMock()
        db.get.return_value = SimpleNamespace(status="disabled", owner_id=1, cookie="unb=11", proxy_type=None)
        with (patch.object(queue, "advance", AsyncMock(side_effect=lambda **kw: self.claim_data())),
              patch("app.services.manual_captcha_service.async_session_maker") as factory):
            factory.return_value.__aenter__.return_value = db
            result = await manager.claim(user, "tab-one")
            again = await manager.claim(user, "tab-one")
            await asyncio.sleep(0)
        self.assertNotIn("claim", result)
        self.assertNotIn("cookie", result["session"])
        self.assertNotIn("url", result["session"])
        self.assertEqual(result["session"]["session_id"], again["session"]["session_id"])
        manager._open.assert_awaited_once()

    async def test_cookie_mismatch_releases_queue_without_opening_browser(self):
        manager = ManualCaptchaManager()
        manager._open = AsyncMock()
        db = AsyncMock()
        db.get.return_value = SimpleNamespace(status="disabled", owner_id=1, cookie="unb=other")
        with (patch.object(queue, "advance", AsyncMock(return_value=self.claim_data())),
              patch.object(queue, "finish", AsyncMock()) as finish,
              patch("app.services.manual_captcha_service.async_session_maker") as factory):
            factory.return_value.__aenter__.return_value = db
            result = await manager.claim(SimpleNamespace(id=1, role=UserRole.MEMBER), "tab-one")
        self.assertFalse(result["available"])
        self.assertNotIn("session", result)
        manager._open.assert_not_awaited()
        finish.assert_awaited_once()


class TokenHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_challenge_handoff_merges_response_once_without_solver_retry(self):
        result = ImTokenApiResult(response_json={"data": {"url": URL}},
            response_cookies={"_m_h5_tk": "fresh"}, status_code=200, duration_seconds=.1)
        with (patch.object(queue, "held", AsyncMock(return_value=False)),
              patch.object(queue, "enqueue", AsyncMock(return_value=1)) as enqueue,
              patch("common.services.slider_retry_guard.account_slider_paused", AsyncMock(return_value=False)),
              patch("common.services.im_token_api.request_im_token", AsyncMock(return_value=result)) as probe):
            response = await request_im_token_with_fallback("unb=11", "device")
        self.assertTrue(response.manual_verification_pending)
        self.assertEqual(response.response_cookies, {})
        enqueue.assert_awaited_once_with("11", url=URL, device_id="device", expected_cookie="unb=11", response_cookies={"_m_h5_tk": "fresh"})
        probe.assert_awaited_once()

    async def test_account_waiting_or_timed_out_makes_no_further_official_request(self):
        with (patch.object(queue, "held", AsyncMock(return_value=True)),
              patch("common.services.im_token_api.request_im_token", AsyncMock()) as probe,
              patch("common.services.im_token_api._request_remote_token_from_settings", AsyncMock()) as remote):
            response = await request_im_token_with_fallback("unb=11", "device", api_mode="remote")
        self.assertTrue(response.manual_verification_pending)
        probe.assert_not_awaited(); remote.assert_not_awaited()

    async def test_success_of_other_account_is_unchanged(self):
        result = ImTokenApiResult(response_json={"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "ok"}}, response_cookies={}, status_code=200, duration_seconds=.1)
        with (patch.object(queue, "held", AsyncMock(return_value=False)),
              patch.object(queue, "enqueue", AsyncMock()) as enqueue,
              patch("common.services.slider_retry_guard.account_slider_paused", AsyncMock(return_value=False)),
              patch("common.services.im_token_api.request_im_token", AsyncMock(return_value=result))):
            response = await request_im_token_with_fallback("unb=22", "device")
        self.assertIs(response, result)
        enqueue.assert_not_awaited()

    async def test_deadline_cancels_inflight_check_and_never_saves_late_success(self):
        manager = ManualCaptchaManager()
        active = ManualSession(id="s", operator_id=1, owner_id=1, account_pk=1,
            account_id="11", original_cookie="unb=11", device_id="d", log_id=1,
            cookies={"unb": "11"}, status="ready", interactions=1,
            expires_at=time.time() + .02, context=SimpleNamespace(cookies=AsyncMock(return_value=[])))
        async def slow_probe(session):
            await asyncio.sleep(1)
        manager._probe = slow_probe
        manager._save_success = AsyncMock()
        with self.assertRaises(HTTPException) as error: await manager.check(active)
        self.assertEqual(error.exception.status_code, 410)
        manager._save_success.assert_not_awaited()
        self.assertEqual(active.operation_tasks, set())
