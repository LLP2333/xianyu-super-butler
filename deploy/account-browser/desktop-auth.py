#!/lsiopy/bin/python3
"""Run once at native container startup; replace only desktop authentication."""
import hashlib
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

api = os.environ['DESKTOP_AUTH_API'].rstrip('/')
parsed = urlsplit(api)
if parsed.scheme != 'http' or not re.fullmatch(r'[A-Za-z0-9_.:-]+', parsed.netloc) or parsed.path:
    raise SystemExit('Invalid internal desktop authorization address')
key = os.environ['PASSWORD']
if not re.fullmatch(r'[A-Za-z0-9_-]{32,}', key):
    raise SystemExit('Invalid internal desktop key')
path = Path('/etc/nginx/sites-available/default')
source = path.read_text()
if '# xianyu-desktop-session' in source:
    raise SystemExit(0)
# Keep all original Selkies locations, static files, codecs and input transport.
source = re.sub(r'^\s*auth_basic(?:_user_file)?\s+[^;]+;\s*$', '', source, flags=re.M)
block = '''
  # xianyu-desktop-session
  resolver 127.0.0.11 valid=10s ipv6=off;
  auth_request /_desktop_auth;
  add_header Referrer-Policy no-referrer always;
  location = /_desktop_health { auth_request off; return 204; }
  location = /_desktop_auth {
    internal;
    auth_request off;
    set $desktop_api "API";
    proxy_pass $desktop_api/api/account-browser/desktop-auth;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header X-Desktop-Key "KEY";
    proxy_set_header X-Desktop-Session $cookie_xianyu_desktop;
    proxy_set_header Cookie "";
  }
  location = /_desktop_open {
    auth_request off;
    access_log off;
    set $desktop_api "API";
    proxy_pass $desktop_api/api/account-browser/desktop-entry;
    proxy_set_header X-Desktop-Ticket $arg_ticket;
    proxy_set_header X-Desktop-Key "KEY";
    proxy_set_header Cookie "";
    proxy_redirect off;
    add_header Cache-Control no-store always;
    add_header Referrer-Policy no-referrer always;
  }
'''.replace('API', api).replace('KEY', key)
if source.count('server {') != 2:
    raise SystemExit('Unsupported native desktop nginx layout; refusing to remove authentication')
path.write_text(source.replace('server {', 'server {' + block))
