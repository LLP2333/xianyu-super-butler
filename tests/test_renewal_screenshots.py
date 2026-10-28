"""Failure screenshots stay private, bounded in number and never break failure handling."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from utils.account_browser import screenshots


class ScreenshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name) / 'renewal'
        p = patch('utils.account_browser.screenshots.SCREENSHOT_DIR', self.dir)
        p.start(); self.addCleanup(p.stop)

    def page(self, fail=False):
        async def shoot(path, **kwargs):
            if fail:
                raise TimeoutError('screenshot hung')
            Path(path).write_bytes(b'jpg')
        return SimpleNamespace(is_closed=lambda: False, screenshot=AsyncMock(side_effect=shoot))

    async def test_saves_jpeg_with_timeout_and_keeps_latest_ten(self):
        self.dir.mkdir(parents=True)
        for index in range(12):
            old = self.dir / f'renewal_123_{index}.jpg'
            old.write_bytes(b'old')
            os.utime(old, (index, index))
        page = self.page()
        name = await screenshots.save(page, '123')
        self.assertRegex(name, r'^renewal_123_\d+\.jpg$')
        kwargs = page.screenshot.call_args.kwargs
        self.assertEqual((kwargs['type'], kwargs['timeout']), ('jpeg', 5000))
        files = sorted(file.name for file in self.dir.iterdir())
        self.assertEqual(len(files), 10)
        self.assertIn(name, files)
        self.assertNotIn('renewal_123_0.jpg', files)

    async def test_failed_closed_or_missing_page_returns_none(self):
        self.assertIsNone(await screenshots.save(self.page(fail=True), '123'))
        self.assertIsNone(await screenshots.save(SimpleNamespace(is_closed=lambda: True), '123'))
        self.assertIsNone(await screenshots.save(None, '123'))

    async def test_path_refuses_names_outside_the_folder(self):
        name = await screenshots.save(self.page(), '../etc')
        self.assertRegex(name, r'^renewal_[A-Za-z0-9_-]+_\d+\.jpg$')
        self.assertEqual(screenshots.path(name), self.dir / name)
        for bad in ('../secret.jpg', 'renewal_1_2.png', None, '/etc/passwd'):
            self.assertIsNone(screenshots.path(bad))
        screenshots.remove(name)
        self.assertIsNone(screenshots.path(name))


if __name__ == '__main__':
    unittest.main()
