"""Single-account onboarding, identity, explicit credential adoption and browser reset."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import aiohttp
from app.services.account_browser import AccountBrowserService, PRIMARY, PENDING
from utils.account_browser.tokens import BrowserToken, TokenStore


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = TokenStore()
        p = patch('app.services.account_browser.tokens', self.store)
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
        self.manager = SimpleNamespace(pause_cookie_task=AsyncMock(), resume_cookie_task=AsyncMock())
        self.browser = SimpleNamespace(alive=True, owner_id=7, desktop=SimpleNamespace(
            desktop_url='https://localhost:3001/', auth=aiohttp.BasicAuth('account','secret')))
        self.lock = asyncio.Lock()
        self.browsers = SimpleNamespace(lock=lambda key:self.lock, ensure=AsyncMock(return_value=self.browser),
            stop=AsyncMock(), discover=AsyncMock(), browsers={PRIMARY:self.browser}, pool=SimpleNamespace(clear_profile=AsyncMock()))
        self.service = AccountBrowserService(self.db, lambda:self.manager, self.browsers)
        def listener(key, expected, browser, callback):
            return SimpleNamespace(account_id=key, expected_account=expected, browser=browser,
                current_account=AsyncMock(return_value='123'), start=AsyncMock(), close=AsyncMock(),
                context=object(), task=SimpleNamespace(done=lambda:False), ready=SimpleNamespace(is_set=lambda:True), error=None)
        p=patch('app.services.account_browser.TokenListener',side_effect=listener)
        p.start();self.addCleanup(p.stop)

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
        self.assertEqual(self.service.desktop_url(7), 'https://localhost:3001/')
        with self.assertRaises(LookupError): self.service.desktop_url(8)

    def capture(self, account='123'):
        value=BrowserToken(account,'website-token','website-device','website-app',f'unb={account}; cookie2=web')
        self.store.observe(account,value)
        return value

    async def test_open_reuses_primary_browser_and_pauses_once(self):
        result=await self.service.takeover('123',7)
        await self.service.takeover('123',7)
        self.assertEqual(result['desktop_url'],'https://localhost:3001/')
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
        with self.assertRaisesRegex(ValueError,'尚未捕获'): await self.service.finish('123',7)
        self.db.update_cookie_account_info.assert_not_called()
        self.manager.resume_cookie_task.assert_not_awaited()

    async def test_confirm_adopts_exact_browser_token(self):
        await self.service.takeover('123',7)
        value=self.capture()
        await self.service.finish('123',7)
        self.assertIs(self.store.get('123'),value)
        self.manager.resume_cookie_task.assert_awaited_once_with('123',value.cookies)

    async def test_switching_account_after_capture_is_rejected(self):
        await self.service.takeover('123',7)
        self.capture()
        self.service.listener.current_account.return_value='999'
        with self.assertRaises(ValueError): await self.service.finish('123',7)
        self.db.update_cookie_account_info.assert_not_called()

    async def test_first_login_creates_account_without_restarting_browser(self):
        self.records.clear()
        await self.service.setup(7)
        original=self.service.listener
        value=self.capture()
        await self.service.capture_new_account(value)
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
        await self.service.capture_new_account(self.capture())
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
