import unittest
from unittest.mock import AsyncMock, patch

from app.services.scheduler.cookies_refresh_task import CookiesRefreshTaskService
from common.models.ytx_account import YtxAccount
from common.services.slider_retry_guard import SLIDER_GUARD_KEY


class SliderRetryPauseTests(unittest.IsolatedAsyncioTestCase):
    def account(self):
        return YtxAccount(
            account_id="paused-seller", status="disabled",
            metadata_json={SLIDER_GUARD_KEY: {"paused_at": "2026-10-20T00:00:00+00:00", "consecutive_failures": 3}},
        )

    async def test_cookie_task_never_opens_browser_for_paused_account(self):
        service = CookiesRefreshTaskService()
        service._get_schedule = AsyncMock()
        with patch(
            "app.services.scheduler.cookies_refresh_task.cookies_refresh_browser_service.refresh_account_cookies",
            AsyncMock(),
        ) as browser:
            result = await service._process_account(AsyncMock(), self.account())
        self.assertEqual(result.status, "skipped")
        browser.assert_not_awaited()
        service._get_schedule.assert_not_awaited()

    async def test_successful_old_refresh_cannot_automatically_enable_paused_account(self):
        account = self.account()
        session = AsyncMock()
        await CookiesRefreshTaskService()._enable_account_after_refresh(session, account)
        self.assertEqual(account.status, "disabled")
        session.commit.assert_not_awaited()
