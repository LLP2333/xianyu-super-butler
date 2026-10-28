import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import risk_notifications
from utils.risk_control import AccountGuard
from utils.account_browser.credentials import invalidate_credentials
from utils.account_browser.tokens import BrowserToken, TokenStore


class RiskNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = MagicMock()
        self.db.get_cookie_details.return_value = {'user_id': 7, 'nickname': '测试账号'}
        self.rule = dict(channel_id=1, enabled=True, channel_type='bark', channel_config={})
        self.db.get_account_notifications.return_value = [self.rule]
        self.sender = AsyncMock()
        for patcher in (
            patch.dict('sys.modules', {'app.db_manager': SimpleNamespace(db_manager=self.db)}),
            patch.object(risk_notifications, 'send_notification', self.sender),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def drain(self):
        if risk_notifications._pending:
            await asyncio.gather(*risk_notifications._pending)

    async def test_first_transition_notifies_once_and_verified_recovery_rearms(self):
        guard = AccountGuard('account')
        guard.require_verification('闲鱼要求验证')
        guard.require_verification('后续请求发现凭据不可用')
        guard.reset()
        guard.require_verification('重复检查')
        await self.drain()
        self.sender.assert_awaited_once()
        self.assertIn('闲鱼要求验证', self.sender.call_args.args[2])
        self.db.get_account_notifications.assert_called_once_with(
            'account', user_id=7, event_type='captcha_manual')
        guard.reset(verified=True)
        guard.require_verification('再次风控')
        await self.drain()
        self.assertEqual(self.sender.await_count, 2)

    async def test_invalidation_survives_account_task_cancellation(self):
        guard = AccountGuard('account')
        with patch('utils.account_browser.credentials.risk_control.registry') as registry:
            registry.get.return_value = guard
            with patch('utils.account_browser.credentials.tokens', TokenStore()):
                async def account_task():
                    invalidate_credentials('account', '平台要求人工验证', kind='risk')
                    asyncio.current_task().cancel()
                    await asyncio.sleep(0)
                task = asyncio.create_task(account_task())
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await self.drain()
        self.sender.assert_awaited_once()

    async def test_disabled_duplicate_and_failed_channels(self):
        self.db.get_account_notifications.return_value = [
            dict(self.rule, channel_id=3, enabled=False),
            dict(self.rule, channel_id=2), self.rule, self.rule,
        ]
        self.sender.side_effect = [RuntimeError('provider failure'), None]
        guard = AccountGuard('account')
        guard.require_verification('验证')
        await self.drain()
        self.assertTrue(guard.verification_required)
        self.assertEqual(self.sender.await_count, 2)

    async def test_expiry_and_renewal_do_not_send_risk_notification(self):
        guard = AccountGuard('account')
        guard.require_verification('消息 Token 已到期', 'expired')
        guard.require_verification('正在打开消息页', 'renewing')
        await self.drain()
        self.sender.assert_not_awaited()
        guard.require_verification('闲鱼要求验证')
        await self.drain()
        self.sender.assert_awaited_once()

    async def test_risk_is_not_downgraded_by_later_expiry(self):
        guard = AccountGuard('account')
        guard.require_verification('闲鱼要求验证')
        guard.require_verification('消息 Token 已到期', 'expired')
        self.assertEqual(guard.verification_kind, 'risk')
        self.assertEqual(guard.last_hit_reason, '闲鱼要求验证')

    async def test_renewal_failure_notification_uses_its_own_title(self):
        risk_notifications.schedule_manual_verification_notification(
            'account', '未捕获到新的消息 Token',
            risk_notifications.RENEWAL_TITLE, risk_notifications.RENEWAL_HINT)
        await self.drain()
        text = self.sender.call_args.args[2]
        self.assertTrue(text.startswith('凭据到期 · 自动续期失败'))
        self.assertNotIn('平台风控', text)

    async def test_unsubscribed_account_sends_nothing(self):
        self.db.get_account_notifications.return_value = []
        AccountGuard('account').require_verification('验证')
        await self.drain()
        self.sender.assert_not_awaited()

    async def test_late_rejection_does_not_notify_for_new_credentials(self):
        store = TokenStore()
        old = BrowserToken('123', 'old', 'device', 'app', 'unb=123', 1)
        new = BrowserToken('123', 'new', 'device', 'app', 'unb=123', 2)
        with patch('utils.account_browser.credentials.tokens', store), patch.object(
            risk_notifications, 'schedule_manual_verification_notification') as notify:
            with patch.object(store, 'get', return_value=new):
                invalidate_credentials('account', '旧请求失败', old)
            notify.assert_not_called()
