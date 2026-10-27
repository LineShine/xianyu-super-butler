"""Token request wire contract; all HTTP traffic stays on loopback."""
import asyncio
import hashlib
import json
import unittest
from urllib.parse import parse_qs
from unittest.mock import patch

from aiohttp import web

from common.services.captcha.token_refetch import _post_token_api
from common.services.im_token_api import build_im_token_request, request_im_token


# Core query keys observed with the official lib-mtop 2.7.3 SDK offline.
# Its actual jsv is 2.7.2. Page-derived analytics are deliberately not replayed.
CORE_QUERY_KEYS = {
    "jsv", "appKey", "t", "sign", "v", "type", "accountSite", "dataType",
    "timeout", "api", "sessionOption",
}


class ImTokenRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_and_sync_clients_send_valid_json_with_matching_signature(self):
        received = []

        async def endpoint(request):
            received.append((dict(request.query), parse_qs(await request.text())))
            return web.json_response({"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "fixture"}})

        app = web.Application()
        app.router.add_post("/h5/{api}/1.0/", endpoint)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{runner.addresses[0][1]}/h5"
        # Quotes, slashes, Unicode and form delimiters must survive exactly once.
        device = 'fixture-"设备\\id&value=+%20'
        cookie = "unb=fixture; _m_h5_tk=fixture_9999999999999"
        try:
            with patch("common.services.im_token_api.IM_TOKEN_API_BASE_URL", base):
                await request_im_token(cookie, device)
                await asyncio.to_thread(
                    _post_token_api, "fixture", "web", {"_m_h5_tk": "fixture_9999999999999"},
                    cookie, device, 5,
                )
        finally:
            await runner.cleanup()
        self.assertEqual(len(received), 2)
        for query, form in received:
            self.assertEqual(set(query), CORE_QUERY_KEYS)
            self.assertEqual(query["jsv"], "2.7.2")
            self.assertEqual(query["api"], "mtop.taobao.idlemessage.pc.login.token")
            self.assertEqual(set(form), {"data"})
            raw_data = form["data"][0]
            self.assertEqual(json.loads(raw_data), {
                "appKey": "444e9908a51d1cb236a27862abc769c9", "deviceId": device,
            })
            digest = hashlib.md5(f"fixture&{query['t']}&34839810&{raw_data}".encode()).hexdigest()
            self.assertEqual(query["sign"], digest)

    def test_normal_device_preserves_existing_signed_body_format(self):
        params, data = build_im_token_request({"_m_h5_tk": "fixture_expiry"}, "fixture-device")
        self.assertEqual(data, '{"appKey":"444e9908a51d1cb236a27862abc769c9","deviceId":"fixture-device"}')
        self.assertEqual(set(params), CORE_QUERY_KEYS)

    def test_missing_device_remains_empty_json_string(self):
        _, data = build_im_token_request({}, "")
        self.assertEqual(json.loads(data)["deviceId"], "")


if __name__ == "__main__":
    unittest.main()
