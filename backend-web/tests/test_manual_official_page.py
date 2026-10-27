import asyncio
import json
import unittest
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import AsyncMock, Mock

from app.services.manual_captcha_service import ManualCaptchaManager, ManualSession
from app.services.official_token_observer import OfficialTokenObserver
from common.services.im_token_api import ImTokenApiResult, build_im_token_api_url


def response(payload, *, url=None, device="official-device", app_key="444e9908a51d1cb236a27862abc769c9"):
    return SimpleNamespace(
        request=SimpleNamespace(url=url or build_im_token_api_url(), method="POST", post_data=urlencode({
            "data": json.dumps({"appKey": app_key, "deviceId": device}),
        })), status=200, headers={"content-type": "application/json"},
        text=AsyncMock(return_value=json.dumps(payload)),
    )


def session():
    active = ManualSession(id="fixture", operator_id=1, owner_id=1, account_pk=1,
                           account_id="123", original_cookie="unb=123", device_id="old-device",
                           log_id=1, cookies={}, interaction_mode="native")
    active.status = "ready"
    active.context = SimpleNamespace(cookies=AsyncMock(return_value=[{"name": "unb", "value": "123"}]))
    active.page = SimpleNamespace(is_closed=Mock(return_value=False), frames=[])
    active.official_observer = OfficialTokenObserver()
    return active


class OfficialPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_observer_accepts_only_exact_official_endpoint_and_expected_payload(self):
        observer = OfficialTokenObserver()
        payload = {"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "fixture"}}
        for kwargs in [
            {"url": "https://h5api.m.goofish.com.evil.test" + build_im_token_api_url().split(".com", 1)[1]},
            {"url": build_im_token_api_url().replace("https:", "http:")},
            {"url": build_im_token_api_url().replace("/1.0/", "/other/")},
            {"app_key": "wrong"}, {"device": ""},
        ]:
            observer.response(response(payload, **kwargs))
        await asyncio.gather(*list(observer._tasks))
        self.assertIsNone(observer.result)
        observer.response(response(payload))
        await asyncio.gather(*list(observer._tasks))
        self.assertEqual(observer.result.device_id, "official-device")
        self.assertEqual(observer.result.response_json, payload)
        await observer.close()
        self.assertIsNone(observer.result)

    async def test_slow_old_response_cannot_replace_newer_result(self):
        observer = OfficialTokenObserver()
        release = asyncio.Event()
        old = response({"ret": ["FAIL_SYS_USER_VALIDATE"]})
        async def delayed():
            await release.wait()
            return '{"ret":["FAIL_SYS_USER_VALIDATE"]}'
        old.text.side_effect = delayed
        observer.response(old)
        await asyncio.sleep(0)
        old_tasks = set(observer._tasks)
        observer.response(response({"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "new"}}))
        newest = list(observer._tasks - old_tasks)
        await asyncio.gather(*newest)
        release.set()
        await asyncio.gather(*list(observer._tasks))
        self.assertEqual(observer.result.response_json["data"]["accessToken"], "new")
        await observer.close()

    async def test_close_cancels_response_before_it_can_publish_credentials(self):
        observer = OfficialTokenObserver()
        entered = asyncio.Event()
        async def delayed():
            entered.set()
            await asyncio.Event().wait()
        pending = response({})
        pending.text.side_effect = delayed
        observer.response(pending)
        await entered.wait()
        await observer.close()
        self.assertIsNone(observer.result)
        self.assertFalse(observer._tasks)
        observer.response(response({"ret": ["SUCCESS::调用成功"]}))
        self.assertFalse(observer._tasks)

    async def test_failed_page_response_never_calls_backend_probe_or_saves_token(self):
        manager, active = ManualCaptchaManager(), session()
        active.interactions = 1
        active.official_observer.result = ImTokenApiResult(
            response_json={"ret": ["FAIL_SYS_USER_VALIDATE"]}, response_cookies={},
            status_code=200, duration_seconds=0, device_id="official-device",
        )
        active.official_observer.generation = 1
        manager._probe = AsyncMock()
        manager._record_check = AsyncMock()
        manager._save_success = AsyncMock()
        await manager.check(active)
        manager._probe.assert_not_awaited()
        manager._save_success.assert_not_awaited()
        self.assertEqual(active.status, "ready")

    async def test_observed_success_uses_official_device_and_needs_no_extra_request(self):
        manager, active = ManualCaptchaManager(), session()
        active.official_observer.result = ImTokenApiResult(
            response_json={"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "fixture"}},
            response_cookies={}, status_code=200, duration_seconds=0, device_id="official-device",
        )
        active.official_observer.generation = 1
        manager._probe = AsyncMock()
        manager._record_check = AsyncMock()
        async def saved(s, token, message):
            self.assertEqual(s.device_id, "official-device")
            self.assertEqual(s.cookies, {"unb": "123"})
            s.status = "success"
        manager._save_success = AsyncMock(side_effect=saved)
        result = await manager.snapshot(active)
        self.assertEqual(result["status"], "success")
        manager._save_success.assert_awaited_once()
        manager._probe.assert_not_awaited()
        self.assertEqual(active.interactions, 0)


if __name__ == "__main__":
    unittest.main()
