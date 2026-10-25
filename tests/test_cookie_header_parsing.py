import unittest
from utils.xianyu_utils import trans_cookies


class CookieHeaderParsingTests(unittest.TestCase):
    def test_separator_whitespace_is_optional(self):
        expected = {'unb': '123', '_m_h5_tk': 'signed_1', 'cookie2': 'session'}
        for header in (
            'unb=123;_m_h5_tk=signed_1;cookie2=session',
            'unb=123; _m_h5_tk=signed_1; cookie2=session',
            ' unb=123;  _m_h5_tk=signed_1;\tcookie2=session; ',
        ):
            with self.subTest(header=header):
                self.assertEqual(trans_cookies(header), expected)

    def test_values_can_contain_equals_and_be_empty(self):
        self.assertEqual(trans_cookies('a=abc==;b=;bad-fragment;=bad'), {'a': 'abc==', 'b': ''})

    def test_duplicate_name_uses_last_value(self):
        self.assertEqual(trans_cookies('_m_h5_tk=old;_m_h5_tk=new'), {'_m_h5_tk': 'new'})

    def test_empty_header_remains_an_error(self):
        for value in ('', None):
            with self.assertRaises(ValueError):
                trans_cookies(value)


if __name__ == '__main__':
    unittest.main()
