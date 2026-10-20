"""Token service URLs must not receive slider-service credentials or requests."""
import unittest
from unittest.mock import AsyncMock, patch

from app.api.routes.captcha import (
    RemoteConfigUpdate,
    TestRemoteSolveRequest,
    _check_token_api_domain,
    test_remote_slider_solve,
    update_remote_config,
)


class RemoteCaptchaConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_service_cannot_be_saved_as_slider_service(self):
        for host in ("api.xianyushop.shop", "api.xianyusite.shop", "api.zhinianblog.cn"):
            with self.subTest(host=host):
                with patch("app.api.routes.captcha.SystemSettingService") as settings:
                    result = await update_remote_config(
                        RemoteConfigUpdate(url=f"https://{host}/api/external/invoke", secret_key="test-only"),
                        current_user=None,
                        db=AsyncMock(),
                    )
                    self.assertFalse(result.success)
                    self.assertIn("Token获取方式", result.message)
                    settings.assert_not_called()

    async def test_wrong_service_test_never_sends_credentials(self):
        with patch("aiohttp.ClientSession") as http:
            result = await test_remote_slider_solve(
                TestRemoteSolveRequest(
                    url="https://API.XIANYUSHOP.SHOP/api/external/invoke",
                    secret_key="test-only",
                ),
                current_user=None,
            )
            self.assertFalse(result.success)
            http.assert_not_called()

    def test_slider_provider_and_self_hosted_urls_remain_available(self):
        for url in (
            "https://xy-api.zhinianboke.com/api/v1/captcha/slider-solve",
            "https://my-slider.example/api/v1/captcha/slider-solve",
            "",
        ):
            with self.subTest(url=url):
                self.assertIsNone(_check_token_api_domain(url))
