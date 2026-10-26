"""Own one Xvfb display, window manager and loopback-only VNC server."""
import asyncio
import os
import shutil
import tempfile
from pathlib import Path


class Desktop:
    def __init__(self):
        self.processes = []
        self.display = None
        self.port = None
        self.directory = None

    @property
    def alive(self):
        return bool(self.processes) and all(p.returncode is None for p in self.processes)

    async def _spawn(self, *args, **kwargs):
        kwargs.setdefault('stdout', asyncio.subprocess.DEVNULL)
        process = await asyncio.create_subprocess_exec(
            *args, stderr=asyncio.subprocess.DEVNULL, **kwargs)
        self.processes.append(process)
        return process

    async def start(self):
        for executable in ('Xvfb', 'openbox', 'x11vnc'):
            if not shutil.which(executable):
                raise RuntimeError(f'账号浏览器需要 Linux 桌面组件 {executable}，请使用新版 Docker 镜像')
        self.directory = tempfile.TemporaryDirectory(prefix='account-desktop-')
        display_file = Path(self.directory.name) / 'display'
        try:
            # Xvfb allocates an unused display atomically; no shared desktop across accounts.
            with display_file.open('wb') as output:
                await self._spawn('Xvfb', '-displayfd', str(output.fileno()),
                                  '-screen', '0', '1280x900x24', '-nolisten', 'tcp',
                                  pass_fds=(output.fileno(),))
                for _ in range(100):
                    number = display_file.read_text().strip()
                    if number and self.alive:
                        self.display = f':{int(number)}'
                        break
                    if not self.alive:
                        raise RuntimeError('虚拟桌面启动失败')
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError('虚拟桌面启动超时')
            environment = {**os.environ, 'DISPLAY': self.display}
            await self._spawn('openbox', env=environment)
            # Let VNC bind an available port itself. Reserving then releasing a
            # Python socket would race with simultaneous account startups.
            vnc = await self._spawn('x11vnc', '-display', self.display, '-localhost',
                                    '-no6', '-autoport', '5900', '-forever', '-shared',
                                    '-nopw', '-noxdamage', '-quiet', stdout=asyncio.subprocess.PIPE)
            async with asyncio.timeout(15):
                while True:
                    line = await vnc.stdout.readline()
                    if not line:
                        raise RuntimeError('远程桌面未返回监听端口')
                    if line.startswith(b'PORT='):
                        self.port = int(line.split(b'=', 1)[1])
                        break
            for _ in range(100):
                if not self.alive:
                    raise RuntimeError('远程桌面启动失败')
                try:
                    reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
                    try:
                        greeting = await asyncio.wait_for(reader.readexactly(12), 2)
                        if not greeting.startswith(b'RFB '):
                            raise RuntimeError('远程桌面协议错误')
                    finally:
                        writer.close()
                        await writer.wait_closed()
                    return self
                except OSError:
                    await asyncio.sleep(0.1)
            raise RuntimeError('远程桌面连接超时')
        except BaseException:
            await self.close()
            raise

    async def close(self):
        for process in reversed(self.processes):
            if process.returncode is None:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass
        self.processes.clear()
        if self.directory:
            self.directory.cleanup()
            self.directory = None
