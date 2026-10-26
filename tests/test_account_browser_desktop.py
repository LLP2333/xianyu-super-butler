"""Real Linux desktop integration: noVNC input, reconnect and profile persistence.

Run with RUN_DESKTOP_TESTS=1 in the Linux desktop test environment. All navigation
is redirected to a local fixture; no real account or platform request is used.
"""
import asyncio
import os
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Page, async_playwright
import uvicorn

from app.api_account_browser import create_account_browser_router
from app.services.account_browser import AccountBrowserService
from utils.account_browser.manager import BrowserManager

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('RUN_DESKTOP_TESTS') == '1', 'requires Linux Xvfb, x11vnc, openbox and Chromium')
class DesktopTests(unittest.IsolatedAsyncioTestCase):
    async def test_desktop_input_reconnect_isolation_and_persistence(self):
        temporary = tempfile.TemporaryDirectory()
        browsers = BrowserManager(Path(temporary.name))
        db = MagicMock()
        db.get_cookie_details.return_value = {'user_id': 7, 'value': 'unb=123; cookie2=seed'}
        db.get_cookie_status.return_value = True
        account_manager = SimpleNamespace(pause_cookie_task=AsyncMock(), resume_cookie_task=AsyncMock())
        service = AccountBrowserService(db, lambda: account_manager, browsers)
        app = FastAPI()
        app.include_router(create_account_browser_router(lambda: {'user_id': 7}, service))
        app.mount('/client', StaticFiles(directory=os.environ['ACCOUNT_BROWSER_UI_DIR'], html=True))

        @app.get('/fixture')
        async def fixture():
            return HTMLResponse('''<!doctype html><title>Account browser test</title>
              <body style="margin:0;background:#dbeafe;font:24px sans-serif">
              <h1>Persistent account browser</h1><input id="value" autofocus>
              <button id="clicked" onclick="this.textContent='Mouse works'">Click me</button>
              <script>localStorage.setItem('profile-test','preserved')</script></body>''')

        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        while not server.started:
            await asyncio.sleep(0.01)
        origin = f'http://127.0.0.1:{port}'
        original_goto = Page.goto

        async def local_goto(page, url, **kwargs):
            return await original_goto(page, origin + '/fixture' if url == 'https://www.goofish.com/' else url, **kwargs)

        try:
            with patch.object(Page, 'goto', local_goto):
                async with async_playwright() as playwright:
                    client_browser = await playwright.chromium.launch(headless=True, channel='chromium', args=['--no-sandbox'])
                    client = await client_browser.new_page(viewport={'width': 1360, 'height': 1000})
                    errors = []
                    client.on('pageerror', lambda error: errors.append(str(error)))
                    await client.goto(origin + '/client/tests/account-browser.html')
                    await client.get_by_role('button', name='打开测试账号').click()
                    await client.get_by_text('已连接远程桌面', exact=False).wait_for(timeout=45000)
                    remote = browsers.browsers['a']
                    remote_page = await remote.page()
                    # VNC keyboard events reach Chromium, including its address bar.
                    await client.locator('canvas').click(position={'x': 400, 'y': 300})
                    await client.keyboard.press('Control+l')
                    await client.keyboard.type(origin + '/fixture?keyboard=remote')
                    await client.keyboard.press('Enter')
                    await remote_page.wait_for_url('**/fixture?keyboard=remote')
                    await remote_page.wait_for_function("document.activeElement?.id === 'value'")
                    await client.keyboard.type('VNC keyboard works')
                    await remote_page.wait_for_function("document.querySelector('#value').value === 'VNC keyboard works'")
                    # Full desktop coordinates include the browser chrome, unlike page screenshot coordinates.
                    box = await remote_page.locator('#clicked').bounding_box()
                    geometry = await remote_page.evaluate('({x:screenX,y:screenY,chrome:outerHeight-innerHeight})')
                    canvas = await client.locator('canvas').bounding_box()
                    await client.mouse.click(canvas['x'] + (geometry['x'] + box['x'] + box['width']/2) * canvas['width']/1280,
                                             canvas['y'] + (geometry['y'] + geometry['chrome'] + box['y'] + box['height']/2) * canvas['height']/900)
                    await remote_page.wait_for_function("document.querySelector('#clicked').textContent === 'Mouse works'")
                    await remote.context.add_cookies([{'name': 'session-proof', 'value': 'kept', 'domain': '127.0.0.1', 'path': '/'}])
                    await client.get_by_role('button', name='关闭窗口').click()
                    self.assertIn('a', service.manual)
                    self.assertTrue(remote.alive)
                    await client.get_by_role('button', name='打开测试账号').click()
                    await client.get_by_text('已连接远程桌面', exact=False).wait_for()
                    self.assertIs(browsers.browsers['a'], remote)
                    self.assertEqual(await remote_page.locator('#value').input_value(), 'VNC keyboard works')
                    account_manager.pause_cookie_task.assert_awaited_once_with('a')
                    other = await browsers.ensure('b', 'unb=456', 7)
                    self.assertNotEqual(other.desktop.display, remote.desktop.display)
                    self.assertNotEqual(other.desktop.port, remote.desktop.port)
                    self.assertNotIn('session-proof', {c['name'] for c in await other.context.cookies()})
                    await client.screenshot(path=str(Path(os.environ.get('BROWSER_TEST_ARTIFACTS', '/tmp')) / 'account-browser-desktop.png'))
                    with patch('app.services.account_browser.request_manual_token', AsyncMock(return_value={'token': None, 'reason': 'MOCK_REJECT'})):
                        await client.get_by_role('button', name='确认登录并恢复').click()
                        await client.get_by_role('status').filter(has_text='MOCK_REJECT').wait_for()
                    self.assertIn('a', service.manual)
                    db.update_cookie_account_info.assert_not_called()
                    await client.get_by_role('button', name='重新连接').click()
                    await client.get_by_text('已连接远程桌面', exact=False).wait_for()
                    with patch('app.services.account_browser.request_manual_token', AsyncMock(return_value={'token': 'confirmed', 'cookies_str': 'unb=123; cookie2=seed'})):
                        await client.get_by_role('button', name='确认登录并恢复').click()
                        await client.get_by_role('status').filter(has_text='Token 已确认').wait_for()
                    self.assertNotIn('a', service.manual)
                    self.assertTrue(remote.alive)
                    account_manager.resume_cookie_task.assert_awaited_once_with('a', 'unb=123; cookie2=seed')
                    await client_browser.close()
                    await service.disconnect('a')
                    processes = list(remote.desktop.processes)
                    await browsers.stop('a')
                    self.assertTrue(all(process.returncode is not None for process in processes))
                    restarted = await browsers.ensure('a', 'unb=123; cookie2=seed', 7)
                    restored_page = await restarted.page()
                    self.assertEqual(await restored_page.evaluate("localStorage.getItem('profile-test')"), 'preserved')
                    cookies = {c['name']: c['value'] for c in await restarted.context.cookies()}
                    self.assertEqual(cookies['session-proof'], 'kept')
                    self.assertEqual(cookies['cookie2'], 'seed')
                    self.assertFalse(errors, errors)
        finally:
            await service.close()
            server.should_exit = True
            await task
            temporary.cleanup()


if __name__ == '__main__':
    unittest.main()
