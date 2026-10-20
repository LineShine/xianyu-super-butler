import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.api.routes.risk_control_logs import ManualCaptchaInput, ManualCaptchaInputs
from app.services.manual_captcha_service import (
    LEASE_KEY, ManualCaptchaManager, ManualSession, allowed_url, check_account, token_failure_message,
)
from common.models.user import UserRole
from common.models.ytx_account import YtxAccount
from common.services import manual_verification_queue as verification_queue
from common.services.slider_retry_guard import MANUAL_RESUME_KEY


def session():
    return ManualSession(
        id="test-session", operator_id=1, owner_id=1, account_pk=10,
        account_id="123", original_cookie="unb=123; x5sec=old",
        device_id="device", log_id=20, cookies={"unb": "123", "x5sec": "old"},
    )


def success_database(active):
    active.queued = True
    account = SimpleNamespace(
        id=active.account_pk, account_id=active.account_id, owner_id=1, status="disabled",
        cookie=active.original_cookie, disable_reason="连续三次失败",
        metadata_json={
            verification_queue.QUEUE_KEY: {
                "id": active.id, "log_id": active.log_id, "status": "processing",
                "requested_at": time.time(), "expires_at": active.expires_at,
                "url": "https://taobao.com/punish?private=true", "device_id": "device",
            },
            verification_queue.HOLD_KEY: True,
            "slider_retry_guard": {"paused_at": "today"},
        },
    )
    log = SimpleNamespace(processing_status="processing", processing_result=None)
    db = AsyncMock()
    def execute(statement):
        row = Mock()
        params = statement.compile().params
        row.scalar_one_or_none.return_value = 25 if "account_identifier_1" in params else account
        return row
    db.execute.side_effect = execute
    db.get.side_effect = lambda model, pk: account if model is YtxAccount else log
    return db, account, log


class ManualCaptchaTests(unittest.IsolatedAsyncioTestCase):
    def test_challenge_url_and_resources_are_restricted(self):
        self.assertTrue(allowed_url("https://h5api.m.goofish.com/h5/a/punish?x5secdata=a", challenge=True))
        self.assertTrue(allowed_url("https://g.alicdn.com/script.js"))
        for url in [
            "http://h5api.m.goofish.com/punish", "https://127.0.0.1/punish",
            "https://taobao.com.evil.test/punish", "https://taobao.com@localhost/punish",
            "https://taobao.com:8189/punish", "file:///etc/passwd",
            "https://www.goofish.com/", "https://g.alicdn.com/punish",
        ]:
            self.assertFalse(allowed_url(url, challenge=True), url)

    def test_account_ownership_and_paused_state_required(self):
        account = SimpleNamespace(status="disabled", owner_id=2)
        check_account(account, None)
        check_account(account, 2)
        with self.assertRaises(HTTPException) as error:
            check_account(account, 1)
        self.assertEqual(error.exception.status_code, 404)
        account.status = "active"
        with self.assertRaises(HTTPException) as error:
            check_account(account, None)
        self.assertEqual(error.exception.status_code, 409)

    def test_input_rejects_unbounded_coordinates_and_arbitrary_keys(self):
        for fields in ({"x": float("nan")}, {"x": 901}, {"y": -1}, {"key": "Control+L"}):
            with self.assertRaises(ValidationError):
                ManualCaptchaInput(kind="move", **fields)
        with self.assertRaises(ValidationError):
            ManualCaptchaInputs(sequence=1, events=[{"kind": "move"}] * 65)

    async def test_session_cannot_be_accessed_by_another_operator(self):
        manager = ManualCaptchaManager()
        active = session()
        manager.sessions[active.id] = active
        with self.assertRaises(HTTPException) as error:
            await manager.access(active.id, SimpleNamespace(id=2))
        self.assertEqual(error.exception.status_code, 404)

    async def test_access_rechecks_account_changes(self):
        manager, active = ManualCaptchaManager(), session()
        manager.sessions[active.id] = active
        db = AsyncMock()
        db.get.return_value = SimpleNamespace(owner_id=1, status="disabled", cookie="changed")
        with patch("app.services.manual_captcha_service.async_session_maker") as factory:
            factory.return_value.__aenter__.return_value = db
            with self.assertRaises(HTTPException) as error:
                await manager.access(active.id, SimpleNamespace(id=1, role=UserRole.MEMBER))
            self.assertEqual(error.exception.status_code, 409)

    async def test_success_can_be_polled_after_account_automatically_enabled(self):
        manager, active = ManualCaptchaManager(), session()
        active.status = "success"
        manager.sessions[active.id] = active
        db = AsyncMock()
        db.get.return_value = SimpleNamespace(owner_id=1, status="active", cookie=active.original_cookie)
        with patch("app.services.manual_captcha_service.async_session_maker") as factory:
            factory.return_value.__aenter__.return_value = db
            self.assertIs(await manager.access(active.id, SimpleNamespace(id=1, role=UserRole.MEMBER)), active)

    async def test_input_forwards_human_points_in_order_without_generating_a_path(self):
        manager, active = ManualCaptchaManager(), session()
        active.status = "ready"
        active.page = SimpleNamespace(mouse=AsyncMock())
        events = [ManualCaptchaInput(kind=kind, x=x, y=100)
                  for kind, x in (("down", 10), ("move", 42), ("up", 61))]
        await manager.input(active, 1, events)
        self.assertEqual([call.args for call in active.page.mouse.move.await_args_list],
                         [(10, 100), (42, 100), (61, 100)])
        active.page.mouse.down.assert_awaited_once()
        active.page.mouse.up.assert_awaited_once()
        self.assertFalse(active.pressed)
        await manager.input(active, 1, events)  # duplicate batch is idempotent
        self.assertEqual(active.page.mouse.move.await_count, 3)
        with self.assertRaises(HTTPException):
            await manager.input(active, 3, events)

    async def test_probe_calls_official_endpoint_once_without_remote_fallback(self):
        manager, active = ManualCaptchaManager(), session()
        result = SimpleNamespace(response_cookies={"_m_h5_tk": "new"})
        with patch("app.services.manual_captcha_service.request_im_token", new=AsyncMock(return_value=result)) as probe:
            await manager._probe(active)
        probe.assert_awaited_once_with(active.original_cookie, "device", api_mode="web", timeout_seconds=20)
        self.assertEqual(active.cookies["_m_h5_tk"], "new")

    async def test_expired_signing_cookie_requires_explicit_bounded_refresh(self):
        manager, active = ManualCaptchaManager(), session()
        manager._probe = AsyncMock(return_value=SimpleNamespace(
            response_json={"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"]},
            response_cookies={"_m_h5_tk": "new"},
        ))
        await manager._load_challenge(active)
        self.assertEqual(active.status, "refresh_required")
        self.assertEqual(manager._probe.await_count, 1)
        async def finish(s, status, message):
            s.status, s.message = status, message
        manager._finish = AsyncMock(side_effect=finish)
        await manager.refresh(active)
        self.assertEqual(active.status, "failed")
        self.assertTrue(active.refresh_used)
        with self.assertRaises(HTTPException):
            await manager.refresh(active)
        self.assertEqual(manager._probe.await_count, 2)

    async def test_missing_signing_cookie_can_be_initialized_by_explicit_refresh(self):
        manager, active = ManualCaptchaManager(), session()
        manager._probe = AsyncMock(return_value=SimpleNamespace(
            response_json={"ret": ["FAIL_SYS_TOKEN_EMPTY::令牌为空"]},
            response_cookies={"_m_h5_tk": "new"},
        ))
        await manager._load_challenge(active)
        self.assertEqual(active.status, "refresh_required")
        self.assertEqual(manager._probe.await_count, 1)

    async def test_fresh_challenge_opens_official_page_without_mouse_actions(self):
        manager, active = ManualCaptchaManager(), session()
        active.context, active.page = AsyncMock(), AsyncMock()
        url = "https://h5api.m.goofish.com/h5/test/punish?x5secdata=example"
        manager._probe = AsyncMock(return_value=SimpleNamespace(
            response_json={"data": {"url": url}}, response_cookies={},
        ))
        await manager._load_challenge(active)
        self.assertEqual(active.status, "ready")
        active.page.goto.assert_awaited_once_with(url, wait_until="domcontentloaded", timeout=25000)
        active.page.mouse.move.assert_not_called()
        active.page.screenshot.return_value = b"jpeg"
        snapshot = await manager.snapshot(active)
        self.assertTrue(snapshot["image"].startswith("data:image/jpeg;base64,"))
        self.assertNotIn("cookies", snapshot)
        self.assertNotIn("device_id", snapshot)
        self.assertNotIn("url", snapshot)

    def test_error_message_never_includes_raw_response_or_signed_url(self):
        self.assertIn("重新登录", token_failure_message({"ret": ["FAIL_SYS_SESSION_EXPIRED::Cookie=secret"]}))
        message = token_failure_message({"ret": ["SOME_ERROR::https://secret", "cookie=secret"]})
        self.assertIn("SOME_ERROR", message)
        self.assertNotIn("secret", message)

    async def test_cookie_alone_is_not_success_and_check_is_rate_limited(self):
        manager, active = ManualCaptchaManager(), session()
        active.status, active.interactions = "ready", 1
        active.context = SimpleNamespace(cookies=AsyncMock(return_value=[
            {"domain": ".goofish.com", "name": "x5sec", "value": "new"},
        ]))
        manager._probe = AsyncMock(return_value=SimpleNamespace(response_json={"ret": ["FAIL_SYS_USER_VALIDATE"]}))
        manager._save_success = AsyncMock()
        await manager.check(active)
        manager._save_success.assert_not_called()
        self.assertEqual(active.status, "ready")
        with self.assertRaises(HTTPException) as error:
            await manager.check(active)
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(manager._probe.await_count, 1)

    async def test_three_failed_checks_stop_and_no_background_retry(self):
        manager, active = ManualCaptchaManager(), session()
        active.status, active.interactions = "ready", 1
        active.context = SimpleNamespace(cookies=AsyncMock(return_value=[]))
        manager._probe = AsyncMock(return_value=SimpleNamespace(response_json={}))
        async def finish(s, status, message):
            s.status, s.message = status, message
        manager._finish = AsyncMock(side_effect=finish)
        for _ in range(3):
            active.last_check = 0
            await manager.check(active)
        self.assertEqual(active.status, "failed")
        await manager.check(active)
        self.assertEqual(manager._probe.await_count, 3)

    async def test_check_requires_user_input(self):
        manager, active = ManualCaptchaManager(), session()
        active.status = "ready"
        manager._probe = AsyncMock()
        with self.assertRaises(HTTPException):
            await manager.check(active)
        manager._probe.assert_not_called()

    async def test_success_saves_token_resets_guard_enables_only_target_and_releases_queue(self):
        manager, active = ManualCaptchaManager(), session()
        db, account, log = success_database(active)
        other = SimpleNamespace(status="disabled", cookie="unb=456")
        manager._close_browser = AsyncMock()
        active.cookies["x5sec"] = "verified"
        with (patch("app.services.manual_captcha_service.async_session_maker") as factory,
              patch("app.services.websocket_client.websocket_client.http_client.post",
                    AsyncMock(return_value={"success": True})) as start):
            factory.return_value.__aenter__.return_value = db
            await manager._save_success(active, "token", "通过")
        self.assertEqual(account.cookie, "unb=123; x5sec=verified")
        self.assertEqual(account.status, "active")
        self.assertIsNone(account.disable_reason)
        self.assertEqual(account.metadata_json["slider_retry_guard"],
                         {"consecutive_failures": 0, "resume_after_log_id": 25})
        self.assertNotIn(verification_queue.HOLD_KEY, account.metadata_json)
        self.assertNotIn(MANUAL_RESUME_KEY, account.metadata_json)
        self.assertEqual(verification_queue.state_of(account)["status"], "success")
        self.assertNotIn("url", verification_queue.state_of(account))
        self.assertEqual(other.cookie, "unb=456")
        self.assertEqual(other.status, "disabled")
        self.assertEqual(log.processing_status, "success")
        self.assertEqual(active.status, "success")
        start.assert_awaited_once()
        self.assertTrue(start.await_args.args[0].endswith("/internal/accounts/123/start"))
        self.assertEqual(start.await_args.kwargs, {"json": {}, "timeout": 8, "max_retries": 1})
        cached_ids = [call.args[0].compile().params.get("user_id") for call in db.execute.await_args_list
                      if "user_id" in call.args[0].compile().params]
        self.assertEqual(cached_ids, ["123", "chat_123"])

    async def test_expired_or_cancelled_verification_never_enables_or_starts_account(self):
        for scenario in ("expired", "cancelled", "cookie_changed", "owner_changed"):
            with self.subTest(scenario=scenario):
                manager, active = ManualCaptchaManager(), session()
                db, account, _ = success_database(active)
                if scenario == "expired": active.expires_at = time.time() - 1
                if scenario == "cancelled":
                    account.metadata_json[verification_queue.QUEUE_KEY]["status"] = "cancelled"
                if scenario == "cookie_changed": account.cookie = "unb=123; new_login=true"
                if scenario == "owner_changed": account.owner_id = 2
                with (patch("app.services.manual_captcha_service.async_session_maker") as factory,
                      patch("app.services.websocket_client.websocket_client.http_client.post", AsyncMock()) as start):
                    factory.return_value.__aenter__.return_value = db
                    with self.assertRaises(HTTPException):
                        await manager._save_success(active, "token", "通过")
                self.assertEqual(account.status, "disabled")
                start.assert_not_awaited()
                db.commit.assert_not_awaited()

    async def test_connection_start_failure_keeps_verification_result_and_pauses_only_target(self):
        manager, active = ManualCaptchaManager(), session()
        db, account, log = success_database(active)
        manager._close_browser = AsyncMock()
        with (patch("app.services.manual_captcha_service.async_session_maker") as factory,
              patch("app.services.websocket_client.websocket_client.http_client.post",
                    AsyncMock(return_value={"success": False})) as start):
            factory.return_value.__aenter__.return_value = db
            await manager._save_success(active, "token", "通过")
        start.assert_awaited_once()
        self.assertEqual(account.status, "disabled")
        self.assertTrue(account.metadata_json[verification_queue.HOLD_KEY])
        self.assertIn("连接服务启动失败", account.disable_reason)
        self.assertEqual(log.processing_status, "success")
        self.assertEqual(verification_queue.state_of(account)["status"], "success")
        self.assertEqual(active.status, "failed")

    async def test_newer_account_change_wins_over_late_connection_failure(self):
        manager, active = ManualCaptchaManager(), session()
        db, account, _ = success_database(active)
        manager._close_browser = AsyncMock()
        async def start(*args, **kwargs):
            # Explicit re-enable clears the recovery marker; an old failure must
            # not subsequently disable the user's newly enabled account.
            account.metadata_json.pop(MANUAL_RESUME_KEY)
            account.cookie = "unb=123; newer=true"
            return {"success": False}
        with (patch("app.services.manual_captcha_service.async_session_maker") as factory,
              patch("app.services.websocket_client.websocket_client.http_client.post", side_effect=start)):
            factory.return_value.__aenter__.return_value = db
            await manager._save_success(active, "token", "通过")
        self.assertEqual(account.status, "active")
        self.assertEqual(account.cookie, "unb=123; newer=true")
        self.assertNotIn(verification_queue.HOLD_KEY, account.metadata_json)

    async def test_closing_after_committed_success_does_not_cancel_connection_recovery(self):
        manager, active = ManualCaptchaManager(), session()
        db, account, _ = success_database(active)
        manager.sessions[active.id] = active
        manager._close_browser = AsyncMock()
        started, release = asyncio.Event(), asyncio.Event()
        async def start(*args, **kwargs):
            started.set()
            await release.wait()
            return {"success": True}
        with (patch("app.services.manual_captcha_service.async_session_maker") as factory,
              patch("app.services.websocket_client.websocket_client.http_client.post", side_effect=start)):
            factory.return_value.__aenter__.return_value = db
            checking = asyncio.create_task(manager._save_success(active, "token", "通过"))
            active.operation_tasks.add(checking)
            await asyncio.wait_for(started.wait(), 1)
            recovery = list(manager.recovery_tasks)
            await manager.close(active)
            self.assertTrue(checking.cancelled())
            self.assertTrue(recovery and not recovery[0].done())
            self.assertNotIn(active.id, manager.sessions)
            self.assertEqual(verification_queue.state_of(account)["status"], "success")
            release.set()
            await asyncio.wait_for(asyncio.gather(*recovery), 1)
        self.assertEqual(account.status, "active")
        self.assertNotIn(MANUAL_RESUME_KEY, account.metadata_json)

    async def test_close_cancels_start_and_releases_only_own_lease(self):
        manager, active = ManualCaptchaManager(), session()
        manager.sessions[active.id] = active
        active.init_task = asyncio.create_task(asyncio.sleep(30))
        account = SimpleNamespace(metadata_json={LEASE_KEY: {"id": active.id, "started_at": 1, "expires_at": 99}, "keep": True})
        db = AsyncMock()
        db.get.return_value = SimpleNamespace(processing_status="processing")
        row = Mock()
        row.scalar_one_or_none.return_value = account
        db.execute.return_value = row
        with patch("app.services.manual_captcha_service.async_session_maker") as factory:
            factory.return_value.__aenter__.return_value = db
            await manager.close(active)
        self.assertTrue(active.init_task.cancelled())
        self.assertNotIn(active.id, manager.sessions)
        self.assertEqual(account.metadata_json[LEASE_KEY]["expires_at"], 0)
        self.assertTrue(account.metadata_json["keep"])
        self.assertEqual(active.status, "cancelled")
