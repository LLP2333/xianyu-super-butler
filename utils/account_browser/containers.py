"""Own labelled LinuxServer Chrome containers and their persistent /config volumes."""
import asyncio
import hashlib
import io
import tarfile
import os
import secrets
import uuid
from pathlib import Path
from contextlib import suppress

import aiohttp
import docker
from docker.errors import NotFound

LABEL = 'io.xianyu.account-browser'


class ContainerDesktop:
    def __init__(self, container, network):
        self.container = container
        self.network = network
        self.host = ''
        self.alive = False
        environment = dict(item.split('=', 1) for item in container.attrs.get('Config', {}).get('Env', []) if '=' in item)
        self.auth = aiohttp.BasicAuth(environment.get('CUSTOM_USER', 'account'), environment.get('PASSWORD', ''))

    async def refresh(self):
        try:
            await asyncio.to_thread(self.container.reload)
        except NotFound:
            self.alive = False
            self.host = ''
            return False
        self.alive = self.container.status == 'running'
        networks = self.container.attrs.get('NetworkSettings', {}).get('Networks', {})
        self.host = networks.get(self.network, {}).get('IPAddress', '')
        return self.alive and bool(self.host)

    @property
    def url(self):
        return f'http://{self.host}:3000/'

    @property
    def desktop_url(self):
        ports = self.container.attrs.get('NetworkSettings', {}).get('Ports', {}).get('3000/tcp') or []
        if not ports:
            raise RuntimeError('原生桌面端口未发布，请重新创建账号浏览器')
        host = os.environ.get('ACCOUNT_BROWSER_HOST', 'localhost')
        if ':' in host and not host.startswith('['):
            host = f'[{host}]'
        return f"http://{host}:{ports[0]['HostPort']}/"

    async def ready(self):
        async with aiohttp.ClientSession(auth=self.auth, timeout=aiohttp.ClientTimeout(total=3)) as client:
            for _ in range(90):
                if not await self.refresh():
                    raise RuntimeError('账号浏览器容器未运行或未加入浏览器网络')
                try:
                    async with client.get(self.url + '_desktop_health') as response:
                        desktop_ready = response.status == 204
                    async with client.get(f'http://{self.host}:9223/json/version') as response:
                        if desktop_ready and response.status == 200:
                            return
                except (aiohttp.ClientError, TimeoutError):
                    pass
                await asyncio.sleep(0.5)
        raise RuntimeError('LinuxServer Chrome 启动超时，请检查账号浏览器容器日志')

    async def stop(self):
        await self.refresh()
        if self.container.status == 'paused':
            await asyncio.to_thread(self.container.unpause)
            self.alive = True
        if self.alive or self.container.status == 'restarting':
            await asyncio.to_thread(self.container.stop, timeout=15)
        self.alive = False

    async def remove(self):
        """Discard the runtime only; never delete the persistent profile volume."""
        with suppress(NotFound):
            await self.stop()
            await asyncio.to_thread(self.container.remove)

    async def wait_for_chrome_exit(self):
        # Browser.close acknowledges before the OS process has finished writing its profile.
        for _ in range(50):
            listing = await asyncio.to_thread(self.container.top, ps_args='-eo args')
            executables = [row[0].split(' ', 1)[0] for row in listing.get('Processes', []) if row]
            if not any(name in ('/usr/bin/google-chrome', '/opt/google/chrome/chrome') for name in executables):
                return
            await asyncio.sleep(0.1)
        raise TimeoutError('Chrome 未在限定时间内退出')


class ContainerPool:
    def __init__(self, root, *, image=None, network=None, start_url='https://www.goofish.com/'):
        self.root = Path(root)
        self.image = image or os.environ.get('ACCOUNT_BROWSER_IMAGE', 'lscr.io/linuxserver/chrome:latest')
        self.network = network or os.environ.get('ACCOUNT_BROWSER_NETWORK', 'xianyu-browsers')
        self.start_url = start_url
        self.client = None
        self.instance = None

    async def initialize(self):
        if self.client:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        identity = self.root / 'installation-id'
        try:
            with identity.open('x') as output:
                output.write(uuid.uuid4().hex)
        except FileExistsError:
            pass
        self.instance = identity.read_text().strip()
        if len(self.instance) != 32 or any(c not in '0123456789abcdef' for c in self.instance):
            raise RuntimeError('账号浏览器 installation-id 无效')
        client = None
        try:
            client = await asyncio.to_thread(docker.from_env, timeout=25)
            await asyncio.to_thread(client.networks.get, self.network)
        except Exception as exc:
            if client:
                await asyncio.to_thread(client.close)
            raise RuntimeError('账号浏览器需要 Docker 管理接口及 ACCOUNT_BROWSER_NETWORK 指定的网络，请使用更新后的 Compose 配置') from exc
        self.client = client

    def labels(self, account_id, owner_id):
        return {LABEL: self.instance, LABEL + '.account': str(account_id), LABEL + '.owner': str(owner_id)}

    def name(self, account_id, owner_id):
        digest = hashlib.sha256(f'{owner_id}:{account_id}'.encode()).hexdigest()[:24]
        return f'xianyu-browser-{self.instance[:12]}-{digest}'

    async def discover(self):
        await self.initialize()
        containers = await asyncio.to_thread(self.client.containers.list, all=True, filters={'label': f'{LABEL}={self.instance}'})
        return [(c.labels[LABEL + '.account'], int(c.labels[LABEL + '.owner']),
                 ContainerDesktop(c, self.network))
                for c in containers if LABEL + '.account' in c.labels and LABEL + '.owner' in c.labels]

    async def ensure(self, account_id, owner_id):
        await self.initialize()
        name = self.name(account_id, owner_id)
        labels = self.labels(account_id, owner_id)
        config = os.environ.get('ACCOUNT_BROWSER_CDP_CONFIG')
        if not config or not Path(config).is_absolute():
            raise RuntimeError('请设置 ACCOUNT_BROWSER_CDP_CONFIG 为 Docker 宿主机上 cdp.conf 的绝对路径')
        network = await asyncio.to_thread(self.client.networks.get, self.network)
        volume_name = name + '-config'
        try:
            image = await asyncio.to_thread(self.client.images.get, self.image)
        except NotFound as exc:
            raise RuntimeError(f'缺少账号浏览器镜像 {self.image}，请先构建或拉取该镜像') from exc
        try:
            container = await asyncio.to_thread(self.client.containers.get, name)
            if any(container.labels.get(key) != value for key, value in labels.items()):
                raise RuntimeError('同名容器不属于当前账号，拒绝接管')
            bindings = container.attrs.get('HostConfig', {}).get('PortBindings', {})
            expected_bindings = {'3000/tcp': [{'HostIp': os.environ.get('ACCOUNT_BROWSER_BIND', '127.0.0.1'), 'HostPort': '3000'}]}
            endpoint = container.attrs.get('NetworkSettings', {}).get('Networks', {}).get(self.network, {})
            mounts = {m['Destination']: m for m in container.attrs.get('Mounts', [])}
            profile = mounts.get('/config', {})
            cdp = mounts.get('/etc/nginx/conf.d/account-browser-cdp.conf', {})
            if (container.status != 'running'
                    or container.attrs.get('Image') != image.id
                    or container.labels.get(LABEL + '.desktop-auth') != 'session-v1'
                    or bindings != expected_bindings
                    or endpoint.get('NetworkID') != network.id
                    or not endpoint.get('IPAddress')
                    or profile.get('Type') != 'volume' or profile.get('Name') != volume_name
                    or not profile.get('RW')
                    or cdp.get('Source') != config or cdp.get('RW') is not False):
                desktop = ContainerDesktop(container, self.network)
                # Container metadata is replaceable; its named /config volume is retained.
                await desktop.remove()
                container = None
        except NotFound:
            container = None
        if container is None:
            try:
                volume = await asyncio.to_thread(self.client.volumes.get, volume_name)
                if any((volume.attrs.get('Labels') or {}).get(key) != value for key, value in labels.items()):
                    raise RuntimeError('账号浏览器数据卷归属不匹配，拒绝挂载')
            except NotFound:
                await asyncio.to_thread(self.client.volumes.create, name=volume_name, labels=labels)
            container = await asyncio.to_thread(
                self.client.containers.create, self.image, name=name, labels={**labels, LABEL + '.desktop-auth': 'session-v1'},
                network=self.network, shm_size='1g',
                ports={'3000/tcp': (os.environ.get('ACCOUNT_BROWSER_BIND', '127.0.0.1'), 3000)},
                volumes={volume_name: {'bind': '/config', 'mode': 'rw'},
                         config: {'bind': '/etc/nginx/conf.d/account-browser-cdp.conf', 'mode': 'ro'}},
                environment={
                    'TZ': os.environ.get('TZ', 'Asia/Shanghai'),
                    'CUSTOM_USER': 'account',
                    'DESKTOP_AUTH_API': os.environ.get('ACCOUNT_BROWSER_API_URL', 'http://xianyu-app:8080'),
                    'PASSWORD': secrets.token_urlsafe(32),
                    'CHROME_CLI': '--remote-debugging-port=9222 --user-data-dir=/config/browser '
                                  '--restore-last-session ' + self.start_url,
                },
                # Only the native HTTP desktop is published; the tunnel terminates HTTPS. CDP stays on the private network.
                restart_policy={'Name': 'no'},
                log_config=docker.types.LogConfig(type='json-file', config={'max-size': '10m', 'max-file': '2'}),
            )
            # Official supported startup hook; no derivative image or installed packages.
            script = Path(__file__).resolve().parents[2] / 'deploy/account-browser/desktop-auth.py'
            data = b"#!/bin/sh\nexec /lsiopy/bin/python3 - <<'PY'\n" + script.read_bytes() + b"\nPY\n"
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode='w') as output:
                info = tarfile.TarInfo('custom-cont-init.d/50-desktop-auth')
                info.size, info.mode, info.uid, info.gid = len(data), 0o755, 0, 0
                output.addfile(info, io.BytesIO(data))
            await asyncio.to_thread(container.put_archive, '/', archive.getvalue())
        desktop = ContainerDesktop(container, self.network)
        try:
            if container.status != 'running':
                await asyncio.to_thread(container.start)
            await desktop.ready()
        except BaseException:
            with suppress(Exception):
                await desktop.remove()
            raise
        return desktop

    async def clear_profile(self, owner_id):
        """Explicit user action: remove only the owned primary profile volume."""
        await self.initialize()
        name = self.name('primary', owner_id)
        expected = self.labels('primary', owner_id)
        try:
            container = await asyncio.to_thread(self.client.containers.get, name)
            if any(container.labels.get(k) != v for k, v in expected.items()):
                raise RuntimeError('浏览器归属不匹配，拒绝清空')
            await asyncio.to_thread(container.stop, timeout=15)
            await asyncio.to_thread(container.remove)
        except NotFound:
            pass
        try:
            volume = await asyncio.to_thread(self.client.volumes.get, name + '-config')
            if any((volume.attrs.get('Labels') or {}).get(k) != v for k, v in expected.items()):
                raise RuntimeError('浏览器数据卷归属不匹配，拒绝清空')
            await asyncio.to_thread(volume.remove)
        except NotFound:
            pass

    async def close(self):
        if self.client:
            await asyncio.to_thread(self.client.close)
            self.client = None
