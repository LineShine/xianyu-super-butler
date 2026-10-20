import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.xianyu.cookie_token_manager import CookieTokenManager
from app.services.xianyu.token_manager import TokenManager
from common.services.im_token_api import request_im_token_with_fallback, ImTokenApiResult


@asynccontextmanager
async def unlocked(*args, **kwargs):
    yield


class SliderRetryPauseTests(unittest.IsolatedAsyncioTestCase):
    async def test_paused_account_makes_no_token_request_and_closes_only_its_socket(self):
        parent = SimpleNamespace(
            myid="seller", cookie_id="seller", current_token="old",
            connection_manager=SimpleNamespace(ws=SimpleNamespace(close=AsyncMock())),
        )
        manager = CookieTokenManager(parent)
        with (
            patch("common.services.slider_retry_guard.account_slider_paused", AsyncMock(return_value=True)),
            patch("app.services.xianyu.cookie_token_manager.token_request_lock", unlocked),
            patch("app.services.xianyu.cookie_token_manager.request_im_token_with_fallback", AsyncMock()) as request,
        ):
            self.assertIsNone(await manager.refresh_token())
            request.assert_not_awaited()
        self.assertEqual(parent.last_token_refresh_status, "paused_slider_failures")
        parent.connection_manager.ws.close.assert_awaited_once()

    async def test_shared_token_api_blocks_paused_account_before_http(self):
        with (
            patch("common.services.slider_retry_guard.account_slider_paused", AsyncMock(return_value=True)),
            patch("common.services.manual_verification_queue.held", AsyncMock(return_value=False)),
            patch("common.services.im_token_api.request_im_token", AsyncMock()) as request,
        ):
            with self.assertRaisesRegex(RuntimeError, "已暂停"):
                await request_im_token_with_fallback("unb=seller", "device")
            request.assert_not_awaited()

    async def test_shared_token_api_preserves_other_account(self):
        token = ImTokenApiResult(response_json={}, response_cookies={}, status_code=200, duration_seconds=0)
        with (
            patch("common.services.manual_verification_queue.held", AsyncMock(return_value=False)),
            patch("common.services.slider_retry_guard.account_slider_paused", AsyncMock(return_value=False)),
            patch("common.services.im_token_api.request_im_token", AsyncMock(return_value=token)) as request,
        ):
            self.assertEqual(await request_im_token_with_fallback("unb=other", "device"), token)
            request.assert_awaited_once()

    async def test_cookie_refresh_does_not_retry_after_pause(self):
        parent = SimpleNamespace(
            cookie_id="seller", token_refresh_interval=60, token_retry_interval=5,
            refresh_token=AsyncMock(return_value=None),
            last_token_refresh_status="paused_slider_failures",
            _interruptible_sleep=AsyncMock(),
        )
        manager = TokenManager(parent)
        await manager._execute_cookie_refresh(100)
        parent.refresh_token.assert_awaited_once()
        parent._interruptible_sleep.assert_not_awaited()
        self.assertFalse(manager.cookie_refresh_enabled)
