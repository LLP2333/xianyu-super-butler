import unittest
from utils.account_browser.address import validate_desktop_address

class AddressTests(unittest.TestCase):
    def test_local_and_https_origins(self):
        self.assertEqual(validate_desktop_address(''), '')
        self.assertEqual(validate_desktop_address('http://localhost:3000'), 'http://localhost:3000/')
        self.assertEqual(validate_desktop_address(' https://browser.example.com:8443 '), 'https://browser.example.com:8443/')

    def test_invalid_origins(self):
        for value in ['ftp://example.com', 'https://user:pass@example.com', 'https://example.com/path', 'https://example.com/?x=1', 'https://example.com/#x', 'https://', 'https://example.com:bad', 'https://example.com?', 'https://example.com#']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_desktop_address(value)
