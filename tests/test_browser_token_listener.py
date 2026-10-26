import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import AsyncMock, patch
from app.config import API_ENDPOINTS
from utils.account_browser.listener import TokenListener, token_response
from utils.account_browser.tokens import BrowserToken, TokenStore


class StoreTests(unittest.TestCase):
    def test_authorization_rotation_expiry_and_rejection(self):
        store = TokenStore()
        first = BrowserToken('123', 'one', 'did', 'app', 'unb=123')
        store.observe('a', first)
        self.assertIsNone(store.get('a'))
        store.approve('a', first)
        self.assertIs(store.get('a'), first)
        store.observe('a', BrowserToken('123', 'one', 'did', 'app', 'unb=123'))
        self.assertIs(store.get('a'), first)
        second = BrowserToken('123', 'two', 'did', 'app', 'unb=123')
        store.observe('a', second)
        self.assertIs(store.get('a'), first)
        store.approve('a', second)
        self.assertIs(store.get('a'), second)
        store.reject('a', 'two')
        store.observe('a', second)
        self.assertIsNone(store.get('a'))
        expired = BrowserToken('123', 'expired', 'did', 'app', 'unb=123', time.time()-3601)
        store.observe('a', expired)
        with self.assertRaises(ValueError):
            store.approve('a', expired)
        store.clear('a')
        self.assertIsNone(store.get('a', candidate=True))

    def test_pause_requires_explicit_adoption_of_new_capture(self):
        store = TokenStore()
        first = BrowserToken('123', 'one', 'did', 'app', 'unb=123')
        store.observe('a', first)
        store.approve('a', first)
        store.pause('a')
        store.observe('a', BrowserToken('123', 'two', 'did', 'app', 'unb=123'))
        self.assertIsNone(store.get('a'))
        store.approve('a', store.get('a', candidate=True))
        self.assertEqual(store.get('a').token, 'two')

    def test_reconnect_cannot_extend_expired_token_lifetime(self):
        store = TokenStore()
        old = BrowserToken('123','same','device','app','unb=123',time.time()-3601)
        store.observe('a', old)
        store.clear('a')
        store.observe('a', BrowserToken('123','same','device','app','unb=123'))
        self.assertIsNone(store.get('a',candidate=True))

    def test_secret_values_are_not_in_repr(self):
        value = BrowserToken('123', 'SECRET-TOKEN', 'did', 'app', 'SECRET-COOKIE')
        self.assertNotIn('SECRET', repr(value))


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = TokenStore()
        self.patch = patch('utils.account_browser.listener.tokens', self.store)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.listener = TokenListener('a', '123', None)
        self.listener.context = SimpleNamespace(cookies=AsyncMock(return_value=[{'name':'unb','value':'123'}]))
        self.response = SimpleNamespace(url=API_ENDPOINTS['token'], status=200,
            request=SimpleNamespace(post_data=urlencode({'data':json.dumps({'deviceId':'native-device','appKey':'native-app'})}),
                                    all_headers=AsyncMock(return_value={'cookie':'unb=123'})),
            json=AsyncMock(return_value={'data':{'accessToken':'native-token'}}))

    async def test_observes_response_with_its_original_request_parameters(self):
        await self.listener.consume(self.response)
        value = self.store.get('a', candidate=True)
        self.assertEqual((value.token, value.device_id, value.app_key), ('native-token','native-device','native-app'))
        self.assertIsNone(self.store.get('a'))

    async def test_request_identity_and_current_identity_must_both_match(self):
        self.response.request.all_headers.return_value = {'cookie':'unb=999'}
        await self.listener.consume(self.response)
        self.assertIsNone(self.store.get('a', candidate=True))
        self.response.request.all_headers.return_value = {'cookie':'unb=123'}
        self.listener.context.cookies.return_value = [{'name':'unb','value':'999'}]
        await self.listener.consume(self.response)
        self.assertIsNone(self.store.get('a', candidate=True))

    async def test_unrelated_response_and_challenge_are_not_tokens(self):
        self.response.url = 'https://evil.test/h5/token'
        await self.listener.consume(self.response)
        self.response.json.assert_not_awaited()
        self.response.url = API_ENDPOINTS['token']
        self.response.json.return_value = {'ret':['FAIL_SYS_USER_VALIDATE'], 'data':{'url':'https://example.test/verify'}}
        await self.listener.consume(self.response)
        self.assertIsNone(self.store.get('a', candidate=True))
        self.listener.context.cookies.assert_not_awaited()

    def test_missing_device_parameters_and_malformed_payload_are_rejected(self):
        payload = {'data': {'accessToken':'token'}}
        for body in ('', 'data=not-json', 'data=%7B%7D', 'data=null'):
            self.assertIsNone(token_response(API_ENDPOINTS['token'], body, payload))

    async def test_logged_out_browser_clears_token_without_stopping_listener(self):
        await self.listener.consume(self.response)
        self.listener.context.cookies.return_value = []
        self.listener.task = asyncio.current_task()
        self.assertIsNone(await self.listener.current_account())
        self.assertIsNone(self.store.get('a', candidate=True))

    async def test_request_without_cookie_is_ignored_without_error(self):
        self.response.request.all_headers.return_value = {}
        await self.listener.consume(self.response)
        self.assertIsNone(self.listener.error)
        self.response.json.assert_not_awaited()

    async def test_first_login_learns_identity_only_when_request_and_jar_match(self):
        self.listener.expected_account=None
        self.listener.on_capture=AsyncMock()
        await self.listener.consume(self.response)
        value=self.store.get('a',candidate=True)
        self.assertEqual(value.account,'123')
        self.listener.on_capture.assert_awaited_once_with(value)
        self.store.clear('a')
        self.response.request.all_headers.return_value={'cookie':'unb=999'}
        await self.listener.consume(self.response)
        self.assertIsNone(self.store.get('a',candidate=True))
        self.assertEqual(self.listener.on_capture.await_count,1)
