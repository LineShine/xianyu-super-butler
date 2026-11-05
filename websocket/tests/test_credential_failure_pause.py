"""Unreadable secrets are distinct from a genuinely unconfigured account."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.xianyu.cookie_token_manager import CookieTokenManager
from common.security.vault import CredentialUnavailable


class CredentialFailureTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, account_info=None, error=None):
        manager = CookieTokenManager(SimpleNamespace(cookie_id="fixture", cookies_str="unb=fixture", _safe_str=str))
        renewal = SimpleNamespace(deferred=False, updated_cookie_names=[], success=False, api_message="fixture expiry")
        with patch("common.services.cookie_renew_api_service.cookie_renew_api_service.renew", AsyncMock(return_value=renewal)), \
             patch("common.services.account_credential_guard.verification_pending", AsyncMock(return_value=False)), \
             patch.object(manager, "_load_account_info", AsyncMock(return_value=account_info, side_effect=error)), \
             patch.object(manager, "send_token_refresh_notification", AsyncMock()), \
             patch("common.db.compat.db_manager.add_account_login_log") as log, \
             patch("common.db.compat.db_manager.disable_account") as disable:
            result = await manager._try_password_login_refresh("fixture expiry")
            disable.assert_not_called()
            return result, log.call_args.kwargs

    async def test_decryption_failure_pauses_without_no_credentials_result(self):
        result, log = await self.invoke(error=CredentialUnavailable("fixture key failure"))
        self.assertEqual(result, "skipped_cooldown")
        self.assertEqual(log["failure_reason"], "credential_unavailable")

    async def test_truly_missing_password_preserves_original_disable_signal(self):
        result, log = await self.invoke(account_info={"username": "fixture", "password": "", "show_browser": False})
        self.assertEqual(result, "no_credentials")
        self.assertEqual(log["failure_reason"], "no_credentials")
