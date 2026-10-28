"""The real HTTP client must not send a second request after credential rejection."""
import asyncio
import time
import unittest
from unittest.mock import patch
import aiohttp
from aiohttp import web
from utils import risk_control
from utils.account_browser.tokens import BrowserToken, TokenStore
from utils.account_browser.credentials import platform_request, require_credentials, invalidate_credentials
from utils.xianyu_seller_api import XianyuSellerAPI
from app.secure_confirm import SecureConfirm
from app.secure_freeshipping import SecureFreeshipping


class CredentialGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = TokenStore()
        for target, value in [('utils.account_browser.credentials.tokens', self.store),
                              ('utils.risk_control.registry', risk_control.RiskControlRegistry())]:
            p=patch(target,value);p.start();self.addCleanup(p.stop)
        self.calls=0
        self.payload={'ret':['SUCCESS::调用成功'],'data':{}}
        self.delay=None
        self.arrived=asyncio.Event()
        async def handle(request):
            self.calls+=1
            self.arrived.set()
            if self.delay: await self.delay.wait()
            return web.json_response(self.payload)
        app=web.Application();app.router.add_post('/{path:.*}',handle)
        self.runner=web.AppRunner(app);await self.runner.setup()
        self.site=web.TCPSite(self.runner,'127.0.0.1',0);await self.site.start()
        self.url=f'http://127.0.0.1:{self.site._server.sockets[0].getsockname()[1]}/'
        self.session=aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close();await self.runner.cleanup()

    def adopt(self, token='new', age=0, cookies='unb=123; cookie2=browser'):
        value=BrowserToken('123',token,'device','app',cookies,time.time()-age)
        self.store.observe('acc',value);self.store.approve('acc',value)
        risk_control.registry.get('acc').reset(verified=True)
        return value

    async def call(self, cookies='unb=123; cookie2=browser'):
        async with platform_request(self.session,'acc','post',self.url,cookies=cookies) as response:
            return await response.json()

    async def test_absent_and_locally_expired_credentials_send_zero_requests(self):
        with self.assertRaises(risk_control.RiskControlBlocked): await self.call()
        self.adopt(cookies=f'unb=123; _m_h5_tk=expired_{int(time.time()*1000)-1000}')
        with self.assertRaises(risk_control.RiskControlBlocked): await self.call()
        self.assertEqual(self.calls,0)

    async def test_rejection_blocks_all_subsequent_calls_until_new_capture_adopted(self):
        first=self.adopt()
        self.payload={'ret':['FAIL_SYS_SESSION_EXPIRED::Session过期']}
        for _ in range(3):
            with self.assertRaises(risk_control.RiskControlBlocked): await self.call()
        self.assertEqual(self.calls,1)
        self.assertIsNone(self.store.get('acc'))
        self.store.clear('acc');self.store.observe('acc',first)
        self.assertIsNone(self.store.get('acc',candidate=True))
        self.adopt('fresh')
        self.payload={'ret':['SUCCESS::调用成功']}
        await self.call()
        self.assertEqual(self.calls,2)

    async def test_expiry_and_validation_are_classified_separately(self):
        self.adopt()
        self.payload={'ret':['FAIL_SYS_TOKEN_EXOIRED::令牌过期']}
        with self.assertRaises(risk_control.RiskControlBlocked): await self.call()
        self.assertEqual(risk_control.registry.get('acc').verification_kind,'expired')
        self.adopt('fresh')
        self.payload={'ret':['FAIL_SYS_USER_VALIDATE'],'data':{'url':'https://example/punish?x5'}}
        with self.assertRaises(risk_control.RiskControlBlocked): await self.call()
        self.assertEqual(risk_control.registry.get('acc').verification_kind,'risk')

    async def test_seller_expiry_no_retry_and_other_api_is_blocked(self):
        self.adopt()
        self.payload={'ret':['FAIL_SYS_TOKEN_EXOIRED::令牌过期']}
        seller=XianyuSellerAPI('acc','unb=123; cookie2=browser',self.session)
        seller.BASE_URL=self.url+'{api}/{version}/'
        with self.assertRaises(risk_control.RiskControlBlocked): await seller._call('fixture',{})
        with self.assertRaises(risk_control.RiskControlBlocked): await seller.get_order_detail('1')
        self.assertEqual(self.calls,1)

    async def test_late_old_response_does_not_revoke_new_credentials(self):
        self.adopt('old')
        self.payload={'ret':['FAIL_SYS_SESSION_EXPIRED']}
        self.delay=asyncio.Event()
        request=asyncio.create_task(self.call())
        await self.arrived.wait()
        fresh=self.adopt('fresh')
        self.delay.set()
        with self.assertRaises(risk_control.RiskControlBlocked): await request
        self.assertIs(self.store.get('acc'),fresh)
        self.assertFalse(risk_control.registry.get('acc').verification_required)

    async def test_stale_request_and_wrong_account_cookies_never_leave_process(self):
        self.adopt(cookies='unb=123; _m_h5_tk=current_9999999999999')
        for cookies in ['unb=999', 'unb=123; _m_h5_tk=old_9999999999999']:
            with self.assertRaises(risk_control.RiskControlBlocked): await self.call(cookies)
        self.assertEqual(self.calls,0)

    async def test_business_error_does_not_invalidate_credentials(self):
        value=self.adopt()
        self.payload={'ret':['FAIL_BIZ_COMMON_PARAM_NULL']}
        await self.call()
        self.assertIs(self.store.get('acc'),value)
        self.assertFalse(risk_control.registry.get('acc').verification_required)

    async def test_shipping_methods_stop_without_network_when_blocked(self):
        confirm=SecureConfirm(self.session,'unb=123','acc')
        free=SecureFreeshipping(self.session,'unb=123','acc')
        self.assertTrue((await confirm.auto_confirm('1','2'))['waiting_browser'])
        self.assertTrue((await free.auto_freeshipping('1','2','3'))['waiting_browser'])
        self.assertEqual(self.calls,0)
