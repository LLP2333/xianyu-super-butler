"""Ownership, takeover, credential commit and remote connection authorization."""
import asyncio
import ast
from pathlib import Path
from typing import Dict, Any
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI, HTTPException, Depends
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from app.api_account_browser import create_account_browser_router
from app.services.account_browser import AccountBrowserService


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = MagicMock()
        self.record = {'value': 'unb=123; cookie2=original', 'user_id': 7}
        self.db.get_cookie_details.return_value = self.record
        self.db.get_cookie_status.return_value = True
        self.db.get_all_cookies.return_value = {'a': self.record['value']}
        self.db.update_cookie_account_info.return_value = True
        self.account_manager = SimpleNamespace(pause_cookie_task=AsyncMock(), resume_cookie_task=AsyncMock(), release_browser_pause=AsyncMock())
        self.browser = SimpleNamespace(alive=True, database_cookie=self.record['value'],
                                       context=SimpleNamespace(add_cookies=AsyncMock()), checkpoint=AsyncMock())
        locks = {}
        self.browsers = SimpleNamespace(
            lock=lambda key: locks.setdefault(key, asyncio.Lock()),
            ensure=AsyncMock(return_value=self.browser), stop=AsyncMock(),
            browsers={'a': self.browser})
        self.service = AccountBrowserService(self.db, lambda: self.account_manager, self.browsers)
        self.service.cookies = AsyncMock(return_value='unb=123; x5sec=verified')

    async def test_reopen_reuses_browser_and_pause_once(self):
        await self.service.takeover('a', 7)
        await self.service.takeover('a', 7)
        self.account_manager.pause_cookie_task.assert_awaited_once_with('a')
        self.browsers.stop.assert_not_awaited()
        self.assertIn('a', self.service.manual)

    async def test_other_owner_cannot_open_browser(self):
        with self.assertRaises(LookupError):
            await self.service.takeover('a', 8)
        self.account_manager.pause_cookie_task.assert_not_awaited()
        self.browsers.ensure.assert_not_awaited()

    async def test_launch_failure_restores_original_task(self):
        self.browsers.ensure.side_effect = RuntimeError('missing desktop')
        with self.assertRaises(RuntimeError):
            await self.service.takeover('a', 7)
        self.account_manager.resume_cookie_task.assert_awaited_once_with('a', self.record['value'])
        self.assertNotIn('a', self.service.manual)

    async def test_token_failure_preserves_original_and_manual_browser(self):
        self.service.manual.add('a')
        with patch('app.services.account_browser.request_manual_token', AsyncMock(return_value={'token': None, 'reason': 'FAIL_SYS_USER_VALIDATE'})):
            with self.assertRaises(ValueError):
                await self.service.finish('a', 7)
        self.db.update_cookie_account_info.assert_not_called()
        self.account_manager.resume_cookie_task.assert_not_awaited()
        self.browsers.stop.assert_not_awaited()
        self.assertIn('a', self.service.manual)

    async def test_wrong_account_cannot_overwrite_credentials(self):
        self.service.manual.add('a')
        self.service.cookies.return_value = 'unb=other'
        with patch('app.services.account_browser.request_manual_token', AsyncMock()) as token:
            with self.assertRaises(ValueError):
                await self.service.finish('a', 7)
        token.assert_not_awaited()
        self.db.update_cookie_account_info.assert_not_called()

    async def test_success_commits_then_resumes_without_closing_browser(self):
        self.service.manual.add('a')
        with patch('app.services.account_browser.request_manual_token', AsyncMock(return_value={'token': 'confirmed', 'cookies_str': 'unb=123; x5sec=passed'})):
            await self.service.finish('a', 7)
        self.db.update_cookie_account_info.assert_called_once_with('a', cookie_value='unb=123; x5sec=passed')
        self.account_manager.resume_cookie_task.assert_awaited_once_with('a', 'unb=123; x5sec=passed')
        self.assertNotIn('a', self.service.manual)
        self.browsers.stop.assert_not_awaited()

    async def test_database_failure_keeps_manual_and_does_not_resume(self):
        self.service.manual.add('a')
        self.db.update_cookie_account_info.return_value = False
        with patch('app.services.account_browser.request_manual_token', AsyncMock(return_value={'token': 'confirmed', 'cookies_str': 'unb=123'})):
            with self.assertRaises(RuntimeError):
                await self.service.finish('a', 7)
        self.account_manager.resume_cookie_task.assert_not_awaited()
        self.assertIn('a', self.service.manual)

    async def test_release_uses_original_cookie_without_token_or_save(self):
        self.service.manual.add('a')
        await self.service.finish('a', 7, save=False)
        self.service.cookies.assert_not_awaited()
        self.db.update_cookie_account_info.assert_not_called()
        self.account_manager.resume_cookie_task.assert_awaited_once_with('a', self.record['value'])
        self.browsers.stop.assert_not_awaited()

    async def test_disabled_account_stops_desktop_and_clears_pause(self):
        self.service.manual.add('a')
        self.db.get_cookie_status.return_value = False
        await self.service.reconcile()
        self.browsers.stop.assert_awaited_once_with('a')
        self.account_manager.release_browser_pause.assert_awaited_once_with('a')
        self.assertNotIn('a', self.service.manual)

    async def test_cookie_rotation_never_overwrites_manual_browser(self):
        self.service.manual.add('a')
        self.browser.database_cookie = 'old'
        await self.service.reconcile()
        self.browser.context.add_cookies.assert_not_awaited()
        self.browser.checkpoint.assert_awaited_once()


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.user = {'user_id': 7, 'username': 'admin'}
        self.browser = SimpleNamespace(desktop=SimpleNamespace(port=5900))
        self.service = MagicMock()
        self.service.viewers = {}
        self.service.require_manual.return_value = ({}, self.browser)
        self.service.browsers.lock.side_effect = lambda _: asyncio.Lock()
        self.service.disconnect = AsyncMock()
        self.service.takeover = AsyncMock(return_value={'message': 'open'})
        app = FastAPI()

        def auth():
            if self.user is None:
                raise HTTPException(401)
            return self.user

        tree = ast.parse(Path('app/reply_server.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'require_admin')
        env = {'Depends': Depends, 'get_current_user': auth, 'HTTPException': HTTPException, 'Dict': Dict, 'Any': Any}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<admin dependency>', 'exec'), env)
        app.include_router(create_account_browser_router(env['require_admin'], self.service))
        self.client = TestClient(app)

    def test_http_requires_authentication(self):
        self.user = None
        self.assertEqual(self.client.post('/api/account-browser/a/open').status_code, 401)
        self.service.takeover.assert_not_awaited()

    def test_non_admin_cannot_open_or_obtain_ticket(self):
        self.user = {'user_id': 7, 'username': 'ordinary-user'}
        self.assertEqual(self.client.post('/api/account-browser/a/open').status_code, 403)
        self.assertEqual(self.client.post('/api/account-browser/a/ticket').status_code, 403)
        self.service.takeover.assert_not_awaited()

    def test_ticket_requires_account_ownership(self):
        self.service.require_manual.side_effect = LookupError('not owner')
        self.assertEqual(self.client.post('/api/account-browser/a/ticket').status_code, 404)

    def test_websocket_rejects_missing_ticket(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect('/api/account-browser/a/desktop'):
                pass

    def test_ticket_is_single_use_and_rechecks_owner(self):
        ticket = self.client.post('/api/account-browser/a/ticket').json()['ticket']

        async def relay(ws, port):
            await ws.send_bytes(b'RFB 003.008\n')
            await ws.receive_bytes()

        with patch('app.api_account_browser.relay_vnc', relay):
            with self.client.websocket_connect(f'/api/account-browser/a/desktop?ticket={ticket}') as ws:
                self.assertEqual(ws.receive_bytes(), b'RFB 003.008\n')
                ws.send_bytes(b'bye')
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect(f'/api/account-browser/a/desktop?ticket={ticket}'):
                    pass
        self.assertGreaterEqual(self.service.require_manual.call_count, 2)

    def test_ticket_cannot_be_used_for_another_account(self):
        ticket = self.client.post('/api/account-browser/a/ticket').json()['ticket']
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(f'/api/account-browser/b/desktop?ticket={ticket}'):
                pass

    def test_ticket_expires(self):
        ticket = self.client.post('/api/account-browser/a/ticket').json()['ticket']
        with patch('app.api_account_browser.time.monotonic', return_value=float('inf')):
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect(f'/api/account-browser/a/desktop?ticket={ticket}'):
                    pass


if __name__ == '__main__':
    unittest.main()
