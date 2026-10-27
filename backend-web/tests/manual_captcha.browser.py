"""Offline browser regression: only synthetic pages and a loopback Token endpoint.

PYTHONPATH=backend-web:. backend-web/.venv/bin/python backend-web/tests/manual_captcha.browser.py
No live account or real CAPTCHA is accessed or operated.
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).resolve().parents[2] / "yutaixian_data/local_dev/browsers"))

from app.api.routes.risk_control_logs import ManualCaptchaInput
from app.services.manual_captcha_service import ManualCaptchaManager, ManualSession, restrict_verification_request
from common.services.im_token_api import build_im_token_api_url


async def main(mode):
    received = []

    async def token_endpoint(reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        headers = dict(line.split(": ", 1) for line in head.split("\r\n")[1:] if ": " in line)
        await reader.readexactly(int(headers.get("Content-Length", "0")))
        received.append({key.lower(): value for key, value in headers.items()})
        body = json.dumps({"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "test-token"}}).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                     + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(token_endpoint, "127.0.0.1", 0)
    api_url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/token"
    manager = ManualCaptchaManager()
    active = ManualSession(id="test", operator_id=1, owner_id=1, account_pk=1, account_id="test-account",
                           original_cookie="", device_id="test-device", log_id=1, cookies={},
                           initial_url="https://taobao.com/punish?old-private-challenge=true",
                           interaction_mode=mode)
    visited = []

    async def fixture(request_route):
        url = urlsplit(request_route.request.url)
        visited.append(url.hostname)
        assert url.hostname in {"h5api.m.goofish.com", "ynuf.aliapp.org", "us.ynuf.aliapp.org"}
        if url.hostname in {"ynuf.aliapp.org", "us.ynuf.aliapp.org"}:
            await request_route.fulfill(content_type="application/javascript", body=
                                       "window.loadedDependencies=(window.loadedDependencies||0)+1;")
            return
        if url.path == "/_____tmd_____/slide":
            await request_route.fulfill(content_type="application/json", body=json.dumps({
                "result": {"code": 300}, "success": False, "token": "private-response-token",
            }))
        else:
            await request_route.fulfill(content_type="text/html; charset=utf-8", body="""
                <body style="margin:0"><div style="width:900px;height:560px;background:#eee">输入转发测试画面</div>
                <script src="https://ynuf.aliapp.org/service/um.js"></script>
                <script src="https://us.ynuf.aliapp.org/service/um.js"></script>
                <p id="result"></p><script>
                window.points=[];
                ['pointerdown','pointermove','pointerup'].forEach(kind=>document.addEventListener(kind,e=>{
                    window.points.push({kind,x:e.clientX,y:e.clientY,time:performance.now()});
                    if(kind==='pointerup') fetch('/_____tmd_____/slide?token=private-query-token').then(r=>r.json()).then(()=>{
                        document.querySelector('#result').textContent='验证失败，点击框体重试(error:L9gXis)';
                    });
                }));
                </script></body>
            """)

    async def route(request_route):
        # Exercise the same production request policy. Its allow branch is
        # fulfilled with synthetic content, never continued to the real network.
        async def continue_locally():
            await fixture(request_route)
        await restrict_verification_request(SimpleNamespace(
            request=request_route.request, continue_=continue_locally, abort=request_route.abort,
        ), active.diagnostics)

    async def load_fixture(session):
        # Registered last: all traffic goes through the policy adapter above.
        await session.context.route("**/*", route)
        await session.page.goto("https://h5api.m.goofish.com/test-fixture")
        session.status = "ready"

    manager._load_challenge = AsyncMock(side_effect=load_fixture)
    manager._load_official_page = AsyncMock(side_effect=load_fixture)
    manager._persist_diagnostics = AsyncMock()
    try:
        await manager._open(active)
        (manager._load_official_page if mode == "native" else manager._load_challenge).assert_awaited_once_with(active)
        assert active.status == "ready", active.status
        assert await active.page.evaluate("window.loadedDependencies") == 2, "Required scripts must execute through the real allowlist"
        assert "request_blocked" not in active.diagnostics.text()
        info = await active.page.evaluate("({ua:navigator.userAgent,platform:navigator.platform})")
        assert active.browser.version.split('.')[0] in info["ua"], info
        assert active.browser_user_agent == info["ua"]
        if info["platform"] == "MacIntel":
            assert "Macintosh" in info["ua"] and "Windows" not in info["ua"], info
        await active.context.add_cookies([{"name": "unb", "value": "test-account", "url": api_url}])
        with (patch("app.services.manual_captcha_service.build_im_token_api_url", return_value=api_url),
              patch("common.services.im_token_api.build_im_token_api_url", return_value=api_url)):
            result = await manager._probe(active)
        assert result.response_json["data"]["accessToken"] == "test-token"
        assert received[0]["user-agent"] == info["ua"], received[0]["user-agent"]
        assert not any(key.startswith("sec-ch-") for key in received[0])
        assert "unb=test-account" in received[0]["cookie"]
        if mode == "native":
            assert "headless=false" in active.diagnostics.text()
            # Untrusted DOM events must not satisfy the manual-activity gate.
            await active.page.evaluate("document.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true}))")
            assert active.interactions == 0
            await active.page.evaluate("window.points=[]")
            await manager.focus(active)
            # Browser input here targets only our synthetic fixture, never a CAPTCHA.
            await active.page.mouse.move(100, 100)
            await active.page.mouse.down()
            await asyncio.sleep(.08)
            await active.page.mouse.move(120, 100)
            await asyncio.sleep(.09)
            await active.page.mouse.up()
        else:
            await manager.input(active, 1, [
                ManualCaptchaInput(kind="down", x=100, y=100, time_ms=0),
                ManualCaptchaInput(kind="move", x=120, y=100, time_ms=80),
                ManualCaptchaInput(kind="up", x=140, y=100, time_ms=170),
            ])
        await active.page.locator("#result").filter(has_text="L9gXis").wait_for()
        points = await active.page.evaluate("window.points")
        down = next(p for p in points if p['kind'] == 'pointerdown')
        up = next(p for p in points if p['kind'] == 'pointerup')
        assert up['time'] - down['time'] >= 160, points
        snapshot = await manager.snapshot(active)
        assert ("image" in snapshot) == (mode == "embedded")
        assert active.interactions == 1 and not active.pressed
        assert "L9gXis" in snapshot["message"], snapshot["message"]
        await asyncio.gather(*list(active.diagnostics._tasks))
        diagnostic = active.diagnostics.text()
        assert "http=200" in diagnostic and "code=300" in diagnostic and "error_code=L9gXis" in diagnostic, diagnostic
        assert "response_codes endpoint=slide" in diagnostic, diagnostic
        assert "private" not in diagnostic and "token" not in diagnostic, diagnostic
        assert len(received) == 1, "Dragging and snapshots must never issue extra Token requests"
        assert set(visited) == {"h5api.m.goofish.com", "ynuf.aliapp.org", "us.ynuf.aliapp.org"}
        assert f"human_input mode={mode} event=up" in diagnostic, diagnostic
        print(f"PASS ({mode}): actual browser window/input observation, resource policy, shared cookies/UA, failure diagnostics without Token polling")
        print(json.dumps({"mode": mode, "browser": active.browser.version, "platform": info["platform"],
                          "captured_interval_ms": 170, "forwarded_interval_ms": round(up['time'] - down['time'])}))
    finally:
        await manager._close_browser(active)
        server.close()
        await server.wait_closed()


async def official_page_regression():
    manager = ManualCaptchaManager()
    active = ManualSession(id="official", operator_id=1, owner_id=1, account_pk=1,
                           account_id="fixture", original_cookie="unb=fixture", device_id="old-device",
                           log_id=1, cookies={"unb": "fixture"}, interaction_mode="native")
    count = 0
    token_url = build_im_token_api_url()

    async def fixture(route):
        nonlocal count
        if route.request.url == token_url:
            count += 1
            payload = ({"ret": ["FAIL_SYS_USER_VALIDATE"], "data": {}} if count == 1 else
                       {"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "fixture-token"}})
            await route.fulfill(content_type="application/json", body=json.dumps(payload), headers={
                "Access-Control-Allow-Origin": "https://www.goofish.com", "Access-Control-Allow-Credentials": "true",
            })
        elif route.request.url == "https://www.goofish.com/im":
            await route.fulfill(content_type="text/html", body="""
              <button id="fixture-login" onclick="token()">Complete synthetic login</button><p id="state"></p>
              <script>async function token(){
                const data=JSON.stringify({appKey:'444e9908a51d1cb236a27862abc769c9',deviceId:'official-fixture-device'});
                const r=await fetch(TOKEN_URL,{method:'POST',credentials:'include',
                  headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({data})});
                const v=await r.json();document.querySelector('#state').textContent=v.ret[0];
              } token();</script>
            """.replace("TOKEN_URL", json.dumps(token_url)))
        else:
            await route.abort()

    async def load_official(s):
        await s.context.route("**/*", fixture)
        await ManualCaptchaManager._load_official_page(manager, s)

    manager._load_official_page = AsyncMock(side_effect=load_official)
    manager._load_challenge = AsyncMock(side_effect=AssertionError("No standalone HTTP challenge in native mode"))
    manager._probe = AsyncMock(side_effect=AssertionError("No standalone Token checks in native mode"))
    manager._record_check = AsyncMock()
    manager._persist_diagnostics = AsyncMock()
    async def saved(s, token, message):
        assert token == "fixture-token" and s.device_id == "official-fixture-device"
        assert s.cookies["unb"] == "fixture"
        s.status = "success"
    manager._save_success = AsyncMock(side_effect=saved)
    try:
        await manager._open(active)
        await active.page.locator("#state").filter(has_text="FAIL_SYS_USER_VALIDATE").wait_for()
        await asyncio.gather(*list(active.official_observer._tasks))
        assert (await manager.snapshot(active))["status"] == "ready"
        manager._save_success.assert_not_awaited()
        # Only a synthetic fixture button is operated. No live CAPTCHA/network.
        await active.page.locator("#fixture-login").click()
        await active.page.locator("#state").filter(has_text="SUCCESS").wait_for()
        await asyncio.gather(*list(active.official_observer._tasks))
        assert (await manager.snapshot(active))["status"] == "success"
        manager._save_success.assert_awaited_once()
        manager._probe.assert_not_awaited()
        manager._load_challenge.assert_not_awaited()
        assert count == 2, "Only the fixture's own page requests should reach the endpoint"
        print("PASS (official page): page-origin Token observation, failed response stays paused, exact device preserved, no HTTP replay")
    finally:
        await manager._close_browser(active)


asyncio.run(main("embedded"))
if sys.platform in ("darwin", "win32") or os.environ.get("DISPLAY"):
    asyncio.run(main("native"))
    asyncio.run(official_page_regression())
