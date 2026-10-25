import unittest
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.services.scheduler.api_cookie_renew_task import ApiCookieRenewTaskService
from app.services.scheduler.cookies_refresh_task import CookiesRefreshTaskService
from app.services.scheduler.login_renew_task import LoginRenewTaskService
from common.services.cookie_renew_api_service import CookieRenewApiResult
from common.utils.time_utils import get_beijing_now_naive


@asynccontextmanager
async def acquired(*args):
    yield True


@asynccontextmanager
async def occupied(*args):
    yield False


def account():
    return SimpleNamespace(account_id="seller", cookie="original", status="active",
                           metadata_json={}, disable_reason=None)


class VerificationRenewalGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_api_result_cannot_overwrite_new_login_or_enable_verifying_account(self):
        seller, db = account(), AsyncMock()
        service = ApiCookieRenewTaskService()

        async def renew(*args, **kwargs):
            seller.cookie = "verified"
            seller.metadata_json = {"manual_verification_hold": True}
            seller.status = "disabled"
            return CookieRenewApiResult(True, "stale", updated_cookie_names=["unb"])

        with patch("app.services.scheduler.api_cookie_renew_task.cookie_renew_api_service.renew", side_effect=renew):
            result = await service._renew_account(db, seller)
            await service._enable_account_after_renew(db, seller)
        self.assertEqual(result.status, "skipped")
        self.assertEqual((seller.cookie, seller.status), ("verified", "disabled"))
        db.rollback.assert_not_awaited()  # The outer loop must still be able to read account fields.

    async def test_pending_verification_stops_login_and_browser_requests(self):
        seller, db = account(), AsyncMock()
        login = LoginRenewTaskService()
        login._fetch_login_user_for_renew = AsyncMock()
        browser = CookiesRefreshTaskService()
        browser._get_schedule = AsyncMock()
        with (
            patch("common.services.account_credential_guard.credential_renewal", occupied),
            patch("common.services.slider_retry_guard.account_slider_paused", AsyncMock(return_value=False)),
        ):
            status, _ = await login._renew_account(db, seller)
            result = await browser._process_account(db, seller)
        self.assertEqual((status, result.status), ("skipped", "skipped"))
        login._fetch_login_user_for_renew.assert_not_awaited()
        browser._get_schedule.assert_not_awaited()

    async def test_late_login_check_does_not_write_or_trigger_password_login(self):
        seller, db = account(), AsyncMock()
        service = LoginRenewTaskService()

        async def fetch(*args):
            seller.metadata_json = {"manual_verification_hold": True}
            return {"status": "session_expired", "new_cookies": "stale"}

        service._fetch_login_user_for_renew = AsyncMock(side_effect=fetch)
        with (
            patch("common.services.account_credential_guard.credential_renewal", acquired),
            patch("common.utils.cookie_refresh.trigger_password_login_async", Mock()) as password,
        ):
            status, _ = await service._renew_account(db, seller)
        self.assertEqual(status, "skipped")
        self.assertEqual(seller.cookie, "original")
        password.assert_not_called()

    async def test_late_browser_cookie_snapshot_is_discarded(self):
        seller, db = account(), AsyncMock()
        service = CookiesRefreshTaskService()
        service._get_schedule = AsyncMock(return_value=SimpleNamespace(
            expire_at=get_beijing_now_naive() - timedelta(seconds=1)))

        async def refresh(*args):
            seller.metadata_json = {"manual_verification_hold": True}
            return SimpleNamespace(success=True, cookies=[{"name": "unb", "value": "stale"}])

        with (
            patch("common.services.account_credential_guard.credential_renewal", acquired),
            patch("common.services.slider_retry_guard.account_slider_paused", AsyncMock(return_value=False)),
            patch("app.services.scheduler.cookies_refresh_task.cookies_refresh_browser_service.refresh_account_cookies", side_effect=refresh),
        ):
            result = await service._process_account(db, seller)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(seller.cookie, "original")
