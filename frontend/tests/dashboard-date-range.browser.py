"""Run with local Vite on :9998. APIs are mocked; no production data is read or changed."""
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright, expect
from common.utils.browser_utils import ensure_playwright_browser_path, get_chromium_executable_path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / 'tests/.dashboard-date-fixture.html'
ENTRY = ROOT / 'tests/.dashboard-date-fixture.tsx'

async def main():
    HTML.write_text('<html><body><div id="root"></div><script type="module" src="/tests/.dashboard-date-fixture.tsx"></script></body></html>')
    ENTRY.write_text('''import React from 'react'
import { createRoot } from 'react-dom/client'
import { Dashboard } from '/src/pages/dashboard/Dashboard'
import { useAuthStore } from '/src/store/authStore'
import '/src/styles/globals.css'
import '/src/styles/theme.css'
import '/src/styles/yutaixian.css'
useAuthStore.setState({ isAuthenticated: true, _hasHydrated: true, token: 'test-only', user: { id: 1, is_admin: true } })
createRoot(document.getElementById('root')!).render(<div style={{padding: 24}}><Dashboard /></div>)
''')
    ranges, trends, errors = [], [], []
    fail_next = False
    async def network(route):
        nonlocal fail_next
        url = urlsplit(route.request.url)
        if not url.path.startswith('/api/'):
            if url.hostname not in ('localhost', '127.0.0.1'):
                return await route.abort()
            return await route.continue_()
        result = {'success': True, 'data': {}}
        if url.path.endswith('/stats/today'):
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            ranges.append(query)
            if fail_next:
                fail_next = False
                return await route.fulfill(status=500, content_type='application/json', body='{}')
            result = dict(success=True, today_users=1, today_accounts=2, today_orders=3,
                          today_amount=123.45, today_shipped=2, today_pending=1)
        elif url.path.endswith('/order-trend'):
            trends.append(url.path)
            result['data'] = {'trend': [{'date': '11-01', 'amount': 10, 'count': 1}]}
        elif url.path.endswith('/cookies/stats'):
            result['data'] = dict(total_accounts=1, total_keywords=2, active_accounts=1, total_orders=3,
                                  today_reply_count=2, yesterday_reply_count=1, used_account_count=1)
        elif url.path.endswith('/admin/stats'):
            result = {key: 1 for key in ('total_users', 'total_cookies', 'active_cookies', 'online_cookies',
                       'password_configured', 'total_cards', 'total_keywords', 'total_orders', 'today_reply_count', 'yesterday_reply_count')}
        await route.fulfill(content_type='application/json', body=json.dumps(result))
    ensure_playwright_browser_path()
    try:
        async with async_playwright() as p:
            executable = get_chromium_executable_path()
            browser = await p.chromium.launch(headless=True, **({'executable_path': executable} if executable else {}))
            page = await browser.new_page(viewport={'width': 1450, 'height': 1100}, timezone_id='Asia/Shanghai')
            await page.route('**/*', network)
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto('http://127.0.0.1:9998/tests/.dashboard-date-fixture.html')
            refresh = page.get_by_role('button', name='刷新统计和金额趋势', exact=True)
            await expect(refresh).to_be_enabled()
            today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
            for label, start, end in [('今日', today, today), ('昨天', today-timedelta(days=1), today-timedelta(days=1)),
                                     ('前天', today-timedelta(days=2), today-timedelta(days=2)),
                                     ('近7天', today-timedelta(days=6), today), ('近30天', today-timedelta(days=29), today)]:
                await page.get_by_role('button', name=label, exact=True).click()
                await expect(refresh).to_be_enabled()
                assert ranges[-1] == {'start_date': str(start), 'end_date': str(end)}, ranges[-1]
                await expect(page.get_by_role('heading', name=f'{label}统计（管理员）', exact=True)).to_be_visible()
            await page.get_by_role('button', name='自定义', exact=True).click()
            await page.get_by_label('开始日期', exact=True).fill('2026-10-03')
            await page.get_by_label('结束日期', exact=True).fill('2026-10-16')
            await page.get_by_role('button', name='查询', exact=True).click()
            await expect(refresh).to_be_enabled()
            assert ranges[-1] == {'start_date': '2026-10-03', 'end_date': '2026-10-16'}
            count = len(ranges)
            await page.get_by_label('结束日期', exact=True).fill('2026-10-01')
            await page.get_by_role('button', name='查询', exact=True).click()
            await page.wait_for_timeout(100)
            assert len(ranges) == count
            trend_count = len(trends)
            await refresh.click()
            await expect(refresh).to_be_enabled()
            assert len(trends) == trend_count + 1
            assert ranges[-1] == {'start_date': '2026-10-03', 'end_date': '2026-10-16'}
            trend_count = len(trends)
            range_count = len(ranges)
            await page.get_by_role('button', name='刷新数据', exact=True).click()
            await expect(refresh).to_be_enabled()
            assert len(trends) == trend_count + 1
            assert len(ranges) == range_count + 1
            assert ranges[-1] == {'start_date': '2026-10-03', 'end_date': '2026-10-16'}
            fail_next = True
            await refresh.click()
            await expect(page.get_by_text('统计加载失败，请点击刷新重试', exact=True)).to_be_visible()
            await refresh.click()
            await expect(page.get_by_text('所选期间订单金额', exact=True)).to_be_visible()
            await page.get_by_role('button', name='近7天', exact=True).click()
            await expect(refresh).to_be_enabled()
            assert await page.get_by_role('combobox').count() == 0
            await page.wait_for_timeout(1200)
            await page.screenshot(path='/tmp/yutaixian-dashboard-date-range.png', full_page=True, animations='disabled')
            await page.set_viewport_size({'width': 390, 'height': 844})
            await expect(refresh).to_be_visible()
            await page.wait_for_timeout(300)
            overflow = await page.evaluate('Array.from(document.querySelectorAll("body *")).filter(el => el.getBoundingClientRect().right > innerWidth + 1).map(el => ({tag: el.tagName, cls: el.className, right: el.getBoundingClientRect().right})).slice(0, 15)')
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), overflow
            assert not errors, errors
            print('PASS: presets, custom dates, invalid date rejection, both refresh buttons update trend, error/retry, no account selector, mobile width, no runtime errors.')
            await browser.close()
    finally:
        HTML.unlink(missing_ok=True)
        ENTRY.unlink(missing_ok=True)

asyncio.run(main())
