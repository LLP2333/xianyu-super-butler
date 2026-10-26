"""Website-issued credentials clear stale relogin flags; waiting is not login expiry."""
import unittest
from unittest.mock import patch
from XianyuAutoAsync import XianyuLive
from utils.account_browser.tokens import BrowserToken, TokenStore
from utils import risk_control


class ReloginFlagLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.live = XianyuLive.__new__(XianyuLive)
        self.live.cookie_id = 'a'
        self.live.myid = '123'
        self.live.token_refresh_interval = 3600
        self.live.needs_relogin = False
        self.live.relogin_reason = ''
        self.store = TokenStore()
        p = patch('utils.account_browser.tokens.tokens', self.store)
        p.start()
        self.addCleanup(p.stop)
        p=patch('utils.account_browser.credentials.tokens', self.store)
        p.start(); self.addCleanup(p.stop)
        p=patch('utils.risk_control.registry', risk_control.RiskControlRegistry())
        p.start(); self.addCleanup(p.stop)

    async def test_waiting_does_not_claim_session_expired(self):
        self.assertIsNone(await self.live.refresh_token())
        self.assertFalse(self.live.needs_relogin)
        self.assertEqual(self.live.last_token_refresh_status, 'waiting_browser_token')

    async def test_approved_browser_credentials_clear_stale_relogin_state(self):
        self.live.needs_relogin = True
        self.live.relogin_reason = 'old login expired'
        value = BrowserToken('123', 'web-token', 'device', 'app', 'unb=123')
        self.store.observe('a', value)
        self.store.approve('a', value)
        await self.live.refresh_token()
        self.assertFalse(self.live.needs_relogin)
        self.assertEqual(self.live.relogin_reason, '')
