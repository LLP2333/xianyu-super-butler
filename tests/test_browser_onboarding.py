import sqlite3
import threading
import unittest
from app.db_manager import DBManager

class AccountCreationTests(unittest.TestCase):
    def setUp(self):
        self.db=DBManager.__new__(DBManager)
        self.db.lock=threading.RLock()
        self.db.conn=sqlite3.connect(':memory:')
        self.db.conn.execute('CREATE TABLE cookies(id TEXT PRIMARY KEY, value TEXT, user_id INTEGER)')
        self.addCleanup(self.db.conn.close)

    def test_first_account_is_created_and_second_never_overwrites(self):
        self.db.create_browser_account('123','unb=123; cookie2=first',7)
        with self.assertRaises(ValueError):
            self.db.create_browser_account('123','unb=123; cookie2=other',8)
        with self.assertRaises(ValueError):
            self.db.create_browser_account('999','unb=999',7)
        self.assertEqual(self.db.conn.execute('SELECT * FROM cookies').fetchall(),
                         [('123','unb=123; cookie2=first',7)])
