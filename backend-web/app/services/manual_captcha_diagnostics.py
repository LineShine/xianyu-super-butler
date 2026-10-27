"""Bounded diagnostics for a human-operated verification page, without credentials."""
from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from urllib.parse import urlsplit

from common.utils.time_utils import get_beijing_now_naive


def response_codes(payload: object, depth: int = 0) -> list[str]:
    """Only documented-shaped status fields, never arbitrary messages or tokens."""
    if not isinstance(payload, dict) or depth > 2:
        return []
    result = []
    for key in ("code", "errorCode", "error_code", "status", "success"):
        value = payload.get(key)
        if isinstance(value, bool):
            result.append(f"{key}={str(value).lower()}")
        elif (isinstance(value, (str, int)) and not isinstance(value, bool)
              and re.fullmatch(r"-?\d{1,6}|[FS]\d{3}|SUCCESS|FAIL|ERROR|OK|DENY|BLOCK", str(value))):
            result.append(f"{key}={value}")
    for key in ("data", "result"):
        result.extend(response_codes(payload.get(key), depth + 1))
    return result[:8]


class VerificationDiagnostics:
    def __init__(self):
        self.lines: deque[str] = deque(maxlen=20)
        self.generation = 0
        self.saved_generation = -1
        self.save_lock = asyncio.Lock()
        self.page_state = "unknown"
        self.page_error_code = ""
        self.last_page_failure = ""
        self._last_detail = ""
        self._tasks: set[asyncio.Task] = set()
        self.closed = False

    def add(self, detail: str):
        if self.closed or detail == self._last_detail:
            return
        self._last_detail = detail
        self.lines.append(f"{get_beijing_now_naive():%H:%M:%S} {detail}")
        self.generation += 1

    def text(self) -> str:
        lines = list(self.lines)
        if self.last_page_failure and not any(self.last_page_failure in line for line in lines):
            lines.insert(0, self.last_page_failure)
        return "\n".join(lines)

    @staticmethod
    def request_label(request) -> tuple[str, bool]:
        parsed = urlsplit(request.url)
        # Paths and query strings can contain signed challenges and session IDs.
        host = parsed.hostname or "unknown"
        # The actual Baxia submission uses /_____tmd_____/slide, which does
        # not contain any of the generic CAPTCHA keywords below.
        slide = parsed.path.rstrip("/").endswith("/_____tmd_____/slide")
        verification = (slide or bool(re.search(r"analyze|checkcode|verify|captcha|punish", parsed.path, re.I))
                        or host in {"cf.aliyun.com", "cfall.aliyun.com", "ynuf.aliapp.org", "us.ynuf.aliapp.org"})
        kind = "slide" if slide else "verification" if verification else "resource"
        return f"endpoint={kind} host={host[:160]} type={request.resource_type}", verification

    def blocked(self, request):
        label, _ = self.request_label(request)
        self.add(f"request_blocked {label}")

    def request_failed(self, request):
        label, _ = self.request_label(request)
        match = re.search(r"net::ERR_[A-Z_]{1,70}", request.failure or "")
        self.add(f"request_failed {label} error={match[0] if match else 'network_error'}")

    def response(self, response):
        label, verification = self.request_label(response.request)
        if not verification and response.request.resource_type not in ("xhr", "fetch") and response.status < 400:
            return
        self.add(f"response {label} http={response.status}")
        if verification and len(self._tasks) < 8 and not self.closed:
            task = asyncio.create_task(self._response_codes(response, label))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _response_codes(self, response, label: str):
        try:
            async with asyncio.timeout(1):
                headers = response.headers
                if not any(kind in headers.get("content-type", "") for kind in ("json", "javascript")):
                    return
                if int(headers.get("content-length", "0")) > 32768:
                    return
                body = await response.text()
                if len(body) > 32768:
                    return
                # Some official verification endpoints use JSONP. Parse the
                # payload, but never evaluate scripts or log the response body.
                body = re.sub(r"^[\w.$]+\s*\(\s*", "", body.strip())
                body = re.sub(r"\s*\)\s*;?\s*$", "", body)
                codes = response_codes(json.loads(body))
                if codes:
                    self.add(f"response_codes {label} http={response.status} {' '.join(codes)}")
        except Exception:
            pass  # Optional diagnostics must not interrupt human verification.

    def attach(self, page):
        page.on("response", self.response)
        page.on("requestfailed", self.request_failed)
        # Exception messages often contain entire signed URLs.
        page.on("pageerror", lambda _: self.add("page_script_error"))

    async def observe_page(self, page, *, official_page: bool = False):
        if page is None or self.closed:
            return
        state, code = "unknown", ""
        try:
            async with asyncio.timeout(1):
                for frame in page.frames[:5]:
                    try:
                        selector = "body"
                        if official_page and not re.search(r"punish|captcha|validate", urlsplit(frame.url).path, re.I):
                            # Do not inspect message-list text or mistake a chat
                            # mentioning verification for an actual page error.
                            selector = ".nc-container, .nc_wrapper"
                        locator = frame.locator(selector)
                        if selector != "body":
                            locator = locator.first
                        text = (await locator.inner_text(timeout=200))[:32768]
                    except Exception:
                        continue
                    if re.search(r"验证失败|请重试", text):
                        state = "page_reports_failure"
                        match = re.search(r"\berror\s*[:：]\s*([A-Za-z0-9]{2,16})(?=[\s)）]|$)", text, re.I)
                        if match:
                            code = match[1]
                        break
                    if re.search(r"验证通过|验证成功", text):
                        state = "page_reports_success"
        except TimeoutError:
            return
        if (state, code) != (self.page_state, self.page_error_code):
            self.page_state, self.page_error_code = state, code
            detail = f"page={state} error_code={code or 'none'}"
            if state == "page_reports_failure":
                self.last_page_failure = detail
            self.add(detail)

    async def close(self):
        self.closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
