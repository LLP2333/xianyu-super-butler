"""Token confirmation and automatic-task cancellation regression tests."""
import asyncio
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from utils import manual_captcha

class AutomaticVerificationTests(unittest.IsolatedAsyncioTestCase):
    def test_generic_rate_limit_is_not_a_slider_challenge(self):
        tree = ast.parse(Path('XianyuAutoAsync.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'XianyuLive')
        node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_need_captcha_verification')
        env = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<verification classification>', 'exec'), env)
        detect = lambda payload: env['_need_captcha_verification'](None, payload)
        self.assertFalse(detect({'ret': ['FAIL_SYS_FLOW_LIMIT::请稍后重试']}))
        self.assertFalse(detect({'ret': ['RGV587_ERROR::哎哟喂,被挤爆啦']}))
        self.assertTrue(detect({'ret': ['OTHER', 'FAIL_SYS_USER_VALIDATE::验证']}))
        self.assertTrue(detect({'data': {'url': 'https://h5api.m.goofish.com/punish'}}))


    async def test_platform_challenge_waits_for_manual_without_launching_browser(self):
        from utils import risk_control
        tree = ast.parse(Path('XianyuAutoAsync.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'XianyuLive')
        node = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_require_manual_verification')
        env = {'logger': MagicMock()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<manual verification>', 'exec'), env)
        registry = risk_control.RiskControlRegistry()
        db = MagicMock()
        instance = SimpleNamespace(cookie_id='account', send_token_refresh_notification=AsyncMock())
        with patch('app.services.risk_notifications.schedule_manual_verification_notification') as notify, patch.object(risk_control, 'registry', registry), patch.dict('sys.modules', {
            'app.db_manager': SimpleNamespace(db_manager=db),
            'utils.xianyu_slider_stealth': None,
        }):
            await env['_require_manual_verification'](instance)
        self.assertTrue(registry.get('account').verification_required)
        self.assertTrue(registry.get('account').is_blocked)
        self.assertEqual(instance.last_token_refresh_status, 'manual_verification_required')
        db.add_risk_control_log.assert_called_once()
        notify.assert_called_once_with('account', registry.get('account').last_hit_reason)
        instance.send_token_refresh_notification.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
