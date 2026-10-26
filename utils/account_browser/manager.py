"""Account-owned original LinuxServer desktops and read-only CDP connections."""
import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from playwright.async_api import async_playwright

from .containers import ContainerDesktop, ContainerPool


@dataclass
class AccountBrowser:
    desktop: ContainerDesktop
    owner_id: int

    @property
    def alive(self):
        return self.desktop.alive

    @asynccontextmanager
    async def connect(self):
        """Attach temporarily, leaving LinuxServer's Chrome and profile alive on detach."""
        async with aiohttp.ClientSession(auth=self.desktop.auth, timeout=aiohttp.ClientTimeout(total=10)) as client:
            async with client.get(f'http://{self.desktop.host}:9223/json/version') as response:
                response.raise_for_status()
                payload = await response.json()
        endpoint = urlsplit(payload['webSocketDebuggerUrl'])
        endpoint = urlunsplit(('ws', f'{self.desktop.host}:9223', endpoint.path, '', ''))
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(
                endpoint, headers={'Authorization': self.desktop.auth.encode()}, timeout=15000)
            if not browser.contexts:
                raise RuntimeError('Chrome 尚未创建账号会话')
            # Stopping Playwright only detaches CDP. Never close the native context.
            yield browser.contexts[0]

    async def close(self):
        # Let Chrome flush its profile before the desktop/compositor is stopped.
        # CDP Browser.close is intentional here; browser.close() would only detach.
        with suppress(Exception):
            async with asyncio.timeout(8):
                async with self.connect() as context:
                    session = await context.browser.new_browser_cdp_session()
                    await session.send('Browser.close')
                await self.desktop.wait_for_chrome_exit()
        await self.desktop.stop()


class BrowserManager:
    def __init__(self, root=None, **pool_options):
        root = root or Path(__file__).resolve().parents[2] / 'data' / 'account_browsers'
        self.pool = ContainerPool(root, **pool_options)
        self.browsers = {}
        self.locks = {}
        self.loaded = False
        self.checked = set()
        self.discovery_lock = asyncio.Lock()

    def lock(self, account_id):
        return self.locks.setdefault(account_id, asyncio.Lock())

    async def discover(self):
        async with self.discovery_lock:
            if not self.loaded:
                for account_id, owner_id, desktop in await self.pool.discover():
                    await desktop.refresh()
                    self.browsers[account_id] = AccountBrowser(desktop, owner_id)
                self.loaded = True

    async def ensure(self, account_id, owner_id):
        """Caller owns the account lock. Chrome owns its cookies; never seed old ones."""
        await self.discover()
        browser = self.browsers.get(account_id)
        if (browser and account_id in self.checked and browser.owner_id == owner_id
                and await browser.desktop.refresh()):
            return browser
        await self.stop(account_id)
        desktop = await self.pool.ensure(account_id, owner_id)
        browser = AccountBrowser(desktop, owner_id)
        self.browsers[account_id] = browser
        self.checked.add(account_id)
        return browser

    async def stop(self, account_id):
        browser = self.browsers.pop(account_id, None)
        self.checked.discard(account_id)
        if browser:
            await browser.close()

    async def close(self):
        async def stop_one(account_id):
            async with self.lock(account_id):
                with suppress(Exception):
                    await self.stop(account_id)
        await asyncio.gather(*(stop_one(account_id) for account_id in list(self.browsers)))
        await self.pool.close()
