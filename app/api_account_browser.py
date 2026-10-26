"""Authenticated HTTP control and binary WebSocket-to-VNC transport."""
import asyncio
import secrets
import time
from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from utils.manual_captcha import ManualVerificationError


async def relay_vnc(websocket, port):
    reader, writer = await asyncio.open_connection('127.0.0.1', port)

    async def to_browser():
        while True:
            data = await websocket.receive_bytes()
            writer.write(data)
            await writer.drain()

    async def to_client():
        while data := await reader.read(65536):
            await websocket.send_bytes(data)

    tasks = [asyncio.create_task(to_browser()), asyncio.create_task(to_client())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        await writer.wait_closed()


def create_account_browser_router(require_operator, service):
    router = APIRouter(prefix='/api/account-browser', tags=['account-browser'])
    tickets = {}

    async def call(operation):
        try:
            return await operation
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ValueError, ManualVerificationError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, f'浏览器操作失败（{type(exc).__name__}），可重新连接后重试') from exc

    @router.post('/{account_id}/open')
    async def open_browser(account_id: str, user=Depends(require_operator)):
        return await call(service.takeover(account_id, user['user_id']))

    @router.post('/{account_id}/challenge')
    async def challenge(account_id: str, user=Depends(require_operator)):
        return await call(service.challenge(account_id, user['user_id']))

    @router.post('/{account_id}/confirm')
    async def confirm(account_id: str, user=Depends(require_operator)):
        return await call(service.finish(account_id, user['user_id']))

    @router.post('/{account_id}/release')
    async def release(account_id: str, user=Depends(require_operator)):
        return await call(service.finish(account_id, user['user_id'], save=False))

    @router.post('/{account_id}/ticket')
    async def ticket(account_id: str, user=Depends(require_operator)):
        async with service.browsers.lock(account_id):
            try:
                service.require_manual(account_id, user['user_id'])
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            # Short-lived, single-use credentials; the long-lived login token never enters a URL.
            for key, value in list(tickets.items()):
                if value[2] <= time.monotonic() or value[0] == account_id:
                    tickets.pop(key)
            value = secrets.token_urlsafe(32)
            tickets[value] = (account_id, user['user_id'], time.monotonic() + 30)
            return {'ticket': value}

    @router.websocket('/{account_id}/desktop')
    async def desktop(websocket: WebSocket, account_id: str):
        entry = tickets.pop(websocket.query_params.get('ticket', ''), None)
        if not entry or entry[0] != account_id or entry[2] < time.monotonic():
            await websocket.close(code=1008)
            return
        try:
            async with service.browsers.lock(account_id):
                _, browser = service.require_manual(account_id, entry[1])
                if not service.db.get_cookie_status(account_id):
                    raise ValueError('账号已禁用')
                await service.disconnect(account_id)
                await websocket.accept(subprotocol='binary' if 'binary' in websocket.scope.get('subprotocols', []) else None)
                relay = asyncio.create_task(relay_vnc(websocket, browser.desktop.port))
                service.viewers[account_id] = (websocket, relay)
            await relay
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            # Replacing a viewer or confirming credentials intentionally stops its relay.
        except Exception:
            pass
        finally:
            viewer = service.viewers.get(account_id)
            if viewer and viewer[0] is websocket:
                service.viewers.pop(account_id, None)
            with suppress(Exception):
                await websocket.close()

    return router
