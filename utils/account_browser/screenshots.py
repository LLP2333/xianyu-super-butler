"""Screenshots of the native browser when opening Goofish messages fails, kept out of public static files."""
import re
import time
from pathlib import Path
from loguru import logger

# logs/ is never served; the page may show nicknames, chats and orders.
SCREENSHOT_DIR = Path(__file__).resolve().parents[2] / 'logs' / 'renewal'
KEEP = 10
TIMEOUT = 5000
NAME = re.compile(r'^renewal_[A-Za-z0-9_-]+_\d+\.jpg$')


async def save(page, account_id):
    """Return the saved file name, or None: a failed screenshot never changes the failure handling."""
    try:
        if page is None or page.is_closed():
            return None
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r'[^A-Za-z0-9_-]', '_', str(account_id))
        name = f'renewal_{safe}_{int(time.time() * 1000)}.jpg'
        await page.screenshot(path=str(SCREENSHOT_DIR / name), type='jpeg', quality=70, timeout=TIMEOUT)
    except Exception as exc:
        logger.warning(f'【{account_id}】保存续期失败截图失败: {type(exc).__name__}: {exc}')
        return None
    prune()
    return name


def prune():
    try:
        files = sorted(SCREENSHOT_DIR.glob('renewal_*.jpg'), key=lambda file: file.stat().st_mtime, reverse=True)
        for old in files[KEEP:]:
            old.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(f'清理续期失败截图失败: {exc}')


def path(name):
    """Resolve a stored name to its file, refusing anything outside the screenshot folder."""
    if not isinstance(name, str) or not NAME.match(name):
        return None
    file = SCREENSHOT_DIR / name
    return file if file.is_file() else None


def remove(name):
    file = path(name)
    if file:
        file.unlink(missing_ok=True)
