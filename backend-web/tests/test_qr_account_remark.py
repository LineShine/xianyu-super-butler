"""QR login uses the login username for empty remarks, without overwriting edits."""
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

from app.services.account_service import AccountService, _qr_login_nickname
from common.models.ytx_account import YtxAccount


class QRAccountRemarkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = AsyncMock()
        self.session.add = Mock()
        self.service = AccountService(self.session)
        self.service.get_account_by_unb = AsyncMock(return_value=None)
        self.service._generate_unique_account_id = AsyncMock(return_value="123")
        self.limit = patch(
            "app.services.account_service.AccountLimitService.ensure_can_add_account",
            new_callable=AsyncMock,
        )
        self.limit.start()
        self.addCleanup(self.limit.stop)
        self.reset = patch(
            "common.services.slider_retry_guard.reset_slider_guard",
            new_callable=AsyncMock,
        )
        self.reset.start()
        self.addCleanup(self.reset.stop)

    async def scan(self, cookies, *, method="qr_scan"):
        return await self.service.upsert_account_from_qr(
            owner_id=1, cookies=cookies, unb="123", login_method=method,
        )

    async def test_first_scan_stores_decoded_username_in_remark(self):
        nickname = "小鱼 + A&B"
        account, created = await self.scan(f"unb=123;tracknick={quote(nickname)}")
        self.assertTrue(created)
        self.assertEqual(account.remark, nickname)
        self.assertEqual(account.display_name, nickname)
        self.assertIsNone(account.username)  # Never confuse nickname with login credentials.
        self.session.commit.assert_awaited_once()

    async def test_shared_scan_also_fills_remark(self):
        account, created = await self.scan("unb=123; tracknick=Seller", method="shared_qr_scan")
        self.assertTrue(created)
        self.assertEqual(account.remark, "Seller")
        self.assertEqual(account.login_method, "shared_qr_scan")

    async def test_rescan_preserves_custom_remark_and_resolved_nickname(self):
        existing = YtxAccount(
            account_id="123", remark="客服一号", display_name="闲鱼昵称", metadata_json={},
        )
        self.service.get_account_by_unb.return_value = existing
        account, created = await self.scan("unb=123; tracknick=LoginName")
        self.assertFalse(created)
        self.assertEqual(account.remark, "客服一号")
        self.assertEqual(account.display_name, "闲鱼昵称")

    async def test_rescan_fills_blank_remark_from_known_profile(self):
        existing = YtxAccount(
            account_id="123", remark="  ", display_name="闲鱼昵称", metadata_json={},
        )
        self.service.get_account_by_unb.return_value = existing
        account, _ = await self.scan("unb=123; tracknick=LoginName")
        self.assertEqual(account.remark, "闲鱼昵称")

    async def test_rescan_can_fill_previously_missing_name(self):
        existing = YtxAccount(account_id="123", metadata_json={})
        self.service.get_account_by_unb.return_value = existing
        account, _ = await self.scan("unb=123; tracknick=Seller")
        self.assertEqual(account.remark, "Seller")
        self.assertEqual(account.display_name, "Seller")

    async def test_missing_username_does_not_block_login_or_use_id(self):
        account, created = await self.scan("unb=123; cookie=test")
        self.assertTrue(created)
        self.assertIsNone(account.remark)
        self.assertIsNone(account.display_name)
        self.session.commit.assert_awaited_once()

    def test_invalid_names_are_not_saved(self):
        for value in ("", "123", "null", "undefined", "%20", "%FF", "a%0Ab"):
            with self.subTest(value=value):
                self.assertIsNone(_qr_login_nickname(f"tracknick={value}", "123"))
        self.assertIsNone(_qr_login_nickname("", "123"))
