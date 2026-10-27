"""Observe the official page's own Token response; never issue or replay requests."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

from common.services.im_token_api import ImTokenApiResult, build_im_token_api_url


class OfficialTokenObserver:
    def __init__(self):
        self.result: ImTokenApiResult | None = None
        self.generation = 0
        self.closed = False
        self._serial = 0
        self._accepted_serial = 0
        self._tasks: set[asyncio.Task] = set()

    def attach(self, page):
        page.on("response", self.response)

    def response(self, response):
        request = response.request
        url = urlsplit(request.url)
        endpoint = urlsplit(build_im_token_api_url("web"))
        if (self.closed or len(self._tasks) >= 4 or request.method != "POST"
                or url.scheme != "https" or url.netloc != endpoint.netloc
                or url.path != endpoint.path):
            return
        self._serial += 1
        task = asyncio.create_task(self._read(response, self._serial))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _read(self, response, serial: int):
        try:
            async with asyncio.timeout(2):
                body = response.request.post_data or ""
                if len(body) > 32768:
                    return
                data = json.loads(parse_qs(body).get("data", [""])[0])
                if not isinstance(data, dict) or data.get("appKey") != "444e9908a51d1cb236a27862abc769c9":
                    return
                device = data.get("deviceId")
                if not isinstance(device, str) or not 1 <= len(device) <= 128:
                    return
                if int(response.headers.get("content-length", "0")) > 32768:
                    return
                text = await response.text()
                if len(text) > 32768:
                    return
                payload = json.loads(text)
                if not isinstance(payload, dict) or self.closed or serial < self._accepted_serial:
                    return
                self._accepted_serial = serial
                self.result = ImTokenApiResult(
                    response_json=payload, response_cookies={}, status_code=response.status,
                    duration_seconds=0, device_id=device,
                )
                self.generation += 1
        except Exception:
            pass  # Malformed/late responses cannot establish a valid session.

    async def close(self):
        self.closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.result = None
