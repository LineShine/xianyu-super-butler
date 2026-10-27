import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.services.manual_captcha_service import ManualCaptchaManager, ManualSession, verification_outcome
from common.services.im_token_api import request_im_token
from app.services.manual_captcha_diagnostics import VerificationDiagnostics, response_codes
from app.api.routes.risk_control_logs import ManualCaptchaInput
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
            result = await request_im_token("unb=11; _m_h5_tk=old", "device", browser_context=context,
                                            browser_user_agent="native-browser-UA")
        fallback.assert_not_called()
        args = context.request.post.await_args.kwargs
        self.assertNotIn("cookie", args["headers"])
        self.assertEqual(args["headers"]["user-agent"], "native-browser-UA")
        self.assertFalse(any(key.startswith(("sec-ch-", "sec-fetch-")) for key in args["headers"]))
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
        self.assertNotIn("user-agent", context.request.post.await_args.kwargs["headers"])
        response.dispose.assert_awaited_once()

    def test_response_diagnostics_only_keep_status_fields(self):
        self.assertEqual(response_codes({
            "code": 300, "success": False, "token": "secret", "message": "Cookie=secret",
            "data": {"code": "FAIL", "sessionId": "secret", "result": {"code": 0, "token": "secret"}},
        }), ["code=300", "success=false", "code=FAIL", "code=0"])
        self.assertEqual(response_codes({"code": "L9gXis-secret", "status": "https://secret", "success": "secret"}), [])

    async def test_page_error_is_observed_without_token_check_and_survives_later_network_events(self):
        diagnostics = VerificationDiagnostics()
        frame = SimpleNamespace(locator=Mock(return_value=SimpleNamespace(inner_text=AsyncMock(
            return_value="验证失败，点击框体重试(error:L9gXis) Cookie=secret"))))
        page = SimpleNamespace(frames=[frame])
        await diagnostics.observe_page(page)
        generation = diagnostics.generation
        await diagnostics.observe_page(page)
        self.assertEqual(diagnostics.generation, generation)
        self.assertEqual(diagnostics.page_error_code, "L9gXis")
        for i in range(25):
            diagnostics.add(f"http={500 + i}")
        self.assertIn("error_code=L9gXis", diagnostics.text())
        self.assertNotIn("secret", diagnostics.text())
        frame.locator.return_value.inner_text.return_value = "验证通过"
        await diagnostics.observe_page(page)
        self.assertEqual(diagnostics.page_state, "page_reports_success")
        self.assertEqual(diagnostics.page_error_code, "")

    async def test_network_diagnostics_redact_signed_urls_payloads_and_exception_messages(self):
        diagnostics = VerificationDiagnostics()
        request = SimpleNamespace(url="https://example.aliyun.com/verify/private-session?token=private-cookie",
                                  resource_type="xhr", failure="net::ERR_FAILED https://secret")
        response = SimpleNamespace(request=request, status=200, headers={"content-type": "application/javascript"},
                                   text=AsyncMock(return_value='callback({"code":300,"token":"private-token"});'))
        diagnostics.blocked(request)
        diagnostics.request_failed(request)
        diagnostics.response(response)
        await asyncio.gather(*list(diagnostics._tasks))
        text = diagnostics.text()
        for expected in ("request_blocked", "net::ERR_FAILED", "http=200", "code=300", "host=example.aliyun.com"):
            self.assertIn(expected, text)
        for secret in ("private", "token", "https://", "secret"):
            self.assertNotIn(secret, text)
        await diagnostics.close()

    async def test_slide_submission_records_business_code_without_exposing_challenge(self):
        diagnostics = VerificationDiagnostics()
        request = SimpleNamespace(
            url="https://h5api.m.goofish.com/private-id/_____tmd_____/slide?token=private-cookie",
            resource_type="xhr",
        )
        response = SimpleNamespace(request=request, status=200, headers={"content-type": "application/json"},
                                   text=AsyncMock(return_value='{"result":{"code":300,"token":"private-token"}}'))
        diagnostics.response(response)
        await asyncio.gather(*list(diagnostics._tasks))
        detail = diagnostics.text()
        self.assertIn("response_codes endpoint=slide", detail)
        self.assertIn("http=200 code=300", detail)
        for secret in ("private", "token", "https://"):
            self.assertNotIn(secret, detail)
        await diagnostics.close()

    async def test_snapshot_does_not_block_input_and_persists_page_error_without_check(self):
        manager = ManualCaptchaManager()
        active = ManualSession(id="s", operator_id=1, owner_id=1, account_pk=1,
                               account_id="11", original_cookie="secret", device_id="private", log_id=2, cookies={})
        active.status = "ready"
        started, release = asyncio.Event(), asyncio.Event()
        async def screenshot(**kwargs):
            started.set()
            await release.wait()
            return b"jpeg"
        active.page = SimpleNamespace(screenshot=screenshot, frames=[], mouse=AsyncMock())
        active.diagnostics.page_state = "page_reports_failure"
        active.diagnostics.page_error_code = "L9gXis"
        active.diagnostics.observe_page = AsyncMock()
        manager._persist_diagnostics = AsyncMock()
        manager._probe = AsyncMock()
        snapshot = asyncio.create_task(manager.snapshot(active))
        try:
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(manager.input(active, 1, [ManualCaptchaInput(kind="down", x=1, y=2)]), .2)
            active.page.mouse.down.assert_awaited_once()
        finally:
            release.set()
        result = await snapshot
        self.assertIn("L9gXis", result["message"])
        manager._persist_diagnostics.assert_awaited_once()
        manager._probe.assert_not_awaited()

    async def test_input_preserves_captured_intervals_including_between_batches(self):
        manager = ManualCaptchaManager()
        active = ManualSession(id="s", operator_id=1, owner_id=1, account_pk=1,
                               account_id="11", original_cookie="", device_id="", log_id=2, cookies={})
        active.status = "ready"
        active.page = SimpleNamespace(mouse=AsyncMock())
        # Freeze only the service clock, not the event loop's clock.
        clock = SimpleNamespace(time=lambda: active.expires_at - 30, monotonic=lambda: 100)
        with (patch("app.services.manual_captcha_service.time", clock),
              patch("app.services.manual_captcha_service.asyncio.sleep", AsyncMock()) as sleep):
            await manager.input(active, 1, [ManualCaptchaInput(kind="down", time_ms=0),
                                             ManualCaptchaInput(kind="move", time_ms=40)])
            await manager.input(active, 2, [ManualCaptchaInput(kind="up", time_ms=100)])
        self.assertEqual([call.args for call in sleep.await_args_list], [(0.04,), (0.06,)])
        self.assertEqual(active.page.mouse.move.await_count, 3)
        # Reject a malformed batch atomically, before any additional movement.
        with self.assertRaises(Exception) as error:
            await manager.input(active, 3, [ManualCaptchaInput(kind="down", time_ms=200),
                                             ManualCaptchaInput(kind="move", time_ms=150)])
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(active.page.mouse.move.await_count, 3)

    async def test_late_diagnostic_write_is_conditioned_on_processing_status(self):
        manager = ManualCaptchaManager()
        active = ManualSession(id="s", operator_id=1, owner_id=1, account_pk=1,
                               account_id="11", original_cookie="", device_id="", log_id=2, cookies={})
        active.diagnostics.add("page=page_reports_failure error_code=L9gXis")
        db = AsyncMock()
        with patch("app.services.manual_captcha_service.async_session_maker") as factory:
            factory.return_value.__aenter__.return_value = db
            await manager._persist_diagnostics(active)
            await manager._persist_diagnostics(active)
        db.execute.assert_awaited_once()
        statement = db.execute.await_args.args[0]
        self.assertIn("processing", statement.compile().params.values())
        self.assertIn("11", statement.compile().params.values())
        self.assertIn("L9gXis", statement.compile().params["error_message"])

    async def test_queue_expiry_does_not_discard_observed_failure(self):
        manager = ManualCaptchaManager()
        active = ManualSession(id="s", operator_id=1, owner_id=1, account_pk=1,
                               account_id="11", original_cookie="", device_id="", log_id=2, cookies={})
        active.diagnostics.add("page=page_reports_failure error_code=L9gXis")
        log = SimpleNamespace(processing_status="cancelled", account_identifier="11", error_message=None)
        db = AsyncMock()
        db.get.return_value = log
        manager._close_browser = AsyncMock()
        with patch("app.services.manual_captcha_service.async_session_maker") as factory:
            factory.return_value.__aenter__.return_value = db
            await manager._finish(active, "cancelled", "已超时")
        self.assertEqual(log.processing_status, "cancelled")
        self.assertIn("L9gXis", log.error_message)

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
