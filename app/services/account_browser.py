"""Account takeover, credential confirmation and browser reconciliation."""
import asyncio
from contextlib import suppress
from urllib.parse import urlsplit

from loguru import logger
from utils.account_browser.manager import BrowserManager
from utils.manual_captcha import request_manual_token, _from_playwright_cookies, _to_playwright_cookies
from utils.xianyu_utils import trans_cookies, drop_stale_captcha_challenge
from utils import risk_control


class AccountBrowserService:
    def __init__(self, db, account_manager, browsers=None):
        self.db = db
        self.account_manager = account_manager
        self.browsers = browsers or BrowserManager()
        self.manual = set()
        self.viewers = {}
        self.errors = {}
        self.task = None

    def account(self, account_id, user_id):
        record = self.db.get_cookie_details(account_id)
        if not record or record['user_id'] != user_id:
            raise LookupError('账号不存在或无权访问')
        return record

    async def disconnect(self, account_id):
        viewer = self.viewers.pop(account_id, None)
        if viewer:
            socket, task = viewer
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            with suppress(Exception):
                await socket.close(code=1000)

    async def takeover(self, account_id, user_id):
        async with self.browsers.lock(account_id):
            record = self.account(account_id, user_id)
            if not self.db.get_cookie_status(account_id):
                raise ValueError('请先启用账号，再打开浏览器')
            manager = self.account_manager()
            if manager is None:
                raise RuntimeError('账号管理尚未就绪')
            first = account_id not in self.manual
            try:
                if first:
                    self.manual.add(account_id)
                    await manager.pause_cookie_task(account_id)
                await self.browsers.ensure(account_id, record['value'], user_id)
                self.errors.pop(account_id, None)
            except BaseException:
                if first:
                    await manager.resume_cookie_task(account_id, record['value'])
                    self.manual.discard(account_id)
                raise
            return {'manual': True, 'message': '已接管账号浏览器，自动任务已暂停。关闭窗口不会结束接管。'}

    async def cookies(self, account_id):
        from app.config import API_ENDPOINTS
        browser = self.browsers.browsers[account_id]
        return drop_stale_captcha_challenge(_from_playwright_cookies(
            await browser.context.cookies([API_ENDPOINTS['token']])))

    async def challenge(self, account_id, user_id):
        async with self.browsers.lock(account_id):
            record, browser = self.require_manual(account_id, user_id)
            result = await request_manual_token(await self.cookies(account_id))
            await browser.context.add_cookies(_to_playwright_cookies(result['cookies_str']))
            if result['token']:
                return {'message': '登录凭证可用，请点击“确认登录并恢复”'}
            url = urlsplit(result['url'])
            if url.scheme != 'https' or not any(
                url.hostname == domain or (url.hostname or '').endswith('.' + domain)
                for domain in ('goofish.com', 'taobao.com', 'alibaba.com', 'alicdn.com')
            ):
                raise ValueError(f"未取得可用验证链接：{result['reason']}。可在浏览器内登录或刷新页面。")
            page = await browser.page()
            await page.goto(result['url'], wait_until='domcontentloaded', timeout=30000)
            await page.bring_to_front()
            return {'message': '验证页已在当前浏览器中打开，完成后请确认登录并恢复'}

    def require_manual(self, account_id, user_id):
        record = self.account(account_id, user_id)
        browser = self.browsers.browsers.get(account_id)
        if account_id not in self.manual or not browser or not browser.alive:
            raise ValueError('请先打开账号浏览器；若浏览器已退出，请重新连接')
        return record, browser

    async def finish(self, account_id, user_id, save=True):
        async with self.browsers.lock(account_id):
            record = self.account(account_id, user_id)
            if account_id not in self.manual:
                raise ValueError('账号当前未处于接管状态')
            # End input before inspecting credentials, avoiding edits during Token confirmation.
            await self.disconnect(account_id)
            cookies = record['value']
            if save:
                _, browser = self.require_manual(account_id, user_id)
                cookies = await self.cookies(account_id)
                expected = trans_cookies(record['value']).get('unb')
                actual = trans_cookies(cookies).get('unb')
                if not expected or actual != expected:
                    raise ValueError('浏览器登录的闲鱼账号与当前账号不一致，请登录原账号后重试')
                result = await request_manual_token(cookies)
                if not result['token']:
                    raise ValueError(f"Token 确认失败：{result['reason']}，仍保持接管，可重新连接继续操作")
                cookies = result['cookies_str']
                if not self.db.update_cookie_account_info(account_id, cookie_value=cookies):
                    raise RuntimeError('Cookie 保存失败，账号仍保持接管')
                await browser.context.add_cookies(_to_playwright_cookies(cookies))
                browser.database_cookie = cookies
                risk_control.registry.get(account_id).reset()
            manager = self.account_manager()
            if manager is None:
                raise RuntimeError('账号管理尚未就绪，账号仍保持接管')
            await manager.resume_cookie_task(account_id, cookies)
            self.manual.discard(account_id)
            return {'message': 'Token 已确认，Cookie 已保存，账号任务已恢复' if save else '已结束接管，使用原凭证恢复账号任务'}

    async def reconcile(self):
        records = self.db.get_all_cookies()
        for account_id in set(records) | set(self.browsers.browsers) | self.manual:
            async with self.browsers.lock(account_id):
                try:
                    enabled = account_id in records and self.db.get_cookie_status(account_id)
                    if not enabled:
                        await self.disconnect(account_id)
                        await self.browsers.stop(account_id)
                        if account_id in self.manual:
                            manager = self.account_manager()
                            if manager:
                                await manager.release_browser_pause(account_id)
                            self.manual.discard(account_id)
                        self.errors.pop(account_id, None)
                        continue
                    record = self.db.get_cookie_details(account_id)
                    if not record:
                        continue
                    browser = await self.browsers.ensure(account_id, records[account_id], record['user_id'])
                    if account_id not in self.manual and browser.database_cookie != records[account_id]:
                        await browser.context.add_cookies(_to_playwright_cookies(records[account_id]))
                        browser.database_cookie = records[account_id]
                    await browser.checkpoint()
                    self.errors.pop(account_id, None)
                except Exception as exc:
                    message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                    if self.errors.get(account_id) != message:
                        logger.warning(f'账号浏览器 {account_id}: {message}')
                    self.errors[account_id] = message

    async def run(self):
        while True:
            try:
                await self.reconcile()
            except Exception as exc:
                logger.warning(f'账号浏览器管理异常: {type(exc).__name__}')
            await asyncio.sleep(10)

    def start(self):
        self.task = asyncio.create_task(self.run(), name='account-browsers')

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for account_id in list(self.viewers):
            await self.disconnect(account_id)
        await self.browsers.close()
