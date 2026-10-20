"""Browser regression against the local Vite server; all APIs are simulated.

Run from repository root:
PYTHONPATH=. backend-web/.venv/bin/python frontend/tests/manual-verification-queue.browser.py
No live account, Cookie, third-party service, or CAPTCHA is used.
"""
import asyncio
import base64
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect
from common.utils.browser_utils import ensure_playwright_browser_path, get_chromium_executable_path

ROOT = Path(__file__).resolve().parents[2]
HTML = ROOT / 'frontend/tests/.manual-queue-fixture.html'
ENTRY = ROOT / 'frontend/tests/.manual-queue-fixture.tsx'

async def main():
    HTML.write_text('<html><body><div id="root"></div><script type="module" src="/tests/.manual-queue-fixture.tsx"></script></body></html>')
    ENTRY.write_text('''
import React from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter, Routes, Route, Link } from 'react-router-dom'
import { ManualVerificationQueue } from '/src/components/layout/ManualVerificationQueue'
import { useAuthStore } from '/src/store/authStore'
import '/src/styles/globals.css'
import '/src/styles/theme.css'
import '/src/styles/yutaixian.css'
useAuthStore.setState({ isAuthenticated: true, _hasHydrated: true, token: 'test-only' })
createRoot(document.getElementById('root')!).render(<React.StrictMode><MemoryRouter initialEntries={['/accounts']}>
<div style={{padding: 24}}><Link to="/items">商品测试页</Link><Routes>
<Route path="/accounts" element={<h1>账号测试页</h1>} />
<Route path="/risk-logs" element={<h1>风控日志测试页</h1>} />
<Route path="/items" element={<h1>商品测试页</h1>} />
</Routes></div><ManualVerificationQueue /></MemoryRouter></React.StrictMode>)
''')
    items = ['A', 'B', 'C', 'D', 'E']
    deadlines, clients = {}, {}
    deleted, checked, inputs, errors = [], [], [], []
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="560"><rect width="900" height="560" fill="#f1f5f9"/><text x="450" y="260" text-anchor="middle" font-size="24" fill="#475569">测试画面（不连接闲鱼）</text><rect x="80" y="340" width="740" height="50" rx="8" fill="#e2e8f0"/></svg>'
    image = 'data:image/svg+xml;base64,' + base64.b64encode(svg.encode()).decode()
    def session(key):
        return dict(session_id=key, account_id=key, status='success' if key in checked else 'ready',
                    message='验证通过，账号已自动启用，正在恢复连接' if key in checked else '测试队列等待人工操作',
                    width=900, height=560, expires_at=deadlines[key], image=image)

    async def api(route):
        request = route.request
        path = urlsplit(request.url).path
        data = {}
        if path.endswith('/queue') or path.endswith('/queue/claim'):
            key = items[0] if items else None
            data = {'pending_count': len(items), 'available': bool(key)}
            if key:
                deadlines.setdefault(key, time.time() + (4 if key == 'A' else 60))
                data.update(account_id=key, account_name='测试账号 ' + key, expires_at=deadlines[key])
                if path.endswith('/claim'):
                    client = request.post_data_json['client_id']
                    clients.setdefault(key, client)
                    assert clients[key] == client
                    data['session'] = session(key)
        elif request.method == 'DELETE':
            key = path.rsplit('/', 1)[-1]
            deleted.append(key)
            if key in items: items.remove(key)
        elif path.endswith('/check'):
            key = path.split('/')[-2]
            checked.append(key)
            data = session(key)
        elif path.endswith('/input'):
            inputs.extend(request.post_data_json['events'])
        else:
            key = path.rsplit('/', 1)[-1]
            data = session(key)
        await route.fulfill(status=200, content_type='application/json', body=json.dumps({'success': True, 'data': data}))

    ensure_playwright_browser_path()
    try:
        async with async_playwright() as playwright:
            executable = get_chromium_executable_path()
            browser = await playwright.chromium.launch(headless=True, **({'executable_path': executable} if executable else {}))
            context = await browser.new_context(viewport={'width': 1360, 'height': 1000})
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.route('**/api/v1/**', api)
            await page.goto('http://127.0.0.1:9999/tests/.manual-queue-fixture.html')
            await expect(page.get_by_role('dialog')).to_be_visible()
            await expect(page.get_by_role('heading', name='风控日志测试页')).to_have_count(1)
            await expect(page.get_by_role('dialog').get_by_text('测试账号 A', exact=False)).to_be_visible()
            await expect(page.get_by_role('button', name='打开闲鱼验证')).to_have_count(0)
            assert not deleted, 'StrictMode must not close a newly mounted session'
            await expect(page.get_by_role('dialog').get_by_text('测试账号 B', exact=False)).to_be_visible(timeout=10000)
            assert 'A' in deleted, 'Timed-out A must close before B is shown'
            assert checked == [], 'No user drag means no official verification check'
            await expect(page.get_by_role('dialog')).to_have_count(1)
            await expect(page.get_by_role('timer')).to_have_text(re.compile(r'倒计时 (60|59) 秒'))
            await page.get_by_role('button', name='关闭手动验证').click()
            await expect(page.get_by_role('dialog').get_by_text('测试账号 C', exact=False)).to_be_visible()
            assert 'B' in deleted, 'Manual close must release B before C is shown'
            await page.get_by_role('button', name='跳过此项').click()
            await expect(page.get_by_role('dialog').get_by_text('测试账号 D', exact=False)).to_be_visible()
            assert 'C' in deleted, 'Skip must advance to D'
            await page.keyboard.press('Escape')
            await expect(page.get_by_role('dialog').get_by_text('测试账号 E', exact=False)).to_be_visible()
            assert 'D' in deleted, 'Escape must advance to E'
            assert checked == [], 'Closing or timing out must never check/re-enable an account'
            # Change route while the global dialog is mounted (navigation itself is local).
            await page.get_by_role('link', name='商品测试页', exact=True).click(force=True)
            await expect(page.get_by_role('dialog')).to_be_visible()
            await page.screenshot(path='/tmp/yutaixian-manual-verification-queue.png')
            frame = page.get_by_alt_text('可手动操作的闲鱼验证页面')
            box = await frame.bounding_box()
            await page.mouse.move(box['x'] + 100, box['y'] + 300)
            await page.mouse.down()
            await page.mouse.move(box['x'] + 220, box['y'] + 300, steps=6)
            await page.mouse.up()
            await expect(page.get_by_text('验证通过，账号已自动启用，正在恢复连接', exact=True)).to_be_visible(timeout=6000)
            assert checked == ['E'], checked
            assert [e['kind'] for e in inputs][0] == 'down'
            assert [e['kind'] for e in inputs][-1] == 'up'
            await expect(page.get_by_role('dialog')).to_have_count(0, timeout=6000)
            assert 'E' in deleted
            assert errors == [], errors
            print('PASS: auto-navigation, auto-open, StrictMode, countdown, timeout/close/skip/Esc→next, route persistence, drag→success→close')
            print('Screenshot: /tmp/yutaixian-manual-verification-queue.png')
            await browser.close()
    finally:
        HTML.unlink(missing_ok=True)
        ENTRY.unlink(missing_ok=True)

asyncio.run(main())
