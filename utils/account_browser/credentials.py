"""Gate account requests until native-browser credentials have been adopted."""
from contextlib import asynccontextmanager
import json
import time

from utils import risk_control
from utils.xianyu_utils import trans_cookies
from .tokens import tokens

WAITING = '登录凭据不可用，已暂停闲鱼请求；请在原生浏览器登录并打开消息页'


def cookie_expired(cookies):
    value = trans_cookies(cookies).get('_m_h5_tk', '') if cookies else ''
    _, separator, deadline = value.rpartition('_')
    return bool(separator and deadline.isdecimal() and int(deadline) <= time.time() * 1000)


def invalidate_credentials(account_id, reason, value=None):
    # A late response from the old lifecycle cannot revoke freshly adopted credentials.
    current = tokens.get(account_id)
    if value is not None and current is not None and current is not value:
        return
    if value is not None:
        tokens.reject(account_id, value.token)
    else:
        tokens.pause(account_id)
    guard = risk_control.registry.get(account_id)
    if not guard.verification_required or guard.last_hit_reason != reason:
        guard.require_verification(reason)


def require_credentials(account_id, cookies=None):
    value = tokens.get(account_id)
    guard = risk_control.registry.get(account_id)
    if not value or cookie_expired(value.cookies):
        invalidate_credentials(account_id, WAITING, value)
    if guard.is_blocked or not value:
        raise risk_control.RiskControlBlocked(account_id, guard.remaining_seconds, guard.last_hit_reason or WAITING)
    if cookies:
        supplied = trans_cookies(cookies)
        active = trans_cookies(value.cookies)
        if supplied.get('unb') != value.account or (supplied.get('_m_h5_tk') != active.get('_m_h5_tk')):
            raise risk_control.RiskControlBlocked(account_id, 0, '旧请求的登录状态已被替换，请使用新凭据')
    return value


def credential_error(payload):
    if not isinstance(payload, dict):
        return False
    ret = payload.get('ret') or []
    if isinstance(ret, str):
        ret = [ret]
    markers = ('TOKEN_EXPIRED', 'TOKEN_EXOIRED', 'TOKEN_EMPTY', 'TOKEN_INVALID',
               'SESSION_EXPIRED', 'SESSION_INVALID', 'USER_VALIDATE', 'ILLEGAL_ACCESS',
               '令牌过期', 'Session过期', '登录失效', '请先登录')
    data = payload.get('data')
    challenge = data.get('url', '') if isinstance(data, dict) else ''
    return (any(any(marker in str(item) for marker in markers) for item in ret)
            or isinstance(challenge, str) and any(marker in challenge for marker in ('punish', 'captcha', 'validate')))


@asynccontextmanager
async def platform_request(session, account_id, method, url, *, cookies, **kwargs):
    """Check immediately before sending, inspect rejection before callers can retry."""
    value = require_credentials(account_id, cookies)
    async with getattr(session, method)(url, **kwargs) as response:
        try:
            payload = await response.json(content_type=None)
        except (ValueError, UnicodeError):
            payload = None
        if response.status in (401, 403) or credential_error(payload):
            reason = '闲鱼拒绝当前登录凭据，已停止请求；请在原生浏览器重新登录或验证'
            invalidate_credentials(account_id, reason, value)
            raise risk_control.RiskControlBlocked(account_id, 0, reason)
        if risk_control.is_risk_control_error(json.dumps(payload, ensure_ascii=False)):
            reason = '闲鱼要求验证或限制请求，已暂停；请在原生浏览器完成验证'
            invalidate_credentials(account_id, reason, value)
            raise risk_control.RiskControlBlocked(account_id, 0, reason)
        # A request already in flight may finish after another request revoked credentials.
        if tokens.get(account_id) is not value or risk_control.registry.get(account_id).verification_required:
            raise risk_control.RiskControlBlocked(account_id, 0, WAITING)
        yield response
