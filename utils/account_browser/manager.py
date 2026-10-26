"""Own persistent Chromium profiles; all methods run on the web server event loop."""
import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from contextlib import suppress
from pathlib import Path

from playwright.async_api import async_playwright
from utils.manual_captcha import _to_playwright_cookies
from .desktop import Desktop


@dataclass
class AccountBrowser:
    desktop: Desktop
    context: object
    playwright: object
    database_cookie: str
    profile: Path
    owner_id: int
    connected: bool = True

    @property
    def alive(self):
        return self.connected and self.desktop.alive

    async def page(self):
        pages = self.context.pages
        return pages[-1] if pages else await self.context.new_page()

    async def checkpoint(self):
        if not self.connected:
            return
        # Chromium may discard session cookies on clean exit; preserve them explicitly.
        destination = self.profile / 'session-cookies.json'
        temporary = destination.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as output:
            os.chmod(temporary, 0o600)
            json.dump(await self.context.cookies(), output)
        temporary.replace(destination)
        (self.profile / '.initialized').write_text(hashlib.sha256(self.database_cookie.encode()).hexdigest())

    async def close(self):
        try:
            try:
                await self.checkpoint()
            finally:
                await self.context.close()
        finally:
            try:
                await self.playwright.stop()
            finally:
                await self.desktop.close()


class BrowserManager:
    def __init__(self, root=None):
        self.root = Path(root or Path(__file__).resolve().parents[2] / 'data' / 'browser_profiles')
        self.browsers = {}
        self.locks = {}

    def lock(self, account_id):
        return self.locks.setdefault(account_id, asyncio.Lock())

    def profile(self, account_id, owner_id):
        # Database IDs are not filesystem paths, even if supplied by an API client.
        return self.root / hashlib.sha256(f'{owner_id}:{account_id}'.encode()).hexdigest()

    async def ensure(self, account_id, cookies, owner_id):
        """Caller holds the account lock. Existing profiles are never reseeded on restart."""
        browser = self.browsers.get(account_id)
        if browser and browser.alive and browser.owner_id == owner_id:
            return browser
        await self.stop(account_id)
        profile = self.profile(account_id, owner_id)
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = profile / '.initialized'
        desktop = Desktop()
        playwright = context = None
        try:
            await desktop.start()
            playwright = await async_playwright().start()
            context = await playwright.chromium.launch_persistent_context(
                str(profile), headless=False, channel='chromium', no_viewport=True,
                env={**os.environ, 'DISPLAY': desktop.display},
                args=['--no-sandbox', '--disable-dev-shm-usage', '--window-size=1280,900'],
            )
            checkpoint = profile / 'session-cookies.json'
            if checkpoint.exists():
                await context.add_cookies(json.loads(checkpoint.read_text()))
            database_hash = hashlib.sha256(cookies.encode()).hexdigest()
            if not marker.exists() or marker.read_text() != database_hash:
                await context.add_cookies(_to_playwright_cookies(cookies))
                marker.write_text(database_hash)
            browser = AccountBrowser(desktop, context, playwright, cookies, profile, owner_id)
            context.on('close', lambda *_: setattr(browser, 'connected', False))
            page = await browser.page()
            if page.url == 'about:blank':
                # Navigation failures must not destroy the desktop: humans can reload it.
                try:
                    await page.goto('https://www.goofish.com/', wait_until='domcontentloaded', timeout=30000)
                except Exception:
                    pass
            self.browsers[account_id] = browser
            return browser
        except BaseException:
            try:
                if context:
                    with suppress(Exception):
                        await context.close()
                if playwright:
                    with suppress(Exception):
                        await playwright.stop()
            finally:
                await desktop.close()
            raise

    async def stop(self, account_id):
        browser = self.browsers.pop(account_id, None)
        if browser:
            await browser.close()

    async def close(self):
        for account_id in list(self.browsers):
            async with self.lock(account_id):
                # One broken browser must not leak the remaining desktops on shutdown.
                with suppress(Exception):
                    await self.stop(account_id)
