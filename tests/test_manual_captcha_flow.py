"""验证人工验证会话的端到端流程（不依赖闲鱼真实风控）。

用本地 mock 滑块页验证修复后的 open_manual_session 链路：

1. 导航到惩罚页（mock）而不是首页 —— 滑块才会出现
2. _wait_for_captcha_present 检测到滑块，而不是把“没滑块”误判成“已完成”
3. 用户通过远程控制通道拖动滑块，滑块消失后取回 Cookie

真实场景里惩罚 URL 由 request_manual_token 提供；
这里直接用本地文件 URL 模拟，验证流程本身正确。
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

FIXTURE = Path(__file__).parent / "fixtures" / "mock_captcha.html"
FIXTURE_URL = FIXTURE.resolve().as_uri()


async def _run_flow():
    """在 mock 页上跑完整人工验证流程。"""
    from playwright.async_api import async_playwright

    from utils.captcha_remote_control import captcha_controller
    from utils.manual_captcha import (
        _wait_for_captcha_present,
        CAPTCHA_PRESENT_TIMEOUT,
    )

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=True,
        channel="chromium",
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
    )
    try:
        context = await browser.new_context(viewport={"width": 800, "height": 600})
        page = await context.new_page()
        await page.goto(FIXTURE_URL, wait_until="domcontentloaded")
        await asyncio.sleep(0.5)

        # 1. 必须能检测到滑块出现（旧实现首页无滑块 → 误判完成）
        appeared = await _wait_for_captcha_present(page, timeout=CAPTCHA_PRESENT_TIMEOUT)
        if not appeared:
            return {"ok": False, "stage": "captcha_not_present"}

        # 2. 建会话
        await captcha_controller.create_session("mock-account", page)

        # 3. 模拟用户拖滑块：按下 → 拖动 → 释放
        box_handle = await page.query_selector("#nc_scale")
        box_bbox = await box_handle.bounding_box()
        start_x = box_bbox["x"] + 20
        y = box_bbox["y"] + 20
        origin = captcha_controller.active_sessions['mock-account']['clip']
        assert origin and origin['x'] > 0 and origin['y'] > 0
        # The image displayed at half scale must map back to the same page point.
        displayed_x = (start_x - origin['x']) / 2
        displayed_y = (y - origin['y']) / 2
        await captcha_controller.handle_mouse_event('mock-account', 'down',
            round(displayed_x * 2 + origin['x']), round(displayed_y * 2 + origin['y']))
        # 分步拖动，模拟真实手势
        for step in range(1, 9):
            await captcha_controller.handle_mouse_event('mock-account', 'move', start_x + step * 55, y + 2)
            await asyncio.sleep(0.05)
        await captcha_controller.handle_mouse_event('mock-account', 'up', start_x + 450, y + 2)

        # 4. 滑块应消失 → 验证完成
        await asyncio.sleep(1)
        completed = await captcha_controller.check_completion("mock-account")
        return {"ok": completed, "stage": "done" if completed else "not_completed"}
    finally:
        await captcha_controller.close_session('mock-account')
        await browser.close()
        await playwright.stop()


class ManualCaptchaFlowTests(unittest.TestCase):
    def test_flow_detects_slider_and_completes_after_drag(self):
        result = asyncio.run(_run_flow())
        self.assertTrue(result["ok"], f"流程失败于阶段: {result['stage']}")

    def test_wait_for_captcha_present_detects_slider(self):
        from playwright.async_api import async_playwright
        from utils.manual_captcha import _wait_for_captcha_present

        async def _check():
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    channel="chromium",
                    args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
                )
                try:
                    page = await browser.new_page()
                    await page.goto(FIXTURE_URL, wait_until="domcontentloaded")
                    await asyncio.sleep(0.3)
                    return await _wait_for_captcha_present(page, timeout=10)
                finally:
                    await browser.close()

        self.assertTrue(asyncio.run(_check()))

    def test_wait_for_captcha_present_returns_false_on_blank_page(self):
        """无滑块页面（如闲鱼首页）必须返回 False，不能误判为已出现。"""
        from playwright.async_api import async_playwright
        from utils.manual_captcha import _wait_for_captcha_present

        async def _check():
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    channel="chromium",
                    args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
                )
                try:
                    page = await browser.new_page()
                    await page.set_content("<html><body><p>hello</p></body></html>")
                    return await _wait_for_captcha_present(page, timeout=3)
                finally:
                    await browser.close()

        self.assertFalse(asyncio.run(_check()))


if __name__ == "__main__":
    unittest.main()
