"""Single-account onboarding, identity, automatic credential adoption and browser reset."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import aiohttp
from app.services.account_browser import AccountBrowserService, PRIMARY, PENDING
from utils.account_browser.tokens import BrowserToken, TokenStore
from utils import risk_control


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconcile_failures_back_off_and_success_resets_delay(self):
        service = AccountBrowserService(MagicMock(), MagicMock())
        service.reconcile = AsyncMock(side_effect=[RuntimeError('network missing')] * 7 + [None])
        delays = []
        async def sleep(delay):
            delays.append(delay)
            if len(delays) == 8:
                raise asyncio.CancelledError()
        with patch('app.services.account_browser.asyncio.sleep', sleep):
            with self.assertRaises(asyncio.CancelledError):
                await service.run()
        self.assertEqual(delays, [4, 8, 16, 32, 60, 60, 60, 2])

    def setUp(self):
        self.store = TokenStore()
        p = patch('app.services.account_browser.tokens', self.store)
        p.start(); self.addCleanup(p.stop)
        p = patch('utils.account_browser.credentials.tokens', self.store)
        p.start(); self.addCleanup(p.stop)
        p = patch('utils.risk_control.registry', risk_control.RiskControlRegistry())
        p.start(); self.addCleanup(p.stop)
        self.records = {'123': 'unb=123; cookie2=original'}
        self.db = MagicMock()
        self.db.get_system_setting.return_value = None
        self.db.get_all_cookies.side_effect = lambda: dict(self.records)
        self.db.get_cookie_details.side_effect = lambda key: {'value':self.records[key],'user_id':7} if key in self.records else None
        self.db.get_cookie_status.return_value = True
        def save(key, cookie_value):
            self.records[key] = cookie_value
            return True
        self.db.update_cookie_account_info.side_effect = save
        def create(key, cookies, owner):
            if self.records: raise ValueError('已有账号')
            self.records[key] = cookies
        self.db.create_browser_account.side_effect = create
        self.manager = SimpleNamespace(pause_cookie_task=AsyncMock(), resume_cookie_task=AsyncMock(), instances={})
        self.browser = SimpleNamespace(alive=True, owner_id=7, desktop=SimpleNamespace(
            desktop_url='http://localhost:3000/', auth=aiohttp.BasicAuth('account','secret')))
        self.lock = asyncio.Lock()
        self.browsers = SimpleNamespace(lock=lambda key:self.lock, ensure=AsyncMock(return_value=self.browser),
            stop=AsyncMock(), discover=AsyncMock(), browsers={PRIMARY:self.browser}, pool=SimpleNamespace(clear_profile=AsyncMock()))
        self.service = AccountBrowserService(self.db, lambda:self.manager, self.browsers)
        def listener(key, expected, browser, callback):
            return SimpleNamespace(account_id=key, expected_account=expected, browser=browser,
                current_account=AsyncMock(return_value='123'), start=AsyncMock(), close=AsyncMock(), close_goofish_pages=AsyncMock(), open_messages=AsyncMock(),
                context=object(), task=SimpleNamespace(done=lambda:False), ready=SimpleNamespace(is_set=lambda:True), error=None)
        p=patch('app.services.account_browser.TokenListener',side_effect=listener)
        p.start();self.addCleanup(p.stop)

    async def test_stop_preserves_adopted_token_and_never_restarts_from_reconcile(self):
        settings = {}
        self.db.get_system_setting.side_effect = settings.get
        def save_setting(key, value):
            settings[key] = value
            return True
        self.db.set_system_setting.side_effect = save_setting
        await self.service.takeover('123', 7)
        value = self.capture()
        await self.service.capture_credentials(value)
        self.manager.pause_cookie_task.reset_mock()
        self.manager.resume_cookie_task.reset_mock()
        self.browsers.ensure.reset_mock()
        async def stop(key):
            self.browsers.browsers.pop(key, None)
        self.browsers.stop.side_effect = stop
        result = await self.service.stop_browser(7)
        self.assertFalse(result['browser_running'])
        self.assertIs(self.store.get('123'), value)
        for _ in range(3):
            await self.service.reconcile()
        self.browsers.ensure.assert_not_awaited()
        self.manager.pause_cookie_task.assert_not_awaited()
        self.manager.resume_cookie_task.assert_not_awaited()
        self.browsers.pool.clear_profile.assert_not_awaited()
        # The persisted choice survives service reconstruction.
        replacement = AccountBrowserService(self.db, lambda: self.manager, self.browsers)
        await replacement.reconcile()
        self.browsers.ensure.assert_not_awaited()
        # Expired/rejected credentials still pause messaging, without restarting Chrome.
        self.store.reject('123', value.token)
        await replacement.reconcile()
        self.manager.pause_cookie_task.assert_awaited_once()
        self.browsers.ensure.assert_not_awaited()

    async def test_start_after_stop_reuses_credentials_without_restarting_account(self):
        await self.service.takeover('123', 7)
        value = self.capture()
        await self.service.capture_credentials(value)
        await self.service.stop_browser(7)
        self.manager.pause_cookie_task.reset_mock()
        self.manager.resume_cookie_task.reset_mock()
        await self.service.takeover('123', 7)
        self.assertIs(self.store.get('123'), value)
        self.db.set_system_setting.assert_called_with('account_browser_stopped', 'false')
        self.manager.pause_cookie_task.assert_not_awaited()
        self.manager.resume_cookie_task.assert_not_awaited()

    async def test_other_user_cannot_stop_browser(self):
        with self.assertRaises(LookupError):
            await self.service.stop_browser(8)
        self.browsers.stop.assert_not_awaited()
        self.db.set_system_setting.assert_not_called()

    async def test_listener_status_and_reconnect_reuse_browser(self):
        await self.service.takeover('123',7)
        original = self.service.listener
        self.assertEqual((await self.service.status('123',7))['listener_state'], 'listening')
        original.task.done = lambda: True
        original.error = 'CDP disconnected'
        status = await self.service.status('123',7)
        self.assertEqual(status['listener_state'], 'disconnected')
        self.assertEqual(status['listener_error'], 'CDP disconnected')
        result = await self.service.reconnect(7)
        original.close.assert_awaited_once()
        self.assertIsNot(self.service.listener, original)
        self.assertEqual(result['listener_state'], 'listening')
        self.browsers.stop.assert_not_awaited()
        self.manager.pause_cookie_task.assert_awaited_once()

    async def test_configured_origin_used_by_desktop_and_setup(self):
        self.db.get_system_setting.return_value = 'https://browser.example.com:9443'
        self.assertEqual((await self.service.takeover('123',7))['desktop_url'], 'https://browser.example.com:9443/')
        self.records.clear()
        self.assertEqual((await self.service.setup(7))['desktop_url'], 'https://browser.example.com:9443/')
        self.db.get_system_setting.return_value = ''
        self.assertEqual(self.service.desktop_url(7), 'http://localhost:3000/')
        with self.assertRaises(LookupError): self.service.desktop_url(8)

    def capture(self, account='123'):
        value=BrowserToken(account,'website-token','website-device','website-app',f'unb={account}; cookie2=web')
        self.store.observe(self.service.listener.account_id if self.service.listener else account,value)
        return value

    async def test_open_reuses_primary_browser_and_pauses_once(self):
        result=await self.service.takeover('123',7)
        await self.service.takeover('123',7)
        self.assertEqual(result['desktop_url'],'http://localhost:3000/')
        self.assertNotIn('password',result)
        self.manager.pause_cookie_task.assert_awaited_once_with('123')
        self.browsers.ensure.assert_awaited_with(PRIMARY,7)

    async def test_owner_and_disabled_account_rejected(self):
        with self.assertRaises(LookupError): await self.service.takeover('123',8)
        self.db.get_cookie_status.return_value=False
        with self.assertRaises(ValueError): await self.service.takeover('123',7)
        self.manager.pause_cookie_task.assert_not_awaited()

    async def test_missing_token_keeps_task_paused(self):
        await self.service.takeover('123',7)
        self.assertEqual((await self.service.status('123',7))['credentials_state'], 'waiting')
        self.db.update_cookie_account_info.assert_not_called()
        self.manager.resume_cookie_task.assert_not_awaited()

    async def test_capture_automatically_adopts_exact_browser_token(self):
        await self.service.takeover('123',7)
        value=self.capture()
        await self.service.capture_credentials(value)
        self.assertIs(self.store.get('123'),value)
        self.manager.resume_cookie_task.assert_awaited_once_with('123',value.cookies)

    async def test_saved_credentials_close_tabs_before_resume_and_wait_for_auth(self):
        await self.service.takeover('123', 7)
        value = self.capture()
        async def close_tabs():
            self.assertEqual(self.records['123'], value.cookies)
            self.assertIs(self.store.get('123'), value)
            self.manager.resume_cookie_task.assert_not_awaited()
        self.service.listener.close_goofish_pages.side_effect = close_tabs
        await self.service.capture_credentials(value)
        self.service.listener.close_goofish_pages.assert_awaited_once()
        self.assertEqual(self.service.credential_status('123')['credentials_state'], 'connecting')
        instance = SimpleNamespace(connection_state='connecting', ws=SimpleNamespace(closed=False), current_token=value.token)
        self.manager.instances['123'] = instance
        self.assertEqual(self.service.credential_status('123')['credentials_state'], 'connecting')
        instance.connection_state = 'connected'
        self.assertEqual(self.service.credential_status('123')['credentials_state'], 'active')
        instance.ws.closed = True
        self.assertEqual(self.service.credential_status('123')['credentials_state'], 'connecting')

    async def test_tab_close_failure_keeps_account_paused(self):
        await self.service.takeover('123', 7)
        self.service.listener.close_goofish_pages.side_effect = RuntimeError('tab close failed')
        await self.service.capture_credentials(self.capture())
        self.manager.resume_cookie_task.assert_not_awaited()
        self.assertIsNone(self.store.get('123'))
        self.assertEqual(self.service.credential_status('123')['credentials_state'], 'error')

    async def test_switching_account_after_capture_is_rejected(self):
        await self.service.takeover('123',7)
        value=self.capture()
        self.service.listener.current_account.return_value='999'
        await self.service.capture_credentials(value)
        self.assertEqual((await self.service.status('123',7))['credentials_state'], 'error')
        self.db.update_cookie_account_info.assert_not_called()

    async def test_first_login_creates_account_without_restarting_browser(self):
        self.records.clear()
        await self.service.setup(7)
        original=self.service.listener
        value=self.capture()
        await self.service.capture_credentials(value)
        self.db.create_browser_account.assert_called_once_with('123',value.cookies,7)
        self.assertIs(self.service.listener,original)
        self.assertEqual(original.account_id,'123')
        self.assertEqual(original.expected_account,'123')
        self.assertIs(self.store.get('123'),value)
        self.browsers.stop.assert_not_awaited()
        self.manager.resume_cookie_task.assert_awaited_once_with('123',value.cookies)
        self.assertEqual((await self.service.setup_status(7))['account_id'],'123')

    async def test_second_account_cannot_be_added_or_overwrite_existing(self):
        with self.assertRaises(ValueError): await self.service.setup(7)
        self.db.create_browser_account.assert_not_called()
        self.assertEqual(self.records['123'],'unb=123; cookie2=original')

    async def test_setup_owned_by_another_operator_is_rejected(self):
        self.records.clear()
        self.service.pending_owner=8
        with self.assertRaises(ValueError): await self.service.setup(7)

    async def test_onboarding_save_failure_does_not_resume(self):
        self.records.clear()
        await self.service.setup(7)
        self.db.create_browser_account.side_effect=RuntimeError('save failed')
        await self.service.capture_credentials(self.capture())
        self.assertEqual(self.service.setup_error,'save failed')
        self.manager.resume_cookie_task.assert_not_awaited()
        self.assertIsNone(self.store.get('123'))

    async def test_clear_revokes_credentials_preserves_account_and_requires_login(self):
        await self.service.takeover('123',7)
        value=self.capture();self.store.approve('123',value)
        listener=self.service.listener
        result=await self.service.clear(7)
        listener.close.assert_awaited_once()
        self.browsers.pool.clear_profile.assert_awaited_once_with(7)
        self.assertEqual(self.records,{'123':'unb=123'})
        self.assertIsNone(self.store.get('123'))
        self.assertIn('123',self.service.manual)
        self.manager.resume_cookie_task.assert_not_awaited()
        self.assertIn('清空',result['message'])

    async def test_clear_wrong_owner_never_changes_data(self):
        with self.assertRaises(LookupError): await self.service.clear(8)
        self.db.update_cookie_account_info.assert_not_called()
        self.browsers.pool.clear_profile.assert_not_awaited()

    async def test_clear_without_account_returns_clean_onboarding_browser(self):
        self.records.clear()
        await self.service.clear(7)
        self.assertEqual(self.service.pending_owner,7)
        self.assertEqual(self.service.listener.account_id,PENDING)
        self.browsers.pool.clear_profile.assert_awaited_once_with(7)

    async def test_duplicate_capture_only_resumes_once(self):
        await self.service.takeover('123',7)
        value=self.capture()
        await self.service.capture_credentials(value)
        await self.service.capture_credentials(value)
        self.manager.resume_cookie_task.assert_awaited_once()
        self.assertEqual((await self.service.status('123',7))['credentials_state'], 'connecting')

    async def test_save_failure_remains_blocked_and_same_capture_can_retry(self):
        await self.service.takeover('123',7)
        value=self.capture()
        save=self.db.update_cookie_account_info.side_effect
        self.db.update_cookie_account_info.side_effect=RuntimeError('save failed')
        await self.service.capture_credentials(value)
        self.assertIsNone(self.store.get('123'))
        self.assertTrue(risk_control.registry.get('123').verification_required)
        self.manager.resume_cookie_task.assert_not_awaited()
        self.db.update_cookie_account_info.side_effect=save
        await self.service.capture_credentials(value)
        self.assertIs(self.store.get('123'),value)
        self.assertFalse(risk_control.registry.get('123').verification_required)
        self.manager.resume_cookie_task.assert_awaited_once()

    async def test_start_failure_revokes_and_cancels_account_again(self):
        await self.service.takeover('123',7)
        self.manager.resume_cookie_task.side_effect=RuntimeError('start failed')
        await self.service.capture_credentials(self.capture())
        self.assertIsNone(self.store.get('123'))
        self.assertEqual((await self.service.status('123',7))['credentials_state'], 'error')
        self.assertGreaterEqual(self.manager.pause_cookie_task.await_count,2)

    async def test_open_healthy_account_does_not_pause_and_expiry_does(self):
        value=self.capture(); self.store.approve('123',value)
        await self.service.takeover('123',7)
        self.manager.pause_cookie_task.assert_not_awaited()
        self.store.reject('123',value.token)
        await self.service.reconcile()
        self.manager.pause_cookie_task.assert_awaited_once()
        self.assertEqual((await self.service.status('123',7))['credentials_state'], 'waiting')
        fresh=BrowserToken('123','fresh-token','device','app','unb=123; cookie2=fresh')
        self.store.observe('123',fresh)
        await self.service.capture_credentials(fresh)
        self.assertIs(self.store.get('123'),fresh)
        self.manager.resume_cookie_task.assert_awaited_once_with('123',fresh.cookies)

    async def test_disabled_account_never_resumes_from_capture(self):
        await self.service.takeover('123',7)
        self.db.get_cookie_status.return_value=False
        await self.service.capture_credentials(self.capture())
        self.manager.resume_cookie_task.assert_not_awaited()
        self.db.update_cookie_account_info.assert_not_called()

    async def test_website_response_creates_then_renews_account_in_same_listener(self):
        import json
        from urllib.parse import urlencode
        from app.config import API_ENDPOINTS
        from utils.account_browser.listener import TokenListener
        from utils.account_browser.credentials import invalidate_credentials
        self.records.clear()
        self.service.pending_owner=7
        listener=TokenListener(PENDING,None,self.browser,self.service.capture_credentials)
        listener.task=asyncio.current_task()
        listener.ready.set()
        listener.context=SimpleNamespace(pages=[], cookies=AsyncMock(return_value=[{'name':'unb','value':'123'},{'name':'cookie2','value':'website'}]))
        self.service.listener=listener
        response=SimpleNamespace(url=API_ENDPOINTS['token'], status=200,
            request=SimpleNamespace(post_data=urlencode({'data':json.dumps({'deviceId':'native','appKey':'app'})}),all_headers=AsyncMock(return_value={'cookie':'unb=123'})),
            json=AsyncMock(return_value={'data':{'accessToken':'first'}}))
        with patch('utils.account_browser.listener.tokens',self.store):
            await listener.consume(response)
            self.assertEqual(self.store.get('123').token,'first')
            invalidate_credentials('123','expired',self.store.get('123'))
            response.json.return_value={'data':{'accessToken':'second'}}
            await listener.consume(response)
            await listener.consume(response)
        self.assertIs(self.service.listener,listener)
        self.assertEqual(self.store.get('123').token,'second')
        self.assertEqual(self.manager.resume_cookie_task.await_count,2)
        self.assertEqual(self.records['123'],'unb=123; cookie2=website')

    async def test_cancelled_adoption_stays_blocked(self):
        await self.service.takeover('123',7)
        self.manager.resume_cookie_task.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.service.capture_credentials(self.capture())
        self.assertIsNone(self.store.get('123'))
        self.assertTrue(risk_control.registry.get('123').verification_required)

    async def test_fetch_starts_browser_pauses_then_uses_existing_capture_handoff(self):
        await self.service.ensure(7, '123', self.records['123'])
        async def open_messages():
            self.manager.pause_cookie_task.assert_awaited_once_with('123')
            self.db.set_system_setting.assert_called_with('account_browser_stopped', 'false')
            self.manager.resume_cookie_task.assert_not_awaited()
        self.service.listener.open_messages.side_effect = open_messages
        result = await self.service.fetch_credentials('123', 7)
        self.assertTrue(result['browser_running'])
        self.service.listener.open_messages.assert_awaited_once()
        value = self.capture()
        await self.service.capture_credentials(value)
        self.assertEqual(self.records['123'], value.cookies)
        self.assertIs(self.store.get('123'), value)
        self.service.listener.close_goofish_pages.assert_awaited_once()
        self.manager.resume_cookie_task.assert_awaited_once_with('123', value.cookies)

    async def test_fetch_keeps_failed_page_for_manual_handling(self):
        await self.service.ensure(7, '123', self.records['123'])
        self.service.listener.open_messages.side_effect = RuntimeError('login overlay')
        result = await self.service.fetch_credentials('123', 7)
        self.assertEqual(result['credentials_state'], 'error')
        self.assertIn('页面已保留', result['message'])
        self.service.listener.close_goofish_pages.assert_not_awaited()
        self.manager.resume_cookie_task.assert_not_awaited()
        self.assertFalse(self.service.opening_messages)
        self.service.listener.open_messages.side_effect = None
        await self.service.fetch_credentials('123', 7)
        self.assertNotIn('123', self.service.errors)

    async def test_fetch_rejects_wrong_owner_disabled_and_duplicate(self):
        with self.assertRaises(LookupError):
            await self.service.fetch_credentials('123', 8)
        self.db.get_cookie_status.return_value = False
        with self.assertRaises(ValueError):
            await self.service.fetch_credentials('123', 7)
        self.db.get_cookie_status.return_value = True
        self.service.opening_messages.add('123')
        with self.assertRaisesRegex(ValueError, '重复'):
            await self.service.fetch_credentials('123', 7)
        self.manager.pause_cookie_task.assert_not_awaited()
