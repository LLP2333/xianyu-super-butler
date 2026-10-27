"""Validate the public native desktop origin; an empty value selects local access."""
from urllib.parse import urlsplit


def validate_desktop_address(value):
    value = value.strip()
    if not value:
        return ''
    try:
        url = urlsplit(value)
        valid = (url.scheme in ('http', 'https') and url.hostname and not url.username and not url.password
                 and url.path in ('', '/') and not url.query and not url.fragment
                 and not any(c.isspace() for c in value) and not any(c in value for c in ('\\', '<', '>', '?', '#')))
        url.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('浏览器地址必须为 HTTP 或 HTTPS 根地址，例如 https://browser.example.com，不包含路径、账号密码或查询参数')
    return value.rstrip('/') + '/'
