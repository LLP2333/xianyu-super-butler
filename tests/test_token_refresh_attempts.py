"""Real runtime consumes browser-issued Tokens without any HTTP refresh fallback."""
import json
import time
import unittest
from unittest.mock import AsyncMock, patch
import XianyuAutoAsync as runtime
from utils.account_browser.tokens import BrowserToken, TokenStore
from utils import risk_control


class TokenRefreshAttemptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = TokenStore()
        p = patch('utils.account_browser.tokens.tokens', self.store)
        p.start()
        self.addCleanup(p.stop)
        p = patch('utils.account_browser.credentials.tokens', self.store)
        p.start(); self.addCleanup(p.stop)
        p = patch('utils.risk_control.registry', risk_control.RiskControlRegistry())
        p.start(); self.addCleanup(p.stop)
        self.live = runtime.XianyuLive.__new__(runtime.XianyuLive)
        self.live.cookie_id = 'test'
        self.live.myid = '123'
        self.live.token_refresh_interval = 3600
        self.live.current_token = None
        self.live.session = AsyncMock()
        self.live.needs_relogin = False
        self.live.relogin_reason = ''

    def capture(self, account='123', age=0):
        value = BrowserToken(account, 'website-token', 'website-device', 'website-app', f'unb={account}', time.time()-age)
        self.store.observe('test', value)
        if age < 3600:
            self.store.approve('test', value)
        return value

    async def test_no_token_waits_without_http(self):
        self.assertIsNone(await self.live.refresh_token())
        self.assertEqual(self.live.last_token_refresh_status, 'waiting_browser_token')
        self.live.session.post.assert_not_called()

    async def test_uses_exact_token_device_and_app_without_extending_lifetime(self):
        value = self.capture(age=100)
        self.assertEqual(await self.live.refresh_token(), value.token)
        self.assertEqual(self.live.device_id, 'website-device')
        self.assertEqual(self.live.message_app_key, 'website-app')
        self.assertEqual(self.live.last_token_refresh_time, value.received_at)
        self.live.session.post.assert_not_called()

    async def test_wrong_account_and_expired_token_never_connect(self):
        self.capture('999')
        ws = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, '等待网页'):
            await self.live.init(ws)
        ws.send.assert_not_called()
        self.store.clear('test')
        self.capture(age=4000)
        self.assertIsNone(await self.live.refresh_token())
        self.live.session.post.assert_not_called()

    async def register(self, code):
        self.capture()
        frames = []
        ws = AsyncMock()
        async def send(frame):
            frames.append(json.loads(frame))
        async def recv():
            return json.dumps({'code':code,'headers':{'mid':frames[0]['headers']['mid']}})
        ws.send.side_effect = send
        ws.recv.side_effect = recv
        return ws, frames

    async def test_registration_waits_for_ack_and_uses_browser_parameters(self):
        ws, frames = await self.register(200)
        await self.live.init(ws)
        self.assertEqual(frames[0]['headers']['token'], 'website-token')
        self.assertEqual(frames[0]['headers']['did'], 'website-device')
        self.assertEqual(frames[0]['headers']['app-key'], 'website-app')
        self.assertEqual(frames[1]['lwp'], '/r/SyncStatus/ackDiff')
        ws.recv.assert_awaited_once()

    async def test_rejected_registration_invalidates_token_and_never_syncs(self):
        ws, frames = await self.register(401)
        with self.assertRaisesRegex(RuntimeError, '拒绝网页 Token'):
            await self.live.init(ws)
        self.assertEqual(len(frames), 1)
        self.assertIsNone(self.store.get('test'))
        self.assertIsNone(await self.live.refresh_token())
        self.live.session.post.assert_not_called()

    async def test_message_frames_stop_immediately_after_invalidation(self):
        value=self.capture()
        await self.live.refresh_token()
        ws=AsyncMock()
        await self.live._send_account_frame(ws,'first')
        from utils.account_browser.credentials import invalidate_credentials
        invalidate_credentials('test','expired',value)
        with self.assertRaises(risk_control.RiskControlBlocked):
            await self.live._send_account_frame(ws,'second')
        ws.send.assert_awaited_once_with('first')
