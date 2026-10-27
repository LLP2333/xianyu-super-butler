"""Docker lifecycle contracts, without exposing a host Docker socket to tests."""
import tempfile
from contextlib import asynccontextmanager
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from docker.errors import NotFound, APIError
from utils.account_browser.containers import ContainerPool, ContainerDesktop, LABEL
from utils.account_browser.manager import BrowserManager, AccountBrowser


class PoolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'ACCOUNT_BROWSER_CDP_CONFIG': '/host/cdp.conf'})
        env.start()
        self.addCleanup(env.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.pool = ContainerPool(self.directory.name, network='isolated-test', image='browser:test')
        self.pool.instance = 'a' * 32
        self.pool.client = MagicMock()
        self.pool.client.images.get.return_value.id = 'sha256:new'
        self.container = MagicMock(status='created')
        self.container.attrs = {'Config': {'Env': ['CUSTOM_USER=account', 'PASSWORD=test-only']}}
        self.pool.client.containers.get.side_effect = NotFound('missing')
        self.pool.client.volumes.get.side_effect = NotFound('missing')
        self.pool.client.containers.create.return_value = self.container
        self.ready = patch('utils.account_browser.containers.ContainerDesktop.ready', AsyncMock())
        self.ready.start()
        self.addCleanup(self.ready.stop)

    async def test_native_desktop_published_but_cdp_private_and_profile_persistent(self):
        desktop = await self.pool.ensure('a', 7)
        kwargs = self.pool.client.containers.create.call_args.kwargs
        self.assertEqual(kwargs['shm_size'], '1g')
        self.assertEqual(kwargs['network'], 'isolated-test')
        self.assertEqual(kwargs['ports'], {'3000/tcp': ('127.0.0.1', 3000)})
        self.assertNotIn('privileged', kwargs)
        self.assertNotIn('docker.sock', str(kwargs['volumes']))
        self.assertIn({'bind': '/config', 'mode': 'rw'}, kwargs['volumes'].values())
        self.assertEqual(kwargs['volumes']['/host/cdp.conf']['mode'], 'ro')
        self.assertNotIn('SUBFOLDER', kwargs['environment'])
        self.assertGreater(len(kwargs['environment']['PASSWORD']), 30)
        self.assertNotIn('--enable-automation', kwargs['environment']['CHROME_CLI'])
        self.assertIs(desktop.container, self.container)
        self.container.start.assert_called_once()
        self.assertNotEqual(self.pool.name('a', 7), self.pool.name('a', 8))
        self.assertNotEqual(self.pool.name('a', 7), self.pool.name('b', 7))

    async def test_refuses_unowned_container(self):
        self.pool.client.containers.get.side_effect = None
        self.pool.client.containers.get.return_value = SimpleNamespace(labels={LABEL: 'someone-else'})
        with self.assertRaisesRegex(RuntimeError, '不属于'):
            await self.pool.ensure('a', 7)
        self.pool.client.containers.create.assert_not_called()

    async def test_refuses_unowned_volume(self):
        self.pool.client.volumes.get.side_effect = None
        self.pool.client.volumes.get.return_value.attrs = {'Labels': {LABEL: 'someone-else'}}
        with self.assertRaisesRegex(RuntimeError, '数据卷归属'):
            await self.pool.ensure('a', 7)
        self.pool.client.containers.create.assert_not_called()

    async def test_existing_running_container_is_reused(self):
        self.existing_container()
        await self.pool.ensure('a', 7)
        self.container.start.assert_not_called()
        self.pool.client.containers.create.assert_not_called()

    def existing_container(self):
        self.container.status = 'running'
        self.container.labels = {**self.pool.labels('a', 7), LABEL + '.desktop-auth': 'session-v1'}
        self.container.attrs['Image'] = 'sha256:new'
        self.container.attrs['HostConfig'] = {'PortBindings': {'3000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '3000'}]}}
        self.pool.client.networks.get.return_value.id = 'network-current'
        self.container.attrs['NetworkSettings'] = {'Networks': {'isolated-test': {
            'NetworkID': 'network-current', 'IPAddress': '172.20.0.2'}}}
        self.container.attrs['Mounts'] = [
            {'Destination': '/config', 'Type': 'volume', 'Name': self.pool.name('a', 7) + '-config', 'RW': True},
            {'Destination': '/etc/nginx/conf.d/account-browser-cdp.conf', 'Source': '/host/cdp.conf', 'RW': False},
        ]
        self.pool.client.containers.get.side_effect = None
        self.pool.client.containers.get.return_value = self.container

    async def test_exited_container_recreated_with_original_profile_volume(self):
        self.existing_container()
        self.container.status = 'exited'
        volume = MagicMock()
        volume.attrs = {'Labels': self.pool.labels('a', 7)}
        self.pool.client.volumes.get.side_effect = None
        self.pool.client.volumes.get.return_value = volume
        replacement = MagicMock(status='created', attrs={})
        self.pool.client.containers.create.return_value = replacement
        await self.pool.ensure('a', 7)
        self.container.remove.assert_called_once_with()
        self.container.start.assert_not_called()
        replacement.start.assert_called_once()
        self.pool.client.volumes.create.assert_not_called()
        volume.remove.assert_not_called()
        mounts = self.pool.client.containers.create.call_args.kwargs['volumes']
        self.assertEqual(mounts[self.pool.name('a', 7) + '-config'], {'bind': '/config', 'mode': 'rw'})

    async def test_recreated_network_replaces_old_container(self):
        self.existing_container()
        self.container.attrs['NetworkSettings']['Networks']['isolated-test']['NetworkID'] = 'network-deleted'
        await self.pool.ensure('a', 7)
        self.container.remove.assert_called_once_with()
        self.pool.client.containers.create.assert_called_once()

    async def test_changed_config_mount_replaces_old_container(self):
        self.existing_container()
        self.container.attrs['Mounts'][1]['Source'] = '/old/cdp.conf'
        await self.pool.ensure('a', 7)
        self.container.remove.assert_called_once_with()
        self.pool.client.containers.create.assert_called_once()

    async def test_paused_container_is_unpaused_before_removal(self):
        self.existing_container()
        self.container.status = 'paused'
        await self.pool.ensure('a', 7)
        calls = self.container.method_calls
        names = [call[0] for call in calls]
        self.assertLess(names.index('unpause'), names.index('stop'))
        self.assertLess(names.index('stop'), names.index('remove'))

    async def test_start_error_removes_failed_runtime_and_preserves_volume(self):
        self.container.start.side_effect = APIError('network unavailable')
        with self.assertRaisesRegex(APIError, 'network unavailable'):
            await self.pool.ensure('a', 7)
        self.container.remove.assert_called_once_with()
        self.pool.client.volumes.prune.assert_not_called()
        self.pool.client.volumes.get.return_value.remove.assert_not_called()

    async def test_externally_removed_container_is_not_treated_as_alive(self):
        self.container.reload.side_effect = NotFound('removed')
        desktop = ContainerDesktop(self.container, 'isolated-test')
        desktop.alive = True
        self.assertFalse(await desktop.refresh())
        self.assertFalse(desktop.alive)

    async def test_new_image_replaces_container_but_retains_named_volume(self):
        self.container.status = 'exited'
        self.container.labels = {**self.pool.labels('a', 7), LABEL + '.desktop-auth': 'session-v1'}
        self.container.attrs['Image'] = 'sha256:old'
        self.pool.client.containers.get.side_effect = None
        self.pool.client.containers.get.return_value = self.container
        with patch('utils.account_browser.containers.ContainerDesktop.stop', AsyncMock()) as stop:
            await self.pool.ensure('a', 7)
        stop.assert_awaited_once()
        self.container.remove.assert_called_once_with()
        self.pool.client.volumes.prune.assert_not_called()

    async def test_https_container_is_replaced_without_removing_profile(self):
        self.container.status = 'exited'
        self.container.labels = {**self.pool.labels('a', 7), LABEL + '.desktop-auth': 'session-v1'}
        self.container.attrs['Image'] = 'sha256:new'
        self.container.attrs['HostConfig'] = {'PortBindings': {'3001/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '3001'}]}}
        self.pool.client.containers.get.side_effect = None
        self.pool.client.containers.get.return_value = self.container
        with patch('utils.account_browser.containers.ContainerDesktop.stop', AsyncMock()):
            await self.pool.ensure('a', 7)
        self.container.remove.assert_called_once_with()
        self.pool.client.containers.create.assert_called_once()
        self.pool.client.volumes.get.assert_called_once_with(self.pool.name('a', 7) + '-config')
        self.pool.client.volumes.prune.assert_not_called()

    async def test_desktop_url_uses_published_http_port(self):
        self.container.attrs['NetworkSettings'] = {'Ports': {'3000/tcp': [{'HostPort': '3000'}]}}
        with patch.dict(os.environ, {'ACCOUNT_BROWSER_HOST': 'localhost'}):
            self.assertEqual(ContainerDesktop(self.container, 'test').desktop_url, 'http://localhost:3000/')

    async def test_startup_failure_stops_only_the_failed_desktop(self):
        with patch('utils.account_browser.containers.ContainerDesktop.ready', AsyncMock(side_effect=RuntimeError('not ready'))):
            with patch('utils.account_browser.containers.ContainerDesktop.stop', AsyncMock()) as stop:
                with self.assertRaisesRegex(RuntimeError, 'not ready'):
                    await self.pool.ensure('a', 7)
        stop.assert_awaited_once()

    async def test_missing_image_fails_without_silent_download(self):
        self.pool.client.images.get.side_effect = NotFound('missing image')
        with self.assertRaisesRegex(RuntimeError, '缺少账号浏览器镜像'):
            await self.pool.ensure('a', 7)
        self.pool.client.images.pull.assert_not_called()

    async def test_shutdown_waits_for_native_chrome_to_finish_writing_profile(self):
        self.container.top.side_effect = [
            {'Processes': [['/usr/bin/google-chrome --user-data-dir=/config/browser']]},
            {'Processes': [['/usr/bin/wrapped-chrome'], ['/opt/google/chrome/chrome_crashpad_handler']]},
        ]
        desktop = ContainerDesktop(self.container, 'test')
        with patch('utils.account_browser.containers.asyncio.sleep', AsyncMock()) as sleep:
            await desktop.wait_for_chrome_exit()
        self.assertEqual(self.container.top.call_count, 2)
        sleep.assert_awaited_once_with(0.1)

    async def test_clear_removes_only_owned_primary_container_and_volume(self):
        self.pool.client.containers.get.side_effect=None
        self.pool.client.containers.get.return_value=self.container
        self.container.labels=self.pool.labels('primary',7)
        self.pool.client.volumes.get.side_effect=None
        volume=self.pool.client.volumes.get.return_value
        volume.attrs={'Labels':self.pool.labels('primary',7)}
        await self.pool.clear_profile(7)
        self.pool.client.containers.get.assert_called_once_with(self.pool.name('primary',7))
        self.pool.client.volumes.get.assert_called_once_with(self.pool.name('primary',7)+'-config')
        self.container.remove.assert_called_once_with()
        volume.remove.assert_called_once_with()
        self.pool.client.volumes.prune.assert_not_called()

    async def test_clear_refuses_foreign_container_before_any_deletion(self):
        self.pool.client.containers.get.side_effect=None
        self.pool.client.containers.get.return_value=self.container
        self.container.labels=self.pool.labels('primary',8)
        with self.assertRaisesRegex(RuntimeError,'归属'):
            await self.pool.clear_profile(7)
        self.container.stop.assert_not_called()
        self.container.remove.assert_not_called()
        self.pool.client.volumes.get.assert_not_called()


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_restart_preserves_pool_identity_and_no_cookie_seeding(self):
        manager = BrowserManager('/tmp/unused-browser-manager-tests')
        desktop = SimpleNamespace(alive=True, refresh=AsyncMock(return_value=True), remove=AsyncMock())
        manager.pool = SimpleNamespace(discover=AsyncMock(return_value=[]), ensure=AsyncMock(return_value=desktop), close=AsyncMock())
        first = await manager.ensure('a', 7)
        self.assertIs(await manager.ensure('a', 7), first)
        manager.pool.ensure.assert_awaited_once_with('a', 7)
        await manager.stop('a')
        desktop.remove.assert_awaited_once()
        self.assertIsNot(await manager.ensure('a', 7), first)
        self.assertEqual(manager.pool.ensure.await_count, 2)
        await manager.close()
        manager.pool.close.assert_awaited_once()

    async def test_discovered_running_browser_is_adopted_without_stopping(self):
        manager = BrowserManager('/tmp/unused-browser-manager-tests')
        desktop = SimpleNamespace(alive=True, refresh=AsyncMock(return_value=True), remove=AsyncMock())
        manager.pool = SimpleNamespace(discover=AsyncMock(return_value=[('a', 7, desktop)]),
                                       ensure=AsyncMock(return_value=desktop))
        browser = await manager.ensure('a', 7)
        self.assertIs(browser.desktop, desktop)
        manager.pool.ensure.assert_awaited_once_with('a', 7)
        desktop.remove.assert_not_awaited()

    async def test_shutdown_flushes_chrome_before_removing_runtime(self):
        events = []
        async def send(command):
            events.append(command)
        async def wait():
            events.append('flushed')
        async def remove():
            events.append('removed')
        desktop = SimpleNamespace(wait_for_chrome_exit=wait, remove=remove)
        browser = AccountBrowser(desktop, 7)
        session = SimpleNamespace(send=send)
        context = SimpleNamespace(browser=SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=session)))
        @asynccontextmanager
        async def connect():
            yield context
        with patch.object(browser, 'connect', connect):
            await browser.close()
        self.assertEqual(events, ['Browser.close', 'flushed', 'removed'])
