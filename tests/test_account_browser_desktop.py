"""Opt-in integration: original LinuxServer Chrome sends its own fixture Token request."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import aiohttp
from aiohttp import web
from app.config import API_ENDPOINTS
from utils.account_browser.manager import AccountBrowser
from utils.account_browser.listener import TokenListener
from utils.account_browser.tokens import TokenStore


@unittest.skipUnless(os.environ.get('RUN_DESKTOP_TESTS') == '1', 'requires isolated original Chrome container')
class NativeTokenIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_real_browser_response_is_captured_without_extra_requests(self):
        calls = []
        origin = 'http://xianyu-native-test-runner:18766'
        async def page(request):
            response = web.Response(text='''<!doctype html><button id="request">网页请求 Token</button>
            <input id="input" placeholder="native keyboard test"><p id="result"></p>
            <script>document.querySelector('#request').onclick=async()=>{
            const r=await fetch('/token',{method:'POST',headers:{'content-type':'application/x-www-form-urlencoded'},
            body:new URLSearchParams({data:JSON.stringify({deviceId:'native-device',appKey:'native-app'})})});
            document.querySelector('#result').textContent=(await r.json()).data.accessToken;};</script>''', content_type='text/html')
            response.set_cookie('unb','123')
            return response
        async def token(request):
            calls.append(await request.post())
            return web.json_response({'data':{'accessToken':'fixture-website-token'}})
        app = web.Application()
        app.router.add_get('/', page)
        app.router.add_post('/token', token)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', 18766).start()
        desktop = SimpleNamespace(host='xianyu-native-observer-test',
                                  auth=aiohttp.BasicAuth('account','native-fixture-only-0123456789abcdef0123456789abcdef'))
        browser = AccountBrowser(desktop, 7)
        store = TokenStore()
        listener = TokenListener('fixture', '123', browser)
        try:
            with patch.dict(API_ENDPOINTS, {'token': origin+'/token'}), patch('utils.account_browser.listener.tokens', store):
                await listener.start()
                async with browser.connect() as context:
                    native = await context.new_page()
                    await native.goto(origin)
                    await native.locator('#input').fill('native keyboard')
                    await native.locator('#request').click()
                    await native.locator('#result').filter(has_text='fixture-website-token').wait_for()
                    for _ in range(50):
                        if store.get('fixture', candidate=True):
                            break
                        await asyncio.sleep(.1)
                    value = store.get('fixture', candidate=True)
                    self.assertIsNotNone(value)
                    self.assertEqual((value.token,value.device_id,value.app_key),
                                     ('fixture-website-token','native-device','native-app'))
                    self.assertEqual(len(calls),1)
                    self.assertIsNone(store.get('fixture'))
                    store.approve('fixture', value)
                    self.assertIs(store.get('fixture'),value)
                    # Logging out invalidates the captured token before another confirmation.
                    await context.clear_cookies()
                    await listener.current_account()
                    self.assertIsNone(store.get('fixture'))
                    await native.close()
                await listener.close()
        finally:
            await listener.close()
            await runner.cleanup()
