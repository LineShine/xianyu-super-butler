"""Real React views, simulated APIs only. Run with the backend-web venv and local Vite.

PYTHONPATH=. backend-web/.venv/bin/python frontend/tests/code-generation.browser.py
"""
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect
from common.utils.browser_utils import ensure_playwright_browser_path, get_chromium_executable_path

ROOT = Path(__file__).resolve().parents[2]
HTML = ROOT / 'frontend/tests/.code-generation-fixture.html'
ENTRY = ROOT / 'frontend/tests/.code-generation-fixture.tsx'


async def main():
    HTML.write_text('<html><body><div id="root"></div><script type="module" src="/tests/.code-generation-fixture.tsx"></script></body></html>')
    ENTRY.write_text('''
import React from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter, Routes, Route, Link } from 'react-router-dom'
import { Items } from '/src/pages/items/Items'
import { ChatNew } from '/src/pages/chat-new/ChatNew'
import { AutoReplyLogs } from '/src/pages/autoReplyLogs/AutoReplyLogs'
import { useAuthStore } from '/src/store/authStore'
import '/src/styles/globals.css'
import '/src/styles/theme.css'
import '/src/styles/yutaixian.css'
useAuthStore.setState({ isAuthenticated: true, _hasHydrated: true, token: 'test-only' })
createRoot(document.getElementById('root')!).render(<MemoryRouter><div style={{padding: 24}}>
<nav><Link to="/">商品</Link> | <Link to="/chat">测试聊天</Link> | <Link to="/logs">测试回复日志</Link></nav>
<Routes><Route path="/" element={<Items />} /><Route path="/chat" element={<ChatNew />} /><Route path="/logs" element={<AutoReplyLogs />} /></Routes>
</div></MemoryRouter>)
''')
    enabled, fail_next, writes, sends, errors = False, False, [], [], []
    account = dict(account_id='1001', display_name='测试卖家', remark='测试卖家', connected=True, status='active')

    async def api(route):
        nonlocal enabled, fail_next
        request = route.request
        path = urlsplit(request.url).path
        result = {'success': True, 'data': {}}
        if path == '/api/v1/items/paginated':
            result = dict(success=True, data=[dict(id=1, cookie_id='1001', item_id='3001', item_title='口令功能测试商品',
                                                  code_generation_enabled=enabled)], total=1, page=1, page_size=20, total_pages=1)
        elif path.endswith('/code-generation'):
            assert path == '/api/v1/items/1001/3001/code-generation'
            assert request.method == 'PUT'
            writes.append(request.post_data_json)
            if fail_next:
                fail_next = False
                result = {'success': False, 'message': '模拟保存失败'}
            else:
                enabled = request.post_data_json['enabled']
                result['data'] = {'code_generation_enabled': enabled}
        elif path == '/api/v1/cookies/options':
            result = [{'pk': 1, 'id': '1001', 'enabled': True, 'remark': '测试卖家'}]
        elif path == '/api/v1/auto-reply-logs':
            result = dict(success=True, total=1, total_pages=1, data=[dict(
                id=1, owner_id=1, account_id='1001', chat_id='4001', sender_user_id='2001',
                sender_user_name='测试买家', item_id='3001', order_no='3316963513050017696',
                reply_strategy='code_generation', matched_rule_type='code_generation', reply_mode='text',
                process_status='failed', decision_reason='code_send_unknown', send_status='unknown',
                reply_text='口令生成\naB09zZ', error_message='未收到闲鱼明确确认',
                code_generation=dict(entry_source='buyer_auto', step='finished', quota_used=2,
                    quota_limit=3, quota_day='2026-10-26', quota_state='unknown'))])
        elif path == '/api/v1/chat-new/accounts':
            result = dict(success=True, data=[account], total=1, hasMore=False)
        elif path.endswith('/connected-accounts'):
            result['data'] = {'account_ids': []}
        elif '/conversations/' in path:
            result['data'] = dict(conversations=[dict(cid='4001', rawCid='4001@goofish', otherUserId='2001',
                otherUserName='测试买家', otherUserAvatar='', itemTitle='口令功能测试商品', lastMessageSummary='你好',
                lastMessageTime=1792900000000, unreadCount=0)], hasMore=False, nextCursor=None)
        elif '/messages/' in path:
            result['data'] = {'messages': [], 'hasMore': False, 'nextCursor': None}
        elif '/send-message/' in path:
            sends.append(request.post_data_json)
            result['data'] = {'messageId': f'test-{len(sends)}'}
        elif 'quick-phrases' in path or 'customer-orders' in path:
            result['data'] = []
        elif 'official-blacklist' in path:
            result['data'] = {'blocked': False}
        await route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    async def network(route):
        url = urlsplit(route.request.url)
        if url.path.startswith('/api/'):
            await api(route)
        elif url.hostname not in ('127.0.0.1', 'localhost'):
            await route.abort()
        else:
            await route.continue_()

    ensure_playwright_browser_path()
    try:
        async with async_playwright() as playwright:
            executable = get_chromium_executable_path()
            browser = await playwright.chromium.launch(headless=True, **({'executable_path': executable} if executable else {}))
            context = await browser.new_context(viewport={'width': 1560, 'height': 1000})
            await context.route('**/*', network)
            # Block chat WebSockets at the browser boundary; this test never connects to IM.
            await context.route_web_socket('**/chat-new/**', lambda ws: None)
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto('http://127.0.0.1:9999/tests/.code-generation-fixture.html')
            button = page.get_by_role('button', name='口令生成 3001', exact=True)
            await expect(button).to_have_text('已关闭')
            await expect(button).to_have_attribute('title', re.compile('买家.*每单每天最多自动领取3次'))
            headers = await page.locator('thead th').all_text_contents()
            assert headers.index('口令生成') == headers.index('AI提示词') + 1
            await button.click()
            await expect(button).to_have_text('已开启')
            assert writes == [{'enabled': True}]
            await page.reload()
            await expect(button).to_have_text('已开启')
            fail_next = True
            await button.click()
            await expect(button).to_be_enabled()
            await expect(button).to_have_text('已开启')
            await button.click()
            await expect(button).to_have_text('已关闭')
            await page.get_by_role('link', name='测试聊天').click()
            await page.get_by_text('测试卖家', exact=True).first.click()
            await page.get_by_text('测试买家', exact=True).first.click()
            box = page.get_by_placeholder('输入消息...（Shift+Enter 换行，Enter 发送）')
            for value in (' 生成口令', '口令生成\n', '生成口令'):
                await box.fill(value)
                await page.get_by_role('button', name='发送', exact=True).click()
                await expect(box).to_have_value('')
                assert sends[-1] == {'cid': '4001', 'toUserId': '2001', 'text': value}, sends[-1]
            await page.get_by_role('link', name='测试回复日志').click()
            await expect(page.get_by_text('口令生成 · 买家自助', exact=True)).to_be_visible()
            await expect(page.get_by_text('本次额度 2/3', exact=False)).to_be_visible()
            await expect(page.get_by_text('结果未知，保留占用', exact=False)).to_be_visible()
            await expect(page.get_by_text('3316963513050017696', exact=True)).to_be_visible()
            assert not errors, errors
            assert not enabled
            await browser.close()
            print('PASS: toggle and self-service help, persistence, raw Web text, self-service log and quota; all network mocked')
    finally:
        HTML.unlink(missing_ok=True)
        ENTRY.unlink(missing_ok=True)


asyncio.run(main())
