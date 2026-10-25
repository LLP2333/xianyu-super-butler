"""Explicit challenge-page errors must not be treated as a usable slider or successful gesture."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from utils.manual_captcha import (
    ManualVerificationError,
    _wait_for_captcha_present,
    collect_verification_cookies,
    get_captcha_page_error,
)
from utils.captcha_remote_control import captcha_controller


def frame(text):
    return SimpleNamespace(locator=MagicMock(return_value=SimpleNamespace(inner_text=AsyncMock(return_value=text))),
                           query_selector=AsyncMock())


class CaptchaPageErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_visible_refresh_error_in_iframe_is_reported_with_code(self):
        page = SimpleNamespace(frames=[frame('Please drag the slider to verify'), frame(
            "Oops... something's wrong. Please refresh and try again. (error:test-code)")])
        message = await get_captcha_page_error(page)
        self.assertIn('test-code', message)
        self.assertIn('重新加载验证页', message)

    async def test_retryable_or_normal_text_is_not_a_terminal_refresh_error(self):
        for text in ('Please drag the slider to verify', '请稍后重试', '验证成功', '请重新拖动滑块'):
            with self.subTest(text=text):
                self.assertIsNone(await get_captcha_page_error(SimpleNamespace(frames=[frame(text)])))

    async def test_error_container_does_not_count_as_ready_slider(self):
        visible_frame = frame('Please refresh and try again')
        visible_frame.query_selector.return_value = SimpleNamespace(is_visible=AsyncMock(return_value=True))
        with self.assertRaises(ManualVerificationError) as error:
            await _wait_for_captcha_present(SimpleNamespace(frames=[visible_frame]), timeout=1)
        self.assertEqual(error.exception.stage, 'captcha_page')
        visible_frame.query_selector.assert_not_awaited()

    async def test_refresh_error_prevents_cookie_collection_even_with_changed_cookie(self):
        context = SimpleNamespace(cookies=AsyncMock(return_value=[{'name': 'x5sec', 'value': 'changed'}]))
        captcha_controller.active_sessions['error-test'] = {
            'page': SimpleNamespace(frames=[frame('Please refresh and try again')])}
        try:
            with self.assertRaises(ManualVerificationError) as error:
                await collect_verification_cookies(context, 'error-test', 'x5sec=old')
            self.assertEqual(error.exception.stage, 'captcha_page')
            context.cookies.assert_not_awaited()
        finally:
            captcha_controller.active_sessions.pop('error-test', None)

    async def test_detached_frame_is_not_assumed_success_or_failure(self):
        detached = frame('')
        detached.locator.return_value.inner_text.side_effect = RuntimeError('frame detached')
        self.assertIsNone(await get_captcha_page_error(SimpleNamespace(frames=[detached])))


if __name__ == '__main__':
    unittest.main()
