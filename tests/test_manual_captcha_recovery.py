"""Token confirmation and automatic-task cancellation regression tests."""
import asyncio
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from utils import manual_captcha

class TokenRequestTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, responses, cookies='_m_h5_tk=old_1; unb=123; x5sec=passed'):
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
            result = await manual_captcha.request_manual_token(cookies)
        return result, session.post

    async def test_new_qr_login_initializes_missing_signing_cookie_then_confirms(self):
        result, post = await self.request([
            ({'ret': ['FAIL_SYS_TOKEN_EMPTY::empty']}, '_m_h5_tk=initialized_2; Path=/'),
            ({'data': {'accessToken': 'confirmed'}, 'ret': ['SUCCESS::ok']}, ''),
        ], cookies='unb=123; cookie2=login-session')
        self.assertEqual(result['token'], 'confirmed')
        self.assertEqual(post.call_count, 2)
        first, second = post.call_args_list
        self.assertNotIn('_m_h5_tk=', first.kwargs['headers']['cookie'])
        self.assertIn('_m_h5_tk=initialized_2', second.kwargs['headers']['cookie'])
        self.assertIn('cookie2=login-session', second.kwargs['headers']['cookie'])
        self.assertNotEqual(first.kwargs['params']['sign'], second.kwargs['params']['sign'])

    async def test_signing_cookie_bootstrap_stops_after_one_retry(self):
        result, post = await self.request([
            ({'ret': ['FAIL_SYS_TOKEN_EMPTY::empty']}, '_m_h5_tk=initialized_2'),
            ({'ret': ['FAIL_SYS_TOKEN_EXOIRED::expired']}, '_m_h5_tk=rotated_again_3'),
        ], cookies='unb=123; cookie2=login-session')
        self.assertIsNone(result['token'])
        self.assertEqual(post.call_count, 2)

    async def test_missing_signing_cookie_does_not_discard_platform_challenge(self):
        result, post = await self.request([
            ({'ret': ['FAIL_SYS_USER_VALIDATE', 'RGV587_ERROR'], 'data': {'url': 'https://example.test/punish'}}, ''),
        ], cookies='unb=123; cookie2=login-session')
        self.assertIsNone(result['token'])
        self.assertEqual(result['url'], 'https://example.test/punish')
        self.assertEqual(post.call_count, 1)

    async def test_no_signing_cookie_response_reports_initialization_not_login_failure(self):
        with self.assertRaises(manual_captcha.ManualVerificationError) as error:
            await self.request([({'ret': ['FAIL_SYS_TOKEN_EMPTY']}, '')], cookies='unb=123')
        self.assertEqual(error.exception.stage, 'token_initialize')
        self.assertIn('FAIL_SYS_TOKEN_EMPTY', str(error.exception))
        self.assertNotIn('请重新扫码登录', str(error.exception))

    async def test_deleted_signing_cookie_does_not_trigger_empty_signature_retry(self):
        with self.assertRaises(manual_captcha.ManualVerificationError):
            await self.request([({'ret': ['FAIL_SYS_TOKEN_EXOIRED']}, '_m_h5_tk=; Max-Age=0')])

    async def test_compact_cookie_header_preserves_signing_cookie(self):
        result, post = await self.request([
            ({'data': {'accessToken': 'confirmed'}, 'ret': ['SUCCESS::ok']}, ''),
        ], cookies='unb=123;_m_h5_tk=already_2;cookie2=login-session')
        self.assertEqual(result['token'], 'confirmed')
        self.assertIn('_m_h5_tk=already_2', post.call_args.kwargs['headers']['cookie'])
        self.assertEqual(post.call_count, 1)

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
