import unittest
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit,parse_qs
from http.cookies import SimpleCookie
import aiohttp
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from app.api_account_browser import create_account_browser_router


class DesktopAccessTests(unittest.TestCase):
    def setUp(self):
        self.logged_in=True
        self.browser=SimpleNamespace(owner_id=7,desktop=SimpleNamespace(
            desktop_url='https://localhost:3001/',auth=aiohttp.BasicAuth('account','internal-key')))
        self.service=SimpleNamespace(desktop=lambda *_:self.browser,desktop_url=lambda *_:self.browser.desktop.desktop_url,clear=AsyncMock(return_value={'message':'cleared'}))
        def auth():
            if not self.logged_in: raise HTTPException(403,'denied')
            return {'user_id':7}
        def verify(token):
            return {'user_id':7} if self.logged_in and token=='app-session' else None
        app=FastAPI();app.include_router(create_account_browser_router(auth,self.service,verify))
        self.client=TestClient(app)
        self.addCleanup(self.client.close)

    def grant(self):
        r=self.client.post('/api/account-browser/desktop-link',headers={'authorization':'Bearer app-session'})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.headers['cache-control'],'no-store')
        self.assertNotIn('internal-key',r.text)
        return parse_qs(urlsplit(r.json()['open_url']).query)['ticket'][0]

    def enter(self,ticket,key='internal-key'):
        return self.client.get('/api/account-browser/desktop-entry',headers={
            'x-desktop-key':key,'x-desktop-ticket':ticket},follow_redirects=False)

    def test_single_use_link_becomes_secure_session_and_removes_ticket_from_url(self):
        ticket=self.grant();r=self.enter(ticket)
        self.assertEqual(r.status_code,303);self.assertEqual(r.headers['location'],'/')
        self.assertIn('HttpOnly',r.headers['set-cookie']);self.assertIn('Secure',r.headers['set-cookie'])
        cookie=SimpleCookie();cookie.load(r.headers['set-cookie'])
        session=cookie['xianyu_desktop'].value
        self.assertNotEqual(session,ticket)
        check=self.client.get('/api/account-browser/desktop-auth',headers={'x-desktop-key':'internal-key','x-desktop-session':session})
        self.assertEqual(check.status_code,204)
        self.assertEqual(self.enter(ticket).status_code,403)
        self.logged_in=False
        check=self.client.get('/api/account-browser/desktop-auth',headers={'x-desktop-key':'internal-key','x-desktop-session':session})
        self.assertEqual(check.status_code,403)

    def test_custom_origin_link_keeps_ticket_authorization(self):
        self.browser.desktop.desktop_url = 'https://browser.example.com/'
        response = self.client.post('/api/account-browser/desktop-link', headers={'authorization':'Bearer app-session'})
        self.assertTrue(response.json()['open_url'].startswith('https://browser.example.com/_desktop_open?ticket='))
        ticket = parse_qs(urlsplit(response.json()['open_url']).query)['ticket'][0]
        self.assertEqual(self.enter(ticket).status_code,303)

    def test_bad_container_key_cannot_consume_ticket(self):
        ticket=self.grant()
        self.assertEqual(self.enter(ticket,'wrong').status_code,403)
        self.assertEqual(self.enter(ticket).status_code,303)

    def test_unknown_ticket_and_session_rejected(self):
        self.assertEqual(self.enter('unknown').status_code,403)
        self.assertEqual(self.client.get('/api/account-browser/desktop-auth',headers={'x-desktop-key':'internal-key'}).status_code,403)

    def test_reset_container_invalidates_old_link(self):
        ticket=self.grant()
        self.browser.desktop.auth=aiohttp.BasicAuth('account','new-key')
        self.assertEqual(self.enter(ticket,'new-key').status_code,403)

    def test_clear_requires_explicit_confirmation(self):
        self.assertEqual(self.client.post('/api/account-browser/clear',json={'confirmed':False}).status_code,400)
        self.service.clear.assert_not_awaited()
        self.assertEqual(self.client.post('/api/account-browser/clear',json={'confirmed':True}).status_code,200)
        self.service.clear.assert_awaited_once_with(7)

    def test_expired_ticket_is_rejected(self):
        ticket=self.grant()
        now=time.monotonic()
        with patch('app.api_account_browser.time',SimpleNamespace(monotonic=lambda:now+61)):
            self.assertEqual(self.enter(ticket).status_code,403)
