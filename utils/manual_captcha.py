"""Cookie format helpers for browser diagnostics."""

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
