"""账号浏览器 Cookie 转换与 Token 确认。"""

import time
from typing import Any, Dict, Optional

from loguru import logger
from utils.user_agents import CHROME_UA

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
