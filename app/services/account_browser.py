"""One native browser: onboarding, passive credentials, resume and explicit reset."""
import asyncio
from loguru import logger
from utils.account_browser.manager import BrowserManager
from utils.account_browser.listener import TokenListener
from utils.account_browser.tokens import tokens
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
            self.listener = TokenListener(key, expected, browser, self.capture_new_account)
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
            browser = await self.ensure(user_id)
            return {'message': '请在原生浏览器登录闲鱼并打开消息页，系统会自动捕获 Cookie 和消息 Token 并添加账号。',
                    'desktop_url': self.desktop_url(user_id)}

    async def capture_new_account(self, value):
        if self.pending_owner is None:
            return
        async with self.browsers.lock(PRIMARY):
            if self.pending_owner is None or not self.listener or self.listener.account_id != PENDING:
                return
            try:
                manager = self.account_manager()
                if manager is None:
                    raise RuntimeError('账号管理尚未就绪，请稍后刷新闲鱼消息页')
                if not value.account.isdecimal():
                    raise ValueError('网页账号身份无效，未保存')
                # Request identity and current cookie identity were checked by the listener.
                self.db.create_browser_account(value.account, value.cookies, self.pending_owner)
                self.listener.account_id = value.account
                self.listener.expected_account = value.account
                tokens.clear(PENDING)
                tokens.observe(value.account, value)
                tokens.approve(value.account, value)
                self.created_id = value.account
                self.pending_owner = None
                risk_control.registry.get(value.account).reset(verified=True)
                await manager.resume_cookie_task(value.account, value.cookies)
                self.setup_error = None
            except Exception as exc:
                if self.created_id:
                    self.manual.add(self.created_id)
                    tokens.pause(self.created_id)
                self.setup_error = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else '账号保存或启动失败，请重试'

    async def setup_status(self, user_id):
        browser = self.desktop(user_id)
        return {'account_id': self.created_id, 'message': self.setup_error or
                ('账号已添加，消息连接结果请查看账号状态' if self.created_id else
                 (self.listener.error if self.listener else None) or '等待你在闲鱼网页登录并打开消息页'),
                'desktop_url': self.desktop_url(user_id)}

    async def takeover(self, account_id, user_id, restart=False):
        async with self.browsers.lock(PRIMARY):
            record = self.account(account_id, user_id)
            if not self.db.get_cookie_status(account_id):
                raise ValueError('请先启用账号，再打开浏览器')
            manager = self.account_manager()
            if manager is None:
                raise RuntimeError('账号管理尚未就绪')
            if account_id not in self.manual:
                await manager.pause_cookie_task(account_id)
                self.manual.add(account_id)
                tokens.pause(account_id)
            if restart:
                await self.stop()
            browser = await self.ensure(user_id, account_id, record['value'])
            return {'manual': True, 'message': '监听已就绪。请在原生浏览器完成验证并打开或刷新闲鱼消息页。',
                    'desktop_url': self.desktop_url(user_id)}

    async def status(self, account_id, user_id):
        record = self.account(account_id, user_id)
        listener = self.listener
        connected = bool(listener and listener.account_id == account_id and listener.task and not listener.task.done() and listener.ready.is_set())
        value = tokens.get(account_id, candidate=True)
        return {'manual': account_id in self.manual, 'listening': connected,
                'captured': bool(value and value.account == trans_cookies(record['value']).get('unb')),
                'message': self.errors.get(account_id) or (listener.error if listener else None) or
                ('已捕获网页消息 Token，可确认并恢复' if value else '等待网页消息 Token，请打开或刷新闲鱼消息页')}

    async def finish(self, account_id, user_id, save=True):
        async with self.browsers.lock(PRIMARY):
            record = self.account(account_id, user_id)
            if account_id not in self.manual:
                raise ValueError('账号当前未处于人工操作状态')
            manager = self.account_manager()
            if manager is None:
                raise RuntimeError('账号管理尚未就绪')
            cookies = record['value']
            if save:
                expected = trans_cookies(cookies).get('unb')
                if not self.listener or await self.listener.current_account() != expected:
                    raise ValueError('请在原生浏览器登录当前账号，等待网页获取消息 Token')
                value = tokens.get(account_id, candidate=True)
                if not value or value.account != expected:
                    raise ValueError('尚未捕获当前账号的有效网页 Token，请打开或刷新闲鱼消息页')
                if not self.db.update_cookie_account_info(account_id, cookie_value=value.cookies):
                    raise RuntimeError('登录状态保存失败，账号仍保持暂停')
                cookies = value.cookies
                tokens.approve(account_id, value)
                risk_control.registry.get(account_id).reset(verified=True)
            else:
                tokens.resume(account_id)
            try:
                await manager.resume_cookie_task(account_id, cookies)
            except BaseException:
                tokens.pause(account_id)
                raise
            self.manual.discard(account_id)
            return {'message': '已采用网页 Token 并恢复账号任务，请查看消息连接状态' if save else '已结束操作，未更新登录状态；无可用 Token 时等待网页授权'}

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
                await manager.pause_cookie_task(account_id)
                self.manual.add(account_id)
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
                if self.pending_owner is not None:
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
                await self.ensure(record['user_id'], account_id, record['value'])
                self.errors.pop(account_id, None)

    async def run(self):
        while True:
            try:
                await self.reconcile()
            except Exception as exc:
                message = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
                logger.warning(f'账号浏览器管理异常: {message}')
            await asyncio.sleep(10)

    def start(self):
        self.task = asyncio.create_task(self.run(), name='account-browser')

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.listener:
            await self.listener.close()
        await self.browsers.close()
