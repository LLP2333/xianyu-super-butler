"""Manual input transport and Token confirmation, with no requests to real accounts."""
import asyncio
import ast
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import WebSocketDisconnect
from app.api_captcha_remote import websocket_endpoint
from utils import manual_captcha
from utils.captcha_remote_control import captcha_controller


class RemoteInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        captcha_controller.active_sessions.clear()
        captcha_controller.websocket_connections.clear()

    async def test_coalesces_moves_without_blocking_on_screenshots(self):
        session_id = str(uuid4())
        captcha_controller.active_sessions[session_id] = {
            'screenshot': '', 'clip': {'x': 90, 'y': 40}, 'viewport': {},
            'page': SimpleNamespace(mouse=SimpleNamespace(up=AsyncMock())),
        }
        events = [{'type': 'mouse_event', 'event_type': 'down', 'x': 100, 'y': 50}]
        events += [{'type': 'mouse_event', 'event_type': 'move', 'x': x, 'y': 50} for x in range(101, 201)]
        events += [{'type': 'mouse_event', 'event_type': 'up', 'x': 220, 'y': 50}]
        applied = []
        finished = asyncio.Event()

        async def apply(sid, kind, x, y):
            applied.append((kind, x, y))
            if kind == 'up':
                finished.set()
            return True

        async def receive():
            if events:
                return events.pop(0)
            await asyncio.wait_for(finished.wait(), 1)
            raise WebSocketDisconnect()

        ws = SimpleNamespace(accept=AsyncMock(), receive_json=receive, send_json=AsyncMock(), close=AsyncMock())
        with patch.object(captcha_controller, 'handle_mouse_event', side_effect=apply), patch.object(captcha_controller, 'update_screenshot', AsyncMock()) as screenshot:
            await websocket_endpoint(ws, session_id)
        self.assertEqual(applied, [('down', 100, 50), ('move', 200, 50), ('up', 220, 50)])
        screenshot.assert_not_awaited()
        self.assertEqual(ws.send_json.call_args_list[0].args[0]['origin'], {'x': 90, 'y': 40})

    async def test_release_uses_final_pointer_position(self):
        mouse = SimpleNamespace(move=AsyncMock(), up=AsyncMock())
        released = asyncio.Event()
        captcha_controller.active_sessions['s'] = {'page': SimpleNamespace(mouse=mouse), 'released': released}
        self.assertTrue(await captcha_controller.handle_mouse_event('s', 'up', 510, 390))
        mouse.move.assert_awaited_once_with(510, 390)
        mouse.up.assert_awaited_once()
        self.assertTrue(released.is_set())

    async def test_screenshot_producer_survives_connection_replacement(self):
        session = {'completed': False, 'clip': None}
        captcha_controller.active_sessions['s'] = session
        replacement = SimpleNamespace(send_json=AsyncMock(side_effect=lambda _: session.update(completed=True)))

        async def broken_send(message):
            captcha_controller.websocket_connections['s'] = replacement
            raise ConnectionError('disconnected')

        captcha_controller.websocket_connections['s'] = SimpleNamespace(send_json=broken_send)
        with patch.object(captcha_controller, 'update_screenshot', AsyncMock(return_value='frame')):
            await asyncio.wait_for(captcha_controller.auto_refresh_screenshot('s', interval=0), timeout=1)
        replacement.send_json.assert_awaited_once()


class SessionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def run_session(self, token_results, cancelled=False):
        page = SimpleNamespace(goto=AsyncMock())
        context = SimpleNamespace(add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page),
                                  cookies=AsyncMock(return_value=[{'name': '_m_h5_tk', 'value': 'signed_1'}, {'name': 'x5sec', 'value': 'passed'}]), close=AsyncMock())
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        playwright = SimpleNamespace(stop=AsyncMock())
        ws = SimpleNamespace(send_json=AsyncMock())

        async def create(sid, page):
            event = asyncio.Event()
            event.set()
            captcha_controller.active_sessions[sid] = {'released': event, 'cancelled': cancelled}
            captcha_controller.websocket_connections[sid] = ws

        async def push(message):
            if message['type'] == 'retry':
                captcha_controller.active_sessions['session']['released'].set()

        ws.send_json.side_effect = push
        initial = {'token': None, 'url': 'https://example.test/punish', 'cookies_str': '_m_h5_tk=signed_1', 'reason': 'FAIL_SYS_USER_VALIDATE'}
        with patch('playwright.async_api.async_playwright', return_value=SimpleNamespace(start=AsyncMock(return_value=playwright))), patch.object(manual_captcha.browser_limit, 'launch_browser', AsyncMock(return_value=browser)), patch.object(manual_captcha, 'request_manual_token', AsyncMock(side_effect=[initial, *token_results])) as request, patch.object(manual_captcha, '_wait_for_captcha_present', AsyncMock(return_value=True)), patch.object(captcha_controller, 'create_session', side_effect=create), patch.object(captcha_controller, 'auto_refresh_screenshot', AsyncMock()), patch.object(captcha_controller, 'check_completion', AsyncMock(return_value=True)), patch.object(manual_captcha.asyncio, 'sleep', AsyncMock()):
            result = await manual_captcha.open_manual_session('account', '_m_h5_tk=old_1', session_id='session')
        self.assertNotIn('session', captcha_controller.active_sessions)
        browser.close.assert_awaited_once()
        context.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()
        captcha_controller.websocket_connections.clear()
        return result, request

    async def test_user_cancel_closes_browser_without_confirming_or_returning_cookies(self):
        result, request = await self.run_session([], cancelled=True)
        self.assertFalse(result['success'])
        self.assertEqual(result['stage'], 'cancelled')
        self.assertEqual(result['cookies_str'], '_m_h5_tk=old_1')
        self.assertEqual(request.await_count, 1)

    async def test_cookie_collection_requires_token_confirmation(self):
        confirmed = {'token': 'confirmed', 'cookies_str': '_m_h5_tk=rotated_2; x5sec=passed'}
        result, request = await self.run_session([confirmed])
        self.assertTrue(result['success'])
        self.assertEqual(result['cookies_str'], confirmed['cookies_str'])
        self.assertIn('x5sec=passed', request.call_args.args[0])

    async def test_rejected_gesture_keeps_same_session_for_next_attempt(self):
        result, request = await self.run_session([
            {'token': None, 'reason': 'FAIL_SYS_USER_VALIDATE'},
            {'token': 'confirmed', 'cookies_str': 'confirmed-cookie'},
        ])
        self.assertTrue(result['success'])
        self.assertEqual(request.await_count, 3)

    async def test_token_transport_failure_does_not_return_success(self):
        result, _ = await self.run_session([manual_captcha.ManualVerificationError('token_confirm', 'Token 接口 HTTP 502')])
        self.assertFalse(result['success'])
        self.assertEqual(result['cookies_str'], '_m_h5_tk=old_1')
        self.assertEqual(result['stage'], 'token_confirm')
        self.assertIn('502', result['message'])


class RecoveryCommitTests(unittest.IsolatedAsyncioTestCase):
    def endpoint(self, db, manager):
        # Load the exact endpoint body without starting the production DB/server at import.
        from fastapi import Form, Depends, HTTPException
        from fastapi.responses import JSONResponse
        from typing import Dict, Any
        tree = ast.parse(Path('app/reply_server.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'start_manual_captcha')
        node.decorator_list = []
        env = dict(asyncio=asyncio, db_manager=db, cookie_manager=SimpleNamespace(manager=manager),
                   Form=Form, Depends=Depends, get_current_user=lambda: None, HTTPException=HTTPException,
                   JSONResponse=JSONResponse, Dict=Dict, Any=Any, _manual_captcha_accounts=set())
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<manual endpoint>', 'exec'), env)
        return env['start_manual_captcha']

    async def test_only_confirmed_cookies_are_saved_and_used_for_restart(self):
        for success in (True, False):
            with self.subTest(success=success):
                db = MagicMock()
                db.get_all_cookies.return_value = {'account': 'old'}
                db.update_cookie_account_info.return_value = True
                manager = SimpleNamespace(instances={}, tasks={}, cookie_status={}, update_cookie=AsyncMock())
                result = {'success': success, 'cookies_str': 'confirmed', 'session_id': str(uuid4()), 'stage': 'token_confirm', 'message': 'verified' if success else 'Token refused'}
                with patch.object(manual_captcha, 'open_manual_session', AsyncMock(return_value=result)):
                    response = await self.endpoint(db, manager)('account', result['session_id'], 60, {'user_id': 1})
                self.assertEqual(json.loads(response.body)['success'], success)
                if success:
                    db.update_cookie_account_info.assert_called_once_with('account', cookie_value='confirmed')
                else:
                    db.update_cookie_account_info.assert_not_called()
                manager.update_cookie.assert_awaited_once_with('account', 'confirmed' if success else 'old', save_to_db=False)
                self.assertEqual(db.add_risk_control_log.call_args.kwargs['processing_status'], 'success' if success else 'failed')


class TokenRequestTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, responses):
        from http.cookies import SimpleCookie
        contexts = []
        for payload, cookie in responses:
            response = SimpleNamespace(status=200, cookies=SimpleCookie(cookie), json=AsyncMock(return_value=payload))
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=response)
            cm.__aexit__ = AsyncMock(return_value=False)
            contexts.append(cm)
        session = MagicMock()
        session.post.side_effect = contexts
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        with patch('aiohttp.ClientSession', return_value=session):
            result = await manual_captcha.request_manual_token('_m_h5_tk=old_1; unb=123; x5sec=passed')
        return result, session.post

    async def test_returns_response_cookie_rotation_with_confirmed_token(self):
        result, post = await self.request([({'data': {'accessToken': 'token'}, 'ret': ['SUCCESS::ok']}, '_m_h5_tk=new_2')])
        self.assertEqual(result['token'], 'token')
        self.assertIn('_m_h5_tk=new_2', result['cookies_str'])
        self.assertIn('x5sec=passed', post.call_args.kwargs['headers']['cookie'])

    async def test_expired_signature_retries_once_with_rotated_cookie(self):
        result, post = await self.request([
            ({'ret': ['FAIL_SYS_TOKEN_EXOIRED::expired']}, '_m_h5_tk=new_2'),
            ({'data': {'accessToken': 'token'}, 'ret': ['SUCCESS::ok']}, ''),
        ])
        self.assertEqual(result['token'], 'token')
        self.assertEqual(post.call_count, 2)
        self.assertIn('_m_h5_tk=new_2', post.call_args.kwargs['headers']['cookie'])

    async def test_challenge_is_not_success_and_is_not_automatically_retried(self):
        result, post = await self.request([({'ret': ['FAIL_SYS_USER_VALIDATE::sensitive-detail'], 'data': {'url': 'https://example.test/punish?secret'}}, '')])
        self.assertIsNone(result['token'])
        self.assertEqual(result['reason'], 'FAIL_SYS_USER_VALIDATE')
        self.assertEqual(post.call_count, 1)


class AutomaticCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_takeover_does_not_join_thread_on_event_loop(self):
        import os
        import threading
        tree = ast.parse(Path('XianyuAutoAsync.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'XianyuLive')
        node = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_handle_captcha_verification')
        env = {'asyncio': asyncio, 'os': os, 'logger': MagicMock()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<automatic verification>', 'exec'), env)
        started = threading.Event()
        stop = threading.Event()
        fake = SimpleNamespace(cancel_requested=False)

        def run(*args):
            started.set()
            stop.wait(2)
            return False, None

        fake.run = run
        fake._kill_browser_process = stop.set
        module = SimpleNamespace(XianyuSliderStealth=lambda **kwargs: fake)
        instance = SimpleNamespace(cookie_id='account', cookies_str='old')
        with patch.dict('sys.modules', {'utils.xianyu_slider_stealth': module}):
            task = asyncio.create_task(env['_handle_captcha_verification'](instance, {'data': {'url': 'https://example.test/punish'}}))
            try:
                while not started.is_set():
                    await asyncio.sleep(0.01)
                before = asyncio.get_running_loop().time()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertLess(asyncio.get_running_loop().time() - before, 0.5)
                self.assertTrue(fake.cancel_requested)
                self.assertTrue(stop.is_set())
            finally:
                stop.set()


if __name__ == '__main__':
    unittest.main()
