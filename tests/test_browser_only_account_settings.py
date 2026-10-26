"""Browser accounts retain settings and profiles without password-login fields."""
import os
import tempfile
import unittest

from app.db_manager import DBManager
from app.reply_server import app


class BrowserOnlyAccountSettingsTests(unittest.TestCase):
    def test_cookie_update_preserves_account_settings_and_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DBManager(os.path.join(directory, 'accounts.db'))
            try:
                columns = {row[1] for row in db.conn.execute('PRAGMA table_info(cookies)')}
                self.assertTrue({'username', 'password', 'show_browser'}.isdisjoint(columns))
                owner = db.conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
                db.create_browser_account('123', 'unb=123; cookie2=first', owner)
                db.update_cookie_remark('123', '店铺')
                db.update_cookie_pause_duration('123', 0)
                db.update_cookie_profile('123', {
                    'nickname': '昵称', 'avatar_url': 'https://example.com/avatar.png',
                    'location': '上海', 'bio': '简介', 'followers': 12, 'following': 3,
                })
                before = db.get_cookie_details('123')
                self.assertTrue(db.update_cookie_account_info('123', cookie_value='unb=123; cookie2=new'))
                after = db.get_cookie_details('123')
                self.assertEqual(after, {**before, 'value': 'unb=123; cookie2=new'})
                self.assertEqual(after['nickname'], '昵称')
                self.assertEqual(after['followers'], 12)
                self.assertEqual(after['following'], 3)
                self.assertEqual(after['pause_duration'], 0)
                self.assertTrue({'username', 'password', 'show_browser'}.isdisjoint(after))
            finally:
                db.conn.close()

    def test_password_login_routes_are_removed(self):
        paths = {route.path for route in app.routes if hasattr(route, 'path')}
        for path in ('/password-login', '/password-login/check/{session_id}',
                     '/cookies/{cid}/login-info', '/cookie/{cid}/account-info'):
            self.assertNotIn(path, paths)
        self.assertIn('/cookies/{cid}', paths)
