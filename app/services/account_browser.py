"""One native browser: onboarding, passive credentials, resume and explicit reset."""
import asyncio
from loguru import logger
from utils.account_browser.manager import BrowserManager
from utils.account_browser.listener import TokenListener
from utils.account_browser.tokens import tokens
from utils.account_browser.credentials import cookie_expired, invalidate_credentials, WAITING
from utils.xianyu_utils import trans_cookies
from utils import risk_control

PRIMARY = 'primary'
PENDING = 'browser-onboarding'


class AccountBrowserService:
    def __init__(self, db, account_manager, browsers=None):
        self.db = db
        self.account_manager = account_manager
        self.browsers = browsers or BrowserManager()
        self.manual = set()
        self.listener = None
        self.pending_owner = None
        self.created_id = None
        self.setup_error = None
        self.errors = {}
        self.task = None
        self.opening_messages = set()

    def account(self, account_id, user_id):
        record = self.db.get_cookie_details(account_id)
        if not record or record['user_id'] != user_id:
            raise LookupError('账号不存在或无权访问')
        if len(self.db.get_all_cookies()) > 1:
            raise ValueError('当前为单账号模式，请先保留一个账号')
        return record

    def desktop(self, user_id=None):
        browser = self.browsers.browsers.get(PRIMARY)
        if not browser or not browser.alive or (user_id is not None and browser.owner_id != user_id):
            raise LookupError('浏览器未打开或无权访问')
        return browser

    def desktop_url(self, user_id):
        from utils.account_browser.address import validate_desktop_address
        browser = self.desktop(user_id)
        configured = self.db.get_system_setting('browser_desktop_url') or ''
        return validate_desktop_address(configured) or browser.desktop.desktop_url

    def browser_stopped(self):
        return self.db.get_system_setting('account_browser_stopped') == 'true'

    def set_browser_stopped(self, stopped):
        if not self.db.set_system_setting('account_browser_stopped', 'true' if stopped else 'false'):
            raise RuntimeError('浏览器启停状态保存失败')

    async def stop_browser(self, user_id):
        async with self.browsers.lock(PRIMARY):
            records = self.db.get_all_cookies()
            if records:
                self.account(next(iter(records)), user_id)
            elif self.pending_owner != user_id:
                raise LookupError('没有属于当前用户的浏览器')
            existing = self.browsers.browsers.get(PRIMARY)
            if existing and existing.owner_id != user_id:
                raise LookupError('无权停止其他用户的浏览器')
            self.set_browser_stopped(True)
            await self.stop()
            return {**self.listener_status(), 'message': '浏览器已停止，登录数据和已采用的 Token 已保留，管家消息任务继续运行。'}

    async def stop(self):
        if self.listener:
            await self.listener.close()
            self.listener = None
        await self.browsers.stop(PRIMARY)

    async def ensure(self, owner, account_id=None, cookies=None):
        browser = await self.browsers.ensure(PRIMARY, owner)
        expected = trans_cookies(cookies).get('unb') if cookies else None
        key = account_id or PENDING
        if self.listener and (self.listener.browser is not browser or self.listener.account_id != key):
            await self.listener.close()
            self.listener = None
        if self.listener is None:
            self.listener = TokenListener(key, expected, browser, self.capture_credentials)
        await self.listener.start()
        return browser

    async def setup(self, user_id):
        async with self.browsers.lock(PRIMARY):
            if self.db.get_all_cookies():
                raise ValueError('当前为单账号模式，已有账号，请打开该账号的浏览器')
            if self.pending_owner is not None and self.pending_owner != user_id:
                raise ValueError('其他管理员正在添加账号')
            existing = self.browsers.browsers.get(PRIMARY)
            if existing and existing.owner_id != user_id:
                raise ValueError('浏览器属于其他管理员，无法使用其登录资料')
            self.pending_owner = user_id
            self.created_id = None
            self.setup_error = None
            self.set_browser_stopped(False)
            browser = await self.ensure(user_id)
            return {'message': '请在原生浏览器登录闲鱼并打开消息页，系统会自动捕获 Cookie 和消息 Token 并添加账号。',
                    'desktop_url': self.desktop_url(user_id)}

    async def pause_account(self, account_id, reason=WAITING, force=False):
        invalidate_credentials(account_id, reason)
        manager = self.account_manager()
        if manager is None:
            raise RuntimeError('账号管理尚未就绪')
        if force or account_id not in self.manual:
            await manager.pause_cookie_task(account_id)
            self.manual.add(account_id)

    async def capture_credentials(self, value):
        async with self.browsers.lock(PRIMARY):
            listener = self.listener
            if not listener or tokens.get(listener.account_id, candidate=True) is not value:
                return
            account_id = listener.account_id
            try:
                if await listener.current_account() != value.account or cookie_expired(value.cookies):
                    raise ValueError('网页账号不匹配或 Cookie 已过期，请重新登录')
                manager = self.account_manager()
                if manager is None:
                    raise RuntimeError('账号管理尚未就绪，请稍后刷新闲鱼消息页')
                if account_id == PENDING:
                    if self.pending_owner is None or not value.account.isdecimal():
                        return
                    self.db.create_browser_account(value.account, value.cookies, self.pending_owner)
                    account_id = value.account
                    listener.account_id = account_id
                    listener.expected_account = value.account
                    tokens.clear(PENDING)
                    tokens.observe(account_id, value)
                    self.created_id = account_id
                    self.pending_owner = None
                else:
                    record = self.account(account_id, listener.browser.owner_id)
                    if not self.db.get_cookie_status(account_id):
                        return
                    if trans_cookies(record['value']).get('unb') != value.account:
                        raise ValueError('网页账号与已有账号不匹配，未保存')
                    if tokens.get(account_id) is value and not risk_control.registry.get(account_id).verification_required:
                        return
                    await self.pause_account(account_id, '已捕获新凭据，正在保存并恢复')
                    if not self.db.update_cookie_account_info(account_id, cookie_value=value.cookies):
                        raise RuntimeError('登录状态保存失败，账号仍保持暂停')
                tokens.approve(account_id, value)
                await listener.close_goofish_pages()
                risk_control.registry.get(account_id).reset(verified=True)
                await manager.resume_cookie_task(account_id, value.cookies)
                self.manual.discard(account_id)
                self.errors.pop(account_id, None)
                self.setup_error = None
            except asyncio.CancelledError:
                if account_id != PENDING:
                    await self.pause_account(account_id, '凭据采用被中断，等待网页重新获取', force=True)
                raise
            except Exception as exc:
                message = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else '凭据保存或任务启动失败，请刷新闲鱼消息页重试'
                if account_id != PENDING:
                    await self.pause_account(account_id, message, force=True)
                    self.errors[account_id] = message
                else:
                    self.setup_error = message

    def credential_status(self, account_id):
        value = tokens.get(account_id)
        candidate = tokens.get(account_id, candidate=True)
        guard = risk_control.registry.get(account_id)
        active = bool(value and not cookie_expired(value.cookies) and not guard.verification_required and account_id not in self.manual)
        error = self.errors.get(account_id) or self.setup_error
        manager = self.account_manager()
        instance = manager.instances.get(account_id) if manager else None
        connection = getattr(instance, 'connection_state', None)
        ws = getattr(instance, 'ws', None)
        connected = bool(active and getattr(connection, 'value', connection) == 'connected'
                         and ws is not None and not ws.closed
                         and getattr(instance, 'current_token', None) == value.token)
        state = 'error' if error else 'active' if connected else 'connecting' if active else 'captured' if candidate else 'waiting'
        return {'credentials_state': state, 'captured': bool(candidate),
                'captured_at': candidate.received_at if candidate else None,
                'message': error or ('消息认证成功，管家已连接' if connected else
                           '凭据已保存，闲鱼标签页已关闭，管家正在连接并验证消息服务' if active else
                           guard.last_hit_reason or '等待你在闲鱼网页登录并打开消息页')}

    def listener_status(self):
        listener = self.listener
        running = bool(listener and listener.task and not listener.task.done())
        connected = bool(running and listener.ready.is_set() and listener.context is not None)
        browser = self.browsers.browsers.get(PRIMARY)
        return {'browser_running': bool(browser and browser.alive), 'listening': connected,
                'listener_state': 'listening' if connected else 'connecting' if running and not listener.ready.is_set() else 'disconnected',
                'listener_error': listener.error if listener else None}

    async def reconnect(self, user_id):
        async with self.browsers.lock(PRIMARY):
            if self.browser_stopped():
                raise ValueError('浏览器已停止，请先启动浏览器')
            self.desktop(user_id)
            records = self.db.get_all_cookies()
            account_id = next(iter(records), None)
            record = self.account(account_id, user_id) if account_id else None
            if not record and self.pending_owner != user_id:
                raise LookupError('没有待添加的账号')
            if record and not self.db.get_cookie_status(account_id):
                raise ValueError('请先启用账号')
            if record:
                await self.pause_account(account_id, '监听重新连接，等待网页提供新凭据')
            if self.listener:
                await self.listener.close()
                self.listener = None
            await self.ensure(user_id, account_id, record['value'] if record else None)
            return {**self.listener_status(), 'message': '监听已重新连接，请打开或刷新闲鱼消息页',
                    'desktop_url': self.desktop_url(user_id)}

    async def setup_status(self, user_id):
        if self.created_id:
            self.account(self.created_id, user_id)
        elif self.pending_owner != user_id:
            raise LookupError('没有待添加的账号')
        key = self.created_id or PENDING
        return {**self.listener_status(), **self.credential_status(key), 'account_id': self.created_id,
                'desktop_url': None if self.browser_stopped() else self.desktop_url(user_id)}

    async def takeover(self, account_id, user_id, restart=False):
        async with self.browsers.lock(PRIMARY):
            record = self.account(account_id, user_id)
            if not self.db.get_cookie_status(account_id):
                raise ValueError('请先启用账号，再打开浏览器')
            value = tokens.get(account_id)
            if restart or not value or cookie_expired(value.cookies) or risk_control.registry.get(account_id).verification_required:
                await self.pause_account(account_id)
            if restart:
                await self.stop()
            self.set_browser_stopped(False)
            await self.ensure(user_id, account_id, record['value'])
            return {**self.listener_status(), **self.credential_status(account_id),
                    'desktop_url': self.desktop_url(user_id)}

    async def fetch_credentials(self, account_id, user_id):
        self.account(account_id, user_id)
        if not self.db.get_cookie_status(account_id):
            raise ValueError('请先启用账号')
        if account_id in self.opening_messages:
            raise ValueError('正在打开闲鱼消息，请勿重复点击')
        self.opening_messages.add(account_id)
        try:
            async with self.browsers.lock(PRIMARY):
                record = self.account(account_id, user_id)
                if not self.db.get_cookie_status(account_id):
                    raise ValueError('请先启用账号')
                await self.pause_account(account_id, '正在打开闲鱼首页并点击消息，等待网页提供凭据', force=True)
                self.errors.pop(account_id, None)
                self.set_browser_stopped(False)
                try:
                    await self.ensure(user_id, account_id, record['value'])
                    await self.listener.open_messages()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.errors[account_id] = '未能进入闲鱼消息页，请打开原生 Chrome 桌面检查登录或验证；页面已保留'
                return {**self.listener_status(), **self.credential_status(account_id),
                        'desktop_url': self.desktop_url(user_id) if self.listener_status()['browser_running'] else None}
        finally:
            self.opening_messages.discard(account_id)

    async def status(self, account_id, user_id):
        self.account(account_id, user_id)
        return {**self.listener_status(), **self.credential_status(account_id),
                'desktop_url': self.desktop_url(user_id) if self.listener_status()['browser_running'] else None}

    async def clear(self, user_id):
        async with self.browsers.lock(PRIMARY):
            existing = self.browsers.browsers.get(PRIMARY)
            if existing and existing.owner_id != user_id:
                raise LookupError('无权清空其他管理员的浏览器')
            records = self.db.get_all_cookies()
            if len(records) > 1:
                raise ValueError('当前为单账号模式，请先保留一个账号')
            account_id = next(iter(records), None)
            if account_id:
                record = self.account(account_id, user_id)
                manager = self.account_manager()
                if manager is None:
                    raise RuntimeError('账号管理尚未就绪')
                await self.pause_account(account_id, '浏览器数据已清空，等待重新登录')
                identity = trans_cookies(record['value']).get('unb')
                if not identity or not self.db.update_cookie_account_info(account_id, cookie_value=f'unb={identity}'):
                    raise RuntimeError('登录凭证清除失败，未清空浏览器')
                tokens.clear(account_id)
            elif self.pending_owner not in (None, user_id):
                raise LookupError('无权清空其他管理员的浏览器')
            existing = self.browsers.browsers.get(PRIMARY)
            if existing and existing.owner_id != user_id:
                raise LookupError('无权清空其他管理员的浏览器')
            await self.stop()
            self.set_browser_stopped(False)
            await self.browsers.pool.clear_profile(user_id)
            self.created_id = None
            self.setup_error = None
            if account_id:
                browser = await self.ensure(user_id, account_id, f'unb={identity}')
            else:
                self.pending_owner = user_id
                browser = await self.ensure(user_id)
            return {'message': '浏览器数据和旧登录凭证已清空，请重新登录；账号设置保留。',
                    'desktop_url': self.desktop_url(user_id)}

    async def reconcile(self):
        await self.browsers.discover()
        async with self.browsers.lock(PRIMARY):
            records = self.db.get_all_cookies()
            if len(records) > 1:
                await self.stop()
                return
            if not records:
                if self.pending_owner is not None and not self.browser_stopped():
                    await self.ensure(self.pending_owner)
                elif self.listener:
                    await self.stop()
                    self.manual.clear()
                return
            account_id = next(iter(records))
            if not self.db.get_cookie_status(account_id):
                await self.stop()
                return
            record = self.db.get_cookie_details(account_id)
            if record:
                value = tokens.get(account_id)
                if not value or cookie_expired(value.cookies) or risk_control.registry.get(account_id).verification_required:
                    await self.pause_account(account_id)
                if self.browser_stopped():
                    if self.listener or self.browsers.browsers.get(PRIMARY):
                        await self.stop()
                    return
                await self.ensure(record['user_id'], account_id, record['value'])

    async def run(self):
        failures = 0
        while True:
            try:
                await self.reconcile()
                failures = 0
            except Exception as exc:
                failures += 1
                logger.warning(f'账号浏览器管理异常: {type(exc).__name__}: {exc}')
            await asyncio.sleep(min(60, 2 ** min(failures + 1, 6)))

    def start(self):
        self.task = asyncio.create_task(self.run(), name='account-browser')

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.listener:
            await self.listener.close()
        await self.browsers.close()
