import unittest
from unittest.mock import AsyncMock, Mock

from app.services.account_service import AccountService
from common.models.ytx_account import YtxAccount
from common.models.risk_control_log import YtxRiskControlLog
from common.services.slider_retry_guard import (
    SLIDER_GUARD_KEY, SLIDER_PAUSE_REASON, apply_slider_result, is_slider_paused,
    slider_guard_state,
)


class SliderRetryGuardTests(unittest.IsolatedAsyncioTestCase):
    def account(self, name="seller"):
        return YtxAccount(account_id=name, status="active", metadata_json={"keep": True})

    def finish(self, account, log_id, result="failed", previous="processing", call_type="local"):
        log = YtxRiskControlLog(
            id=log_id, account_identifier=account.account_id,
            event_type="slider_captcha", call_type=call_type, processing_status=result,
        )
        apply_slider_result(account, log, previous)

    async def test_third_failure_pauses_only_target_and_survives_recreation(self):
        target, other = self.account(), self.account("other")
        for log_id in (1, 2):
            self.finish(target, log_id)
            self.assertEqual(target.status, "active")
        self.finish(target, 3)
        self.assertEqual(target.status, "disabled")
        self.assertEqual(target.disable_reason, SLIDER_PAUSE_REASON)
        self.assertTrue(is_slider_paused(target))
        restored = YtxAccount(status=target.status, metadata_json=target.metadata_json)
        self.assertTrue(is_slider_paused(restored))
        self.assertEqual(other.status, "active")
        self.assertEqual(other.metadata_json, {"keep": True})
        self.assertTrue(target.metadata_json["keep"])

    async def test_success_breaks_failure_series(self):
        target = self.account()
        self.finish(target, 1)
        self.finish(target, 2)
        self.finish(target, 3, "success")
        self.finish(target, 4)
        self.assertEqual(slider_guard_state(target)["consecutive_failures"], 1)
        self.assertFalse(is_slider_paused(target))

    async def test_duplicates_cancellation_and_remote_calls_do_not_count(self):
        target = self.account()
        self.finish(target, 1)
        self.finish(target, 1, previous="failed")
        self.finish(target, 2, "cancelled")
        self.finish(target, 3, call_type="remote")
        self.assertEqual(slider_guard_state(target)["consecutive_failures"], 1)

    async def test_errors_count_and_late_success_cannot_resume_paused_account(self):
        target = self.account()
        for i in (1, 2, 3):
            self.finish(target, i, "error")
        self.finish(target, 4, "success")
        self.assertTrue(is_slider_paused(target))
        self.assertEqual(slider_guard_state(target)["consecutive_failures"], 3)

    async def test_manual_enable_resets_guard_and_ignores_old_inflight_work(self):
        target = self.account()
        for i in (1, 2, 3):
            self.finish(target, i)
        session = AsyncMock()
        session.add = Mock()
        latest = Mock()
        latest.scalar_one_or_none.return_value = 4
        session.execute.return_value = latest
        await AccountService(session).update_status(target, True)
        self.assertEqual(target.status, "active")
        self.assertIsNone(target.disable_reason)
        self.assertFalse(is_slider_paused(target))
        self.finish(target, 4)
        self.assertEqual(target.metadata_json[SLIDER_GUARD_KEY]["consecutive_failures"], 0)
        self.finish(target, 5)
        self.assertEqual(slider_guard_state(target)["consecutive_failures"], 1)

    async def test_manual_disable_is_preserved(self):
        target = self.account()
        target.status = "disabled"
        target.disable_reason = "手动禁用"
        for i in (1, 2, 3):
            self.finish(target, i)
        self.assertFalse(is_slider_paused(target))
        self.assertEqual(target.disable_reason, "手动禁用")
