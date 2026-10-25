"""人工验证会话：持有服务器浏览器，回收 Cookie，并以 Token 确认结果。"""

import asyncio
import re
import time
from typing import Any, Dict, Optional

from loguru import logger
from utils import browser_limit
from utils.user_agents import CHROME_UA

# 人工操作需要时间，但也不能无限占用浏览器
DEFAULT_TIMEOUT = 300

# 等待滑块真正出现的最长时间（秒）。惩罚页加载本身就要几秒，
# 等不到说明 x5secdata 已失效或该账号当前已不在风控状态。
CAPTCHA_PRESENT_TIMEOUT = 20


class ManualVerificationError(Exception):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


async def request_manual_token(cookies_str: str, *, retry_cookie_rotation: bool = True) -> Dict[str, Any]:
    """Initialize/rotate signing cookies at most once; never auto-solve challenges."""
    import json
    import aiohttp
    from app.config import API_ENDPOINTS
    from utils.xianyu_utils import trans_cookies, generate_sign, generate_device_id

    cd = trans_cookies(cookies_str)
    token = cd.get('_m_h5_tk', '').split('_')[0]
    # QR login can finish before the platform has issued the MTOP signing cookie.
    # The first request may initialize it; absence alone does not prove login expiry.
    cookies_str = '; '.join(f'{key}={value}' for key, value in cd.items())

    ts = str(int(time.time() * 1000))
    params = {
        "jsv": "2.7.2", "appKey": "34839810", "t": ts, "sign": "", "v": "1.0",
        "type": "originaljson", "accountSite": "xianyu", "dataType": "json",
        "timeout": "20000", "api": "mtop.taobao.idlemessage.pc.login.token",
        "sessionOption": "AutoLoginOnly",
        "dangerouslySetWindvaneParams": "%5Bobject%20Object%5D",
        "smToken": "token", "queryToken": "sm", "sm": "sm",
        "spm_cnt": "a21ybx.im.0.0", "spm_pre": "a21ybx.home.sidebar.1.4c053da6vYwnmf",
        "log_id": "4c053da6vYwnmf",
    }
    device_id = generate_device_id(cd.get("unb", ""))
    data_val = json.dumps(
        {"appKey": "444e9908a51d1cb236a27862abc769c9", "deviceId": device_id}
    )
    params["sign"] = generate_sign(params["t"], token, data_val)

    headers = {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "user-agent": CHROME_UA,
        "referer": "https://www.goofish.com/",
        "origin": "https://www.goofish.com",
        "cookie": cookies_str,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                API_ENDPOINTS.get('token'), params=params, data={'data': data_val},
                headers=headers, timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    raise ManualVerificationError('token_confirm', f'Token 接口 HTTP {response.status}')
                payload = await response.json(content_type=None)
                # Preserve cookie rotations issued by the Token response.
                for name, morsel in response.cookies.items():
                    if morsel['max-age'] == '0':
                        cd.pop(name, None)
                    else:
                        cd[name] = morsel.value
    except ManualVerificationError:
        raise
    except Exception as exc:
        raise ManualVerificationError('token_confirm', f'Token 请求失败（{type(exc).__name__}）') from exc
    if not isinstance(payload, dict):
        raise ManualVerificationError('token_confirm', 'Token 接口响应格式错误')
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    # Only retain result codes, never log tokens, cookies or challenge URLs.
    ret = payload.get('ret') or []
    if isinstance(ret, str):
        ret = [ret]
    codes = ', '.join(str(value).split('::')[0][:80] for value in ret)[:240]
    updated_cookies = '; '.join(f'{key}={value}' for key, value in cd.items())
    from utils.risk_control import is_risk_control_error
    new_signing_token = cd.get('_m_h5_tk', '').split('_')[0]
    if (retry_cookie_rotation and not data.get('accessToken')
            and not is_risk_control_error(codes)
            and any(code in codes for code in ('FAIL_SYS_TOKEN_EXOIRED', 'FAIL_SYS_TOKEN_EXPIRED', 'FAIL_SYS_TOKEN_EMPTY'))
            and new_signing_token and new_signing_token != token):
        return await request_manual_token(updated_cookies, retry_cookie_rotation=False)
    if not new_signing_token and not data.get('accessToken') and not data.get('url'):
        raise ManualVerificationError(
            'token_initialize',
            f'平台未返回可用的签名 Cookie（{codes or "无返回码"}）；'
            '请检查扫码 Cookie 补全结果，不能仅凭此错误认定登录失效',
        )
    return {
        'token': data.get('accessToken'),
        'url': data.get('url') or '',
        'cookies_str': updated_cookies,
        'reason': codes or '接口未返回 accessToken',
    }


async def get_captcha_page_error(page) -> Optional[str]:
    """Read visible challenge text, including iframe contents; never scan script source."""
    for frame in page.frames:
        try:
            visible_text = await frame.locator('body').inner_text(timeout=1000)
        except Exception:
            # Detached/navigating frames do not prove either success or failure.
            continue
        match = re.search(
            r'please\s+refresh\s+(?:the\s+page\s+)?and\s+try\s+again'
            r'|请刷新[^\n。]{0,12}重试|刷新页面[^\n。]{0,12}重试',
            visible_text, re.IGNORECASE,
        )
        if match:
            code = re.search(r'error\s*[:：]\s*([a-zA-Z0-9_-]{1,40})', visible_text, re.IGNORECASE)
            suffix = f"（页面错误码：{code.group(1)}）" if code else ''
            return f'验证页面提示出错，请刷新后重试{suffix}；当前会话已结束，请点击“重新加载验证页”'
    return None


async def collect_verification_cookies(context, session_id: str, previous: str) -> str:
    """Wait briefly for the user's challenge response, without issuing Token retries."""
    from app.config import API_ENDPOINTS
    from utils.xianyu_utils import trans_cookies
    from utils.captcha_remote_control import captcha_controller

    previous_pass = trans_cookies(previous).get('x5sec')
    deadline = time.monotonic() + 5
    while True:
        await asyncio.sleep(0.25)
        page_error = await get_captcha_page_error(captcha_controller.active_sessions[session_id]['page'])
        if page_error:
            raise ManualVerificationError('captcha_page', page_error)
        cookies = _from_playwright_cookies(await context.cookies([API_ENDPOINTS['token']]))
        current_pass = trans_cookies(cookies).get('x5sec') if cookies else None
        if ((current_pass and current_pass != previous_pass)
                or await captcha_controller.check_completion(session_id)
                or time.monotonic() >= deadline):
            return cookies


async def open_manual_session(
    cookie_id: str,
    cookies_str: str,
    timeout: int = DEFAULT_TIMEOUT,
    headless: bool = True,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """打开一个人工验证会话，等待人在浏览器里完成滑块。

    Args:
        timeout: 等待人工操作的秒数，超时后放弃并关闭浏览器。
        headless: 默认无头 —— 页面通过远程通道推给用户，不需要本地窗口。

    Returns:
        ``{"success": bool, "cookies_str": str, "message": str, "session_id": str}``。
        成功时 ``cookies_str`` 是完成验证后的新 Cookie，调用方应保存。
    """
    from playwright.async_api import async_playwright

    from utils.captcha_remote_control import captcha_controller

    session_id = session_id or str(cookie_id)
    result: Dict[str, Any] = {
        "success": False,
        "cookies_str": cookies_str,
        "message": "",
        "session_id": session_id,
    }

    playwright = None
    browser = None
    context = None
    refresh_task = None
    stage = "browser_start"
    try:
        playwright = await async_playwright().start()
        browser = await browser_limit.launch_browser(
            playwright,
            {'headless': headless,
             # 只用完整版 Chromium。headless=True 默认会去找单独下载的
             # chromium_headless_shell，缺失时报 Executable doesn't exist，
             # 人工验证页面就会卡在「正在服务器上打开验证页面」。
             'channel': 'chromium',
             'args': [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ]},
            "人工验证码",
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=CHROME_UA,
        )

        if cookies_str:
            await context.add_cookies(_to_playwright_cookies(cookies_str))

        page = await context.new_page()

        stage = 'challenge_fetch'
        initial = await request_manual_token(cookies_str)
        if initial['token']:
            result.update(success=True, cookies_str=initial['cookies_str'],
                          token=initial['token'], message='Token 确认成功，账号已可恢复')
            return result
        verification_url = initial['url']
        if not verification_url or not ('punish' in verification_url or 'action=captcha' in verification_url):
            raise ManualVerificationError(stage, f"未取得滑块验证链接：{initial['reason']}")
        await context.add_cookies(_to_playwright_cookies(initial['cookies_str']))
        stage = 'page_load'
        try:
            await page.goto(verification_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            raise ManualVerificationError(stage, f"验证页面加载失败（{type(exc).__name__}）") from exc
        # 给验证组件一点渲染时间，否则截图可能是空白
        await asyncio.sleep(2)

        # 等滑块真正出现再建会话 —— 找不到滑块说明 x5secdata 已失效，
        # 此时即使建了会话也拖不出结果，尽早告知用户更合适。
        stage = 'captcha_detect'
        captcha_appeared = await _wait_for_captcha_present(page, timeout=CAPTCHA_PRESENT_TIMEOUT)
        if not captcha_appeared:
            result["message"] = (
                "验证页面未检测到可见的滑块组件，请重新加载验证页；"
                "若仍失败，请在闲鱼官方客户端检查账号验证要求"
            )
            logger.warning(f"【{cookie_id}】{result['message']}")
            raise ManualVerificationError(stage, result['message'])

        await captcha_controller.create_session(session_id, page)
        # 持续推送截图，让用户能看到自己的拖动效果
        refresh_task = asyncio.create_task(
            captcha_controller.auto_refresh_screenshot(session_id, interval=0.1)
        )

        logger.warning(
            f"【{cookie_id}】已开启人工验证会话，请在 {timeout} 秒内于"
            f"账号管理页的人工验证弹窗完成操作"
        )

        deadline = time.monotonic() + timeout
        released = captcha_controller.active_sessions[session_id]['released']
        last_reason = '等待人工操作'
        previous_cookies = initial['cookies_str']
        while True:
            stage = 'human_input'
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ManualVerificationError(stage, f'人工验证超时：{last_reason}')
            try:
                await asyncio.wait_for(released.wait(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise ManualVerificationError(stage, f'人工验证超时：{last_reason}') from exc
            released.clear()
            if captcha_controller.active_sessions[session_id].get('cancelled'):
                raise ManualVerificationError('cancelled', '用户已取消人工验证')
            captcha_controller.active_sessions[session_id].update(phase='verifying', message='正在回收 Cookie 并确认 Token…')
            ws = captcha_controller.websocket_connections.get(session_id)
            if ws:
                try:
                    await ws.send_json({'type': 'verifying', 'message': '正在回收 Cookie 并确认 Token…'})
                except Exception:
                    pass
            stage = 'cookie_collect'
            new_cookies = await collect_verification_cookies(context, session_id, previous_cookies)
            if not new_cookies:
                raise ManualVerificationError(stage, '人工操作后未读取到 Cookie')
            from utils.xianyu_utils import drop_stale_captcha_challenge
            new_cookies = drop_stale_captcha_challenge(new_cookies)
            previous_cookies = new_cookies
            stage = 'token_confirm'
            confirmed = await request_manual_token(new_cookies)
            if confirmed['token']:
                captcha_controller.active_sessions[session_id]['completed'] = True
                result.update(success=True, cookies_str=confirmed['cookies_str'],
                              token=confirmed['token'], message='人工验证成功，Token 已确认')
                return result
            page_error = await get_captcha_page_error(page)
            if page_error:
                raise ManualVerificationError('captcha_page', f"{page_error}；Token 结果：{confirmed['reason']}")
            if confirmed.get('cookies_str'):
                await context.add_cookies(_to_playwright_cookies(confirmed['cookies_str']))
            last_reason = f"[token_confirm] Token 确认失败：{confirmed['reason']}"
            captcha_controller.active_sessions[session_id].update(phase='ready', message=f'{last_reason}，账号尚未恢复，可继续操作')
            logger.warning(f"【{cookie_id}】{last_reason}")
            # A rejected gesture does not destroy the browser or start a new challenge.
            # Keep the same session for the user's next attempt; never auto-retry Token.
            if ws:
                try:
                    await ws.send_json({'type': 'retry', 'message': f'{last_reason}，账号尚未恢复，可继续操作'})
                except Exception:
                    pass
    except Exception as exc:
        failure_stage = exc.stage if isinstance(exc, ManualVerificationError) else stage
        message = str(exc) if isinstance(exc, ManualVerificationError) else f'处理异常（{type(exc).__name__}）'
        result.update(stage=failure_stage, message=f'[{failure_stage}] {message}')
        logger.warning(f"【{cookie_id}】人工验证失败：{result['message']}")
        return result
    finally:
        if refresh_task and not refresh_task.done():
            refresh_task.cancel()
            await asyncio.gather(refresh_task, return_exceptions=True)
        try:
            from utils.captcha_remote_control import captcha_controller

            await captcha_controller.close_session(session_id)
        except Exception:
            pass
        for closer in (context, browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass


async def _wait_for_captcha_present(page, timeout: int = CAPTCHA_PRESENT_TIMEOUT) -> bool:
    """等页面里出现可见的滑块/验证码元素。

    原实现不区分“页面没有滑块”和“滑块验证完成” —— 首页没滑块，于是
    check_completion 误判完成。这里显式等滑块出现，避免把“没弹出滑块”
    当成“已完成”。
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError  # noqa: F401

    # 与 captcha_remote_control.check_completion 用同一组选择器，保证判断一致
    selectors = [
        "#nocaptcha",
        "#scratch-captcha-btn",
        ".scratch-captcha-container",
        ".scratch-captcha-slider",
        '[id*="captcha"]',
        ".nc-container",
    ]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page_error = await get_captcha_page_error(page)
        if page_error:
            raise ManualVerificationError('captcha_page', page_error)
        # 先查主页面，再查 iframe（阿里滑块通常包在 iframe 里）
        for frame in page.frames:
            for selector in selectors:
                try:
                    element = await frame.query_selector(selector)
                    if element and await element.is_visible():
                        logger.info(f"检测到滑块元素: {selector} (frame: {frame.url[:80]})")
                        return True
                except Exception:
                    continue
        await asyncio.sleep(1)
    return False


def _to_playwright_cookies(cookies_str: str) -> list:
    """把 Cookie 字符串转成 Playwright 需要的结构。"""
    cookies = []
    for part in cookies_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": ".goofish.com",
            "path": "/",
        })
    return cookies


def _from_playwright_cookies(cookies: list) -> str:
    """把 Playwright 的 Cookie 列表拼回字符串。"""
    pairs = {}
    # A host/path-specific cookie overrides a parent-domain cookie of the same name.
    for cookie in sorted(cookies or [], key=lambda c: (len(c.get('path', '/')), len(c.get('domain', '')))):
        name = cookie.get('name')
        if name:
            pairs[name] = cookie.get('value', '')
    return '; '.join(f'{name}={value}' for name, value in pairs.items())
