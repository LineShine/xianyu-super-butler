import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.services.manual_captcha_service import ManualCaptchaManager, ManualSession, verification_outcome
from common.services.im_token_api import IM_TOKEN_USER_AGENT, request_im_token
from common.services.account_cookie_service import merge_account_cookie_fields
from common.utils.cookie_refresh import update_account_cookies_in_db


class VerificationDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_probe_shares_jar_and_disposes_response_without_http_fallback(self):
        response = SimpleNamespace(
            status=200, json=AsyncMock(return_value={"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "test"}}),
            headers_array=[{"name": "Set-Cookie", "value": "_m_h5_tk=new; HttpOnly; Path=/"}],
            dispose=AsyncMock(),
        )
        context = SimpleNamespace(request=SimpleNamespace(post=AsyncMock(return_value=response)))
        with patch("common.services.im_token_api.aiohttp.ClientSession") as fallback:
            result = await request_im_token("unb=11; _m_h5_tk=old", "device", browser_context=context)
        fallback.assert_not_called()
        args = context.request.post.await_args.kwargs
        self.assertNotIn("cookie", args["headers"])
        self.assertEqual(args["headers"]["user-agent"], IM_TOKEN_USER_AGENT)
        self.assertEqual((args["max_redirects"], args["max_retries"]), (0, 0))
        self.assertEqual(result.response_cookies, {"_m_h5_tk": "new"})
        response.dispose.assert_awaited_once()

    async def test_non_json_rate_limit_preserves_http_failure_category(self):
        response = SimpleNamespace(status=429, json=AsyncMock(side_effect=ValueError("HTML")),
                                   headers_array=[], dispose=AsyncMock())
        context = SimpleNamespace(request=SimpleNamespace(post=AsyncMock(return_value=response)))
        result = await request_im_token("unb=11", "device", browser_context=context)
        self.assertEqual(verification_outcome(result)[0], "rate_limited")
        self.assertEqual(result.response_json, {})
        response.dispose.assert_awaited_once()

    async def test_diagnostics_record_page_hint_and_codes_without_credentials(self):
        manager = ManualCaptchaManager()
        active = ManualSession(id="s", operator_id=1, owner_id=1, account_pk=1,
                               account_id="11", original_cookie="secret", device_id="private", log_id=2, cookies={})
        active.check_count = 1
        manager._page_hint = AsyncMock(return_value="page_reports_success")
        log = SimpleNamespace(account_identifier="11", processing_status="processing", error_message=None)
        db = AsyncMock()
        db.get.return_value = log
        result = SimpleNamespace(response_json={"ret": ["FAIL_SYS_USER_VALIDATE::https://secret?token=private"],
                                               "data": {"url": "https://secret?cookie=private"}},
                                 status_code=200, duration_seconds=0.2)
        with patch("app.services.manual_captcha_service.async_session_maker") as factory:
            factory.return_value.__aenter__.return_value = db
            await manager._record_check(active, result)
        self.assertIn("FAIL_SYS_USER_VALIDATE", log.error_message)
        self.assertIn("page_reports_success", log.error_message)
        self.assertNotIn("secret", log.error_message)
        self.assertNotIn("private", log.error_message)
        self.assertNotIn("https", log.error_message)

    async def test_background_cookie_writers_do_not_overwrite_pending_verification(self):
        account = SimpleNamespace(cookie="unb=11; verified=old", metadata_json={"manual_verification_hold": True})
        db = AsyncMock()
        row = Mock()
        row.scalars.return_value.first.return_value = account
        db.execute.return_value = row
        with patch("common.services.account_cookie_service.async_session_maker") as factory:
            factory.return_value.__aenter__.return_value = db
            self.assertIsNone(await merge_account_cookie_fields(1, "11", {"verified": "stale"}))
        with patch("common.utils.cookie_refresh.get_account_by_identity", AsyncMock(return_value=account)):
            self.assertFalse(await update_account_cookies_in_db("11", "unb=11; stale=true", session=db))
        self.assertEqual(account.cookie, "unb=11; verified=old")
        db.commit.assert_not_awaited()
