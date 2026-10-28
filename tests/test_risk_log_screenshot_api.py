"""Renewal screenshots are served only to the account owner or an admin, and removed with their log."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.reply_server import delete_risk_control_log, get_risk_control_log_screenshot

USER = {"user_id": 7, "username": "seller"}


class RiskLogScreenshotApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        p = patch("utils.account_browser.screenshots.SCREENSHOT_DIR", self.dir)
        p.start(); self.addCleanup(p.stop)
        (self.dir / "renewal_123_1.jpg").write_bytes(b"jpg")

    async def test_owner_gets_file_and_others_are_refused(self):
        with patch("app.reply_server.db_manager.get_risk_control_log",
                   return_value={"id": 1, "screenshot": "renewal_123_1.jpg"}) as lookup:
            response = await get_risk_control_log_screenshot(1, USER)
        lookup.assert_called_once_with(1, user_id=7)
        self.assertEqual(Path(response.path), self.dir / "renewal_123_1.jpg")
        self.assertEqual(response.media_type, "image/jpeg")
        with patch("app.reply_server.db_manager.get_risk_control_log", return_value=None):
            with self.assertRaises(HTTPException) as context:
                await get_risk_control_log_screenshot(1, USER)
        self.assertEqual(context.exception.status_code, 404)

    async def test_pruned_or_forged_name_is_not_found(self):
        for name in ("renewal_123_9.jpg", "../../data/xianyu_data.db", None):
            with patch("app.reply_server.db_manager.get_risk_control_log", return_value={"id": 1, "screenshot": name}):
                with self.assertRaises(HTTPException) as context:
                    await get_risk_control_log_screenshot(1, USER)
            self.assertEqual(context.exception.status_code, 404)

    async def test_deleting_log_removes_its_screenshot(self):
        with patch("app.reply_server.db_manager.get_risk_control_log",
                   return_value={"id": 1, "screenshot": "renewal_123_1.jpg"}), \
                patch("app.reply_server.db_manager.delete_risk_control_log", return_value=True):
            result = await delete_risk_control_log(1, USER)
        self.assertTrue(result["success"])
        self.assertFalse((self.dir / "renewal_123_1.jpg").exists())


if __name__ == "__main__":
    unittest.main()
