"""Real original nginx/Selkies: one-time entry, desktop session and private CDP."""
import asyncio
import os
import unittest
from types import SimpleNamespace
import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException
from starlette.responses import HTMLResponse
from playwright.async_api import async_playwright
from app.api_account_browser import create_account_browser_router

KEY = 'native-fixture-only-0123456789abcdef0123456789abcdef'

@unittest.skipUnless(os.environ.get('RUN_DESKTOP_TESTS') == '1', 'requires isolated native desktop')
class NativeDesktopAccess(unittest.IsolatedAsyncioTestCase):
    async def test_open_link_loads_original_selkies_without_password_prompt(self):
        origin='https://xianyu-native-observer-test:3001/'
        browser=SimpleNamespace(owner_id=7,desktop=SimpleNamespace(desktop_url=origin,auth=aiohttp.BasicAuth('account',KEY)))
        app=FastAPI()
        launch_url=''
        @app.get('/launch')
        def launch_page():
            return HTMLResponse(f'<a href="{launch_url}">Open desktop</a>')
        service=SimpleNamespace(desktop=lambda *_:browser)
        app.include_router(create_account_browser_router(lambda:{'user_id':7},service,
            lambda token: {'user_id':7} if token=='fixture-session' else None))
        server=uvicorn.Server(uvicorn.Config(app,host='0.0.0.0',port=18767,log_level='error',access_log=False))
        task=asyncio.create_task(server.serve())
        try:
            for _ in range(100):
                if server.started: break
                await asyncio.sleep(.05)
            self.assertTrue(server.started)
            async with aiohttp.ClientSession() as client:
                async with client.get(origin,ssl=False) as response:
                    self.assertEqual(response.status,403)
                    self.assertNotIn('WWW-Authenticate',response.headers)
                async with client.post('http://127.0.0.1:18767/api/account-browser/desktop-link',headers={'Authorization':'Bearer fixture-session'}) as response:
                    link=(await response.json())['open_url']
                    launch_url=link
                async with client.get('http://xianyu-native-observer-test:9223/json/version') as response:
                    self.assertEqual(response.status,401)
                async with client.get('http://xianyu-native-observer-test:9223/json/version',auth=aiohttp.BasicAuth('account',KEY)) as response:
                    self.assertEqual(response.status,200)
            async with async_playwright() as playwright:
                chromium=await playwright.chromium.launch(headless=True,args=['--no-sandbox'])
                context=await chromium.new_context(ignore_https_errors=True)
                page=await context.new_page()
                failures=[]
                page.on('response',lambda r: failures.append(r.status) if r.status in (401,403,500,502) else None)
                await page.goto('http://xianyu-native-test-runner:18767/launch')
                await page.get_by_role('link',name='Open desktop').click()
                await page.locator('#overlayInput').wait_for(timeout=45000)
                self.assertEqual(page.url,origin)
                self.assertFalse(failures,failures)
                cookies=await context.cookies()
                session=next(c for c in cookies if c['name']=='xianyu_desktop')
                self.assertTrue(session['httpOnly']);self.assertTrue(session['secure'])
                # One-time ticket cannot open a second independent client.
                async with aiohttp.ClientSession() as client:
                    async with client.get(link,ssl=False,allow_redirects=False) as response:
                        self.assertEqual(response.status,403)
                await chromium.close()
        finally:
            server.should_exit=True
            await asyncio.wait_for(task,10)
