"""Single-account browser actions and one-time native desktop authorization."""
import asyncio
import secrets
import time
from urllib.parse import urlencode
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse, RedirectResponse, Response


class ClearBrowserIn(BaseModel):
    confirmed: bool


def create_account_browser_router(require_operator, service, verify_session):
    router = APIRouter(prefix='/api/account-browser', tags=['account-browser'])
    tickets, sessions = {}, {}

    async def call(operation):
        try:
            async with asyncio.timeout(90):
                return JSONResponse(await operation, headers={'Cache-Control': 'no-store'})
        except TimeoutError as exc:
            raise HTTPException(504, '浏览器操作超时，请检查容器状态') from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, f'浏览器操作失败（{type(exc).__name__}）') from exc

    def grant(request, user):
        browser = service.desktop(user['user_id'])
        bearer = request.headers.get('authorization', '').removeprefix('Bearer ')
        now = time.monotonic()
        for entries in (tickets, sessions):
            for key, entry in list(entries.items()):
                if entry[2] <= now:
                    entries.pop(key, None)
        ticket = secrets.token_urlsafe(32)
        tickets[ticket] = (user['user_id'], bearer, now + 60, browser.desktop.auth.password)
        return service.desktop_url(user['user_id']) + '_desktop_open?' + urlencode({'ticket': ticket})

    def check_container(request):
        try:
            browser = service.desktop()
        except LookupError:
            raise HTTPException(403, '浏览器不可用')
        if not secrets.compare_digest(request.headers.get('x-desktop-key', ''), browser.desktop.auth.password):
            raise HTTPException(403, '无效的桌面认证请求')
        return browser

    def check_entry(entry, browser):
        if not entry or entry[2] <= time.monotonic() or entry[0] != browser.owner_id or entry[3] != browser.desktop.auth.password:
            raise HTTPException(403, '桌面链接已失效，请回管家重新打开')
        user = verify_session(entry[1])
        if not user or user['user_id'] != entry[0]:
            raise HTTPException(403, '管家登录已失效，请重新登录')

    @router.post('/setup')
    async def setup(request: Request, user=Depends(require_operator)):
        async def operation():
            result = await service.setup(user['user_id'])
            result['open_url'] = grant(request, user)
            return result
        return await call(operation())

    @router.get('/setup-status')
    async def setup_status(user=Depends(require_operator)):
        return await call(service.setup_status(user['user_id']))

    @router.post('/desktop-link')
    async def desktop_link(request: Request, user=Depends(require_operator)):
        async def operation():
            return {'open_url': grant(request, user)}
        return await call(operation())

    @router.get('/desktop-entry')
    async def desktop_entry(request: Request):
        browser = check_container(request)
        entry = tickets.pop(request.headers.get('x-desktop-ticket', ''), None)
        check_entry(entry, browser)
        session = secrets.token_urlsafe(32)
        sessions[session] = (entry[0], entry[1], time.monotonic() + 3600, entry[3])
        response = RedirectResponse('/', status_code=303, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        response.set_cookie('xianyu_desktop', session, max_age=3600, path='/', secure=True, httponly=True, samesite='lax')
        return response

    @router.get('/desktop-auth')
    async def desktop_auth(request: Request):
        browser = check_container(request)
        check_entry(sessions.get(request.headers.get('x-desktop-session', '')), browser)
        return Response(status_code=204, headers={'Cache-Control': 'no-store'})

    @router.post('/clear')
    async def clear(body: ClearBrowserIn, user=Depends(require_operator)):
        if not body.confirmed:
            raise HTTPException(400, '请确认清空浏览器数据')
        return await call(service.clear(user['user_id']))

    @router.post('/{account_id}/open')
    async def open_browser(account_id: str, user=Depends(require_operator)):
        return await call(service.takeover(account_id, user['user_id']))

    @router.post('/{account_id}/restart')
    async def restart_browser(account_id: str, user=Depends(require_operator)):
        return await call(service.takeover(account_id, user['user_id'], restart=True))

    @router.get('/{account_id}/status')
    async def status(account_id: str, user=Depends(require_operator)):
        return await call(service.status(account_id, user['user_id']))

    @router.post('/{account_id}/confirm')
    async def confirm(account_id: str, user=Depends(require_operator)):
        return await call(service.finish(account_id, user['user_id']))

    @router.post('/{account_id}/release')
    async def release(account_id: str, user=Depends(require_operator)):
        return await call(service.finish(account_id, user['user_id'], save=False))

    return router
