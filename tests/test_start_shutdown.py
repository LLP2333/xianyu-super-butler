"""A real SIGTERM must reach the threaded API's ASGI shutdown lifecycle."""
import ast
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class ShutdownTests(unittest.TestCase):
    def test_sigterm_finishes_api_shutdown(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'Start.py').read_text())
        definitions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and node.name in ('_start_api_server', 'main')]
        source = ast.unparse(ast.Module(body=definitions, type_ignores=[]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'shutdown.py'
            script.write_text('''import asyncio, threading, signal, os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from fastapi import FastAPI
from loguru import logger
import uvicorn
AUTO_REPLY = {}
COOKIES_LIST = []
class Manager:
    def __init__(self, loop):
        self.cookies = {}
        self.tasks = {}
    def list_cookies(self):
        return {}
cm = SimpleNamespace(CookieManager=Manager)
def setup_file_logging():
    pass
async def _db_backup_loop():
    await asyncio.Event().wait()
@asynccontextmanager
async def lifespan(app):
    Path('started').write_text('ready')
    yield
    await asyncio.sleep(0.05)
    Path('stopped').write_text('cleanup complete')
app = FastAPI(lifespan=lifespan)
OriginalConfig = uvicorn.Config
uvicorn.Config = lambda *args, **kwargs: OriginalConfig(app, host='127.0.0.1', port=0, log_level='error')
''' + source + '\nasyncio.run(main())\n')
            environment = {key: value for key, value in os.environ.items() if key != 'COOKIES_STR'}
            with (root / 'output').open('w') as output:
                process = subprocess.Popen([sys.executable, str(script)], cwd=directory, env=environment,
                                           stdout=output, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 10
                    while not (root / 'started').exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue((root / 'started').exists(), (root / 'output').read_text())
                    process.send_signal(signal.SIGTERM)
                    self.assertEqual(process.wait(timeout=10), 0, (root / 'output').read_text())
                    self.assertEqual((root / 'stopped').read_text(), 'cleanup complete')
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
