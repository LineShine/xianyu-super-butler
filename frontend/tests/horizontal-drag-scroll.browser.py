"""Real mouse interactions with the app's shared drag-scroll hook. Vite must run on :9998."""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright, expect
from common.utils.browser_utils import ensure_playwright_browser_path, get_chromium_executable_path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / 'tests/.horizontal-drag-fixture.html'
ENTRY = ROOT / 'tests/.horizontal-drag-fixture.tsx'

async def main():
    HTML.write_text('<html><meta name="viewport" content="width=device-width, initial-scale=1"><body><div id="root"></div><script type="module" src="/tests/.horizontal-drag-fixture.tsx"></script></body></html>')
    ENTRY.write_text('''import React, { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { useHorizontalDragScroll } from '/src/hooks/useHorizontalDragScroll'
import '/src/styles/globals.css'
function Fixture() {
  useHorizontalDragScroll()
  const [clicks, setClicks] = useState(0)
  const [nativeDrags, setNativeDrags] = useState(0)
  const [dynamic, setDynamic] = useState(false)
  return <div style={{padding: 20, height: 1800}}>
    <output id="clicks">{clicks}</output><output id="native-drags">{nativeDrags}</output>
    <div id="table" className="table-scroll" style={{width: 600, height: 240, flex: 'none', border: '1px solid black'}}>
      <div style={{width: 1500, height: 700, padding: 10}}>
        <button id="action" onClick={() => setClicks(v => v + 1)} style={{padding: 12}}>操作按钮</button>
        <a id="link" href="#clicked" onClick={() => setClicks(v => v + 1)} style={{padding: 12}}>可点击链接</a>
        <input id="editor" defaultValue="可编辑文本" style={{width: 140}} />
        <div id="sortable" draggable onDragStart={event => { setNativeDrags(v => v + 1); event.preventDefault() }} style={{padding: 10, width: 200}}>已有图片拖拽排序</div>
        <div id="opt-out" data-drag-scroll="ignore" style={{padding: 10, width: 200}}>自有拖拽交互</div>
        <div style={{padding: 30}}>表格内容：商品、账号、订单等列表</div>
      </div>
    </div>
    <div id="tabs" className="tabs-bar" style={{width: 600, marginTop: 20}}><div style={{minWidth: 1800}}><button id="tab" onClick={() => setClicks(v => v + 1)}>标签页</button></div></div>
    <div id="outer" className="overflow-x-auto" style={{width: 600, marginTop: 20}}><div style={{width: 1800}}>
      <div id="inner" className="overflow-x-auto" style={{width: 250, height: 60}}><div style={{width: 900, height: 60}}>嵌套横向列表</div></div>
    </div></div>
    <button id="add" onClick={() => setDynamic(true)}>动态新增列表</button>
    {dynamic && <div id="dynamic" className="table-ios-container" style={{width: 400, height: 60}}><div style={{width: 1200, height: 60}}>动态内容</div></div>}
  </div>
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><Fixture /></React.StrictMode>)
''')
    ensure_playwright_browser_path()
    try:
        async with async_playwright() as p:
            executable = get_chromium_executable_path()
            browser = await p.chromium.launch(headless=True, **({'executable_path': executable} if executable else {}))
            page = await browser.new_page(viewport={'width': 1000, 'height': 850})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            await page.goto('http://127.0.0.1:9998/tests/.horizontal-drag-fixture.html')
            table = page.locator('#table')
            async def state(selector='#table'):
                return await page.locator(selector).evaluate('(el) => ({x: el.scrollLeft, y: el.scrollTop})')
            async def reset():
                await table.evaluate('el => { el.scrollLeft = 0; el.scrollTop = 0 }')
            async def drag(locator, dx=-100, dy=0, button='left', release=True):
                await locator.scroll_into_view_if_needed()
                box = await locator.bounding_box()
                x, y = box['x'] + box['width']/2, box['y'] + box['height']/2
                await page.mouse.move(x, y)
                await page.mouse.down(button=button)
                await page.mouse.move(x+dx, y+dy, steps=10)
                if release:
                    await page.mouse.up(button=button)
            await expect(table).to_be_visible()
            # Horizontal direction stays locked even when later motion becomes mostly vertical.
            box = await table.bounding_box()
            await page.mouse.move(box['x']+400, box['y']+190)
            await page.mouse.down()
            await page.mouse.move(box['x']+280, box['y']+195, steps=8)
            await page.mouse.move(box['x']+200, box['y']+360, steps=8)
            assert await state() == {'x': 200, 'y': 0}, await state()
            await page.mouse.wheel(0, 160)
            await page.wait_for_timeout(100)
            assert (await state())['y'] == 0
            assert await page.evaluate('window.scrollY') == 0
            await page.mouse.up()
            assert not await page.locator('html').evaluate('el => el.classList.contains("horizontal-mouse-dragging")')
            await page.mouse.move(box['x']+400, box['y']+190)
            await page.mouse.wheel(0, 120)
            await page.wait_for_timeout(150)
            assert (await state())['y'] > 0
            await reset()
            # Initial vertical movement is never converted into a horizontal drag.
            await page.mouse.move(box['x']+400, box['y']+180)
            await page.mouse.down()
            await page.mouse.move(box['x']+402, box['y']+205, steps=3)
            await page.mouse.move(box['x']+200, box['y']+210, steps=8)
            await page.mouse.up()
            assert (await state())['x'] == 0
            # Clicks work; dragging a button/link never invokes its click action.
            await page.locator('#action').click()
            await expect(page.locator('#clicks')).to_have_text('1')
            await drag(page.locator('#action'), dx=-80)
            await expect(page.locator('#clicks')).to_have_text('1')
            assert (await state())['x'] > 0
            await reset()
            await drag(page.locator('#link'), dx=-80)
            await expect(page.locator('#clicks')).to_have_text('1')
            assert '#clicked' not in page.url
            await reset()
            await page.locator('#action').click()
            await expect(page.locator('#clicks')).to_have_text('2')
            await drag(page.locator('#editor'), dx=-60)
            assert (await state())['x'] == 0
            await page.locator('#editor').fill('正常输入')
            await drag(page.locator('#sortable'), dx=-80)
            await expect(page.locator('#native-drags')).to_have_text('1')
            assert (await state())['x'] == 0
            await drag(page.locator('#opt-out'), dx=-80)
            assert (await state())['x'] == 0
            await drag(table, dx=-80, button='right')
            assert (await state())['x'] == 0
            await page.keyboard.press('Escape')
            await drag(page.locator('#tabs'), dx=-100)
            assert (await state('#tabs'))['x'] > 0
            await drag(page.locator('#inner'), dx=-100)
            assert (await state('#inner'))['x'] > 0
            assert (await state('#outer'))['x'] == 0
            await page.locator('#add').click()
            await drag(page.locator('#dynamic'), dx=-100)
            assert (await state('#dynamic'))['x'] > 0
            # Cancellation cleans up capture/styles; the next click remains usable.
            await reset()
            await drag(table, dx=-100, release=False)
            await page.evaluate('window.dispatchEvent(new Event("blur"))')
            await page.mouse.up()
            assert not await page.locator('html').evaluate('el => el.classList.contains("horizontal-mouse-dragging")')
            await reset()
            await page.locator('#action').click()
            await expect(page.locator('#clicks')).to_have_text('3')
            # Touch is left to native scrolling, not captured by the mouse hook.
            await table.dispatch_event('pointerdown', {'pointerId': 99, 'pointerType': 'touch', 'button': 0, 'isPrimary': True, 'clientX': 400, 'clientY': 150})
            await table.dispatch_event('pointermove', {'pointerId': 99, 'pointerType': 'touch', 'buttons': 1, 'clientX': 200, 'clientY': 150})
            assert (await state())['x'] == 0
            assert not errors, errors
            print('PASS: mouse horizontal drag, axis lock, wheel lock/recovery, release outside, clicks, links, inputs, native sortable, opt-out, right button, tabs, nested/dynamic containers, cancellation, touch exclusion, StrictMode cleanup.')
            await browser.close()
    finally:
        HTML.unlink(missing_ok=True)
        ENTRY.unlink(missing_ok=True)

asyncio.run(main())
