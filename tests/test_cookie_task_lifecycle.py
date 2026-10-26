import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.cookie_manager import CookieManager


class CookieTaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db_patch = patch("app.cookie_manager.db_manager")
        self.db = self.db_patch.start()
        self.db.get_all_cookies.return_value = {}
        self.db.get_all_keywords.return_value = {}
        self.db.get_all_cookie_status.return_value = {}
        self.db.get_cookie_details.return_value = {"user_id": 7}
        self.manager = CookieManager(asyncio.get_running_loop())

        async def wait_forever(*_args, **_kwargs):
            await asyncio.sleep(3600)

        self.manager._run_xianyu = AsyncMock(side_effect=wait_forever)

    async def asyncTearDown(self):
        for task in self.manager.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.manager.tasks.values(), return_exceptions=True)
        self.db_patch.stop()

    async def test_new_account_task_is_registered(self):
        task = await self.manager._ensure_cookie_task_async("account-1", "cookie")

        self.assertIs(task, self.manager.tasks["account-1"])
        self.assertEqual(self.manager.get_task_state("account-1"), "running")

    async def test_running_task_can_be_restarted(self):
        first_task = await self.manager._ensure_cookie_task_async("account-1", "cookie")
        second_task = await self.manager._ensure_cookie_task_async(
            "account-1", "new-cookie", restart=True
        )

        self.assertTrue(first_task.cancelled())
        self.assertIs(second_task, self.manager.tasks["account-1"])
        self.assertEqual(self.manager.cookies["account-1"], "new-cookie")

    async def test_disabled_account_updates_cookie_without_starting(self):
        self.manager.cookie_status["account-1"] = False

        task = await self.manager._ensure_cookie_task_async("account-1", "cookie")

        self.assertIsNone(task)
        self.assertNotIn("account-1", self.manager.tasks)
        self.assertEqual(self.manager.cookies["account-1"], "cookie")

    async def test_completed_task_can_start_again(self):
        completed = asyncio.create_task(asyncio.sleep(0))
        await completed
        self.manager.tasks["account-1"] = completed
        self.manager.cookies["account-1"] = "cookie"

        restarted = await self.manager._ensure_cookie_task_async("account-1")

        self.assertIsNot(restarted, completed)
        self.assertEqual(self.manager.get_task_state("account-1"), "running")

    async def test_waiting_browser_token_is_not_reported_as_running(self):
        from types import SimpleNamespace
        await self.manager._ensure_cookie_task_async('account-1', 'cookie')
        self.manager.instances['account-1'] = SimpleNamespace(
            last_token_refresh_status='waiting_browser_token', connection_state='disconnected')
        self.assertEqual(self.manager.get_task_state('account-1'), 'waiting_browser')

    async def test_takeover_prevents_automatic_restart_until_explicit_resume(self):
        original = await self.manager._ensure_cookie_task_async('account-1', 'old')
        await self.manager.pause_cookie_task('account-1')
        self.assertTrue(original.done())
        self.assertEqual(self.manager.get_task_state('account-1'), 'manual')
        self.assertIsNone(await self.manager._ensure_cookie_task_async('account-1', restart=True))
        await self.manager.update_cookie_async('account-1', 'rotated', save_to_db=False)
        self.assertFalse(any(not task.done() for task in self.manager.tasks.values()))
        await self.manager.resume_cookie_task('account-1', 'confirmed')
        self.assertEqual(self.manager.get_task_state('account-1'), 'running')
        self.assertEqual(self.manager.cookies['account-1'], 'confirmed')

    async def test_takeover_and_resume_use_account_owner_loop(self):
        import threading
        owner = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(owner)
            owner.call_soon(ready.set)
            owner.run_forever()

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(ready.wait(2))
        manager = CookieManager(owner)
        loops = []

        async def worker(*args):
            loops.append(asyncio.get_running_loop())
            try:
                await asyncio.sleep(3600)
            finally:
                await asyncio.sleep(0.02)

        manager._run_xianyu = worker
        try:
            await manager.update_cookie_async('account', 'old', save_to_db=False)
            first = manager.tasks['account']
            await manager.pause_cookie_task('account')
            self.assertTrue(first.done())
            await manager.update_cookie_async('account', 'rotated', save_to_db=False)
            self.assertFalse(any(not task.done() for task in manager.tasks.values()))
            await manager.resume_cookie_task('account', 'confirmed')
            self.assertIs(manager.tasks['account'].get_loop(), owner)
            self.assertTrue(all(loop is owner for loop in loops))
            self.assertEqual(manager.cookies['account'], 'confirmed')
        finally:
            async def cleanup():
                for task in manager.tasks.values():
                    task.cancel()
                await asyncio.gather(*manager.tasks.values(), return_exceptions=True)
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(cleanup(), owner))
            owner.call_soon_threadsafe(owner.stop)
            thread.join(2)
            owner.close()


if __name__ == "__main__":
    unittest.main()
