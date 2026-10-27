"""Observe website-owned Token responses. Never navigate, evaluate JS or send MTOP requests."""
import asyncio
import json
from urllib.parse import parse_qs, urlsplit

from app.config import API_ENDPOINTS
from utils.manual_captcha import _from_playwright_cookies
from utils.xianyu_utils import trans_cookies
from .tokens import BrowserToken, tokens


def token_response(url, request_body, payload):
    endpoint = urlsplit(API_ENDPOINTS['token'])
    actual = urlsplit(url)
    if (actual.scheme, actual.netloc, actual.path.rstrip('/')) != (
            endpoint.scheme, endpoint.netloc, endpoint.path.rstrip('/')):
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('accessToken'), str) or not data['accessToken']:
        return None
    params = parse_qs(request_body or '')
    params = params or parse_qs(actual.query)
    try:
        request = json.loads(params['data'][0])
    except (KeyError, ValueError, TypeError):
        return None
    if not isinstance(request, dict):
        return None
    device, app_key = request.get('deviceId'), request.get('appKey')
    if not isinstance(device, str) or not device or not isinstance(app_key, str) or not app_key:
        return None
    return data['accessToken'], device, app_key


class TokenListener:
    def __init__(self, account_id, expected_account, browser, on_capture=None):
        self.account_id = account_id
        self.expected_account = expected_account
        self.browser = browser
        self.on_capture = on_capture
        self.task = None
        self.ready = asyncio.Event()
        self.error = None
        self.context = None
        self.pending = set()

    async def start(self):
        if self.task and not self.task.done():
            return
        self.ready.clear()
        self.error = None
        self.task = asyncio.create_task(self.run(), name=f'browser-token:{self.account_id}')
        try:
            await asyncio.wait_for(self.ready.wait(), 20)
            if self.error:
                raise RuntimeError(self.error)
        except BaseException:
            await self.close()
            raise

    async def consume(self, response):
        try:
            endpoint = urlsplit(API_ENDPOINTS['token'])
            actual = urlsplit(response.url)
            if (actual.scheme, actual.netloc, actual.path.rstrip('/')) != (
                    endpoint.scheme, endpoint.netloc, endpoint.path.rstrip('/')):
                return
            if response.status != 200:
                return
            # Bind the response to the account that sent the request, not just the
            # current cookie jar (the user may switch accounts while it is in flight).
            headers = await response.request.all_headers()
            request_account = (trans_cookies(headers['cookie']) if headers.get('cookie') else {}).get('unb')
            if not request_account or (self.expected_account and request_account != self.expected_account):
                return
            parsed = token_response(response.url, response.request.post_data, await response.json())
            if not parsed:
                return
            cookies = _from_playwright_cookies(await self.context.cookies([API_ENDPOINTS['token']]))
            if (trans_cookies(cookies) if cookies else {}).get('unb') != request_account:
                tokens.clear(self.account_id)
                return
            token, device, app_key = parsed
            value = BrowserToken(request_account, token, device, app_key, cookies)
            tokens.observe(self.account_id, value)
            candidate = tokens.get(self.account_id, candidate=True)
            if self.on_capture and candidate and candidate.token == value.token:
                await self.on_capture(candidate)
            self.error = None
        except asyncio.CancelledError:
            raise
        except Exception:
            self.error = '网页响应读取失败，请在原生桌面刷新闲鱼消息页'

    def on_response(self, response):
        task = asyncio.create_task(self.consume(response))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def close_goofish_pages(self):
        """End website messaging without closing Chrome or clearing its profile."""
        if self.context is None:
            raise RuntimeError('浏览器监听已断开，无法确认闲鱼标签页已关闭')
        def is_goofish(page):
            host = urlsplit(page.url).hostname or ''
            return host == 'goofish.com' or host.endswith('.goofish.com')

        pages = list(self.context.pages)
        targets = [page for page in pages if is_goofish(page)]
        # Keep Chrome alive even when every existing tab belongs to Goofish.
        if targets and len(targets) == len(pages):
            await self.context.new_page()
        for page in targets:
            await page.close(run_before_unload=False)
        if any(is_goofish(page) for page in self.context.pages):
            raise RuntimeError('闲鱼标签页未全部关闭，账号保持暂停')

    async def current_account(self):
        if not self.context or not self.task or self.task.done():
            raise ValueError('网页 Token 监听未连接，请重新打开浏览器入口')
        cookies = _from_playwright_cookies(await self.context.cookies([API_ENDPOINTS['token']]))
        account = (trans_cookies(cookies) if cookies else {}).get('unb')
        if self.expected_account and account != self.expected_account:
            tokens.clear(self.account_id)
        return account

    async def run(self):
        try:
            async with self.browser.connect() as context:
                self.context = context
                context.on('response', self.on_response)
                self.ready.set()
                while True:
                    await asyncio.sleep(5)
                    # Read-only health/identity check also invalidates credentials after logout.
                    await self.current_account()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.error = '网页 Token 监听已断开，请重新打开浏览器入口'
        finally:
            self.ready.set()
            tokens.discard_unadopted(self.account_id)
            if self.context:
                self.context.remove_listener('response', self.on_response)
            for task in self.pending:
                task.cancel()
            await asyncio.gather(*self.pending, return_exceptions=True)
            self.pending.clear()
            self.context = None

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
