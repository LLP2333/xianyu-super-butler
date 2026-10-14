"""
刮刮乐远程控制 API 路由
提供 WebSocket 和 HTTP 接口用于远程操作滑块验证
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel
import asyncio
import time
from loguru import logger

from utils.captcha_remote_control import captcha_controller

# 创建路由器
router = APIRouter(prefix="/api/captcha", tags=["captcha"])


class MouseEvent(BaseModel):
    """鼠标事件模型"""
    session_id: str
    event_type: str  # down, move, up
    x: int
    y: int


class SessionCheckRequest(BaseModel):
    """会话检查请求"""
    session_id: str


# =============================================================================
# WebSocket 端点 - 实时通信
# =============================================================================

@router.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """
    WebSocket 连接用于实时传输截图和接收鼠标事件
    """
    await websocket.accept()
    logger.info(f"🔌 WebSocket 连接建立: {session_id}")
    
    from collections import deque

    pending = deque()
    ready = asyncio.Event()
    worker = None

    async def apply_input():
        while True:
            await ready.wait()
            while pending:
                event = pending.popleft()
                if not await captcha_controller.handle_mouse_event(
                    session_id, event['event_type'], event['x'], event['y']
                ):
                    await websocket.send_json({'type': 'error', 'message': '鼠标操作失败，请重新开启人工验证'})
            ready.clear()

    try:
        deadline = time.monotonic() + 90
        while session_id not in captcha_controller.active_sessions:
            if time.monotonic() >= deadline:
                await websocket.send_json({'type': 'error', 'message': '验证会话启动超时'})
                return
            await asyncio.sleep(0.2)

        previous = captcha_controller.websocket_connections.get(session_id)
        if previous and previous is not websocket:
            await previous.close()
        captcha_controller.websocket_connections[session_id] = websocket
        session = captcha_controller.active_sessions[session_id]
        await websocket.send_json({
            'type': 'session_info', 'screenshot': session['screenshot'],
            'origin': session['clip'] or {'x': 0, 'y': 0},
            'viewport': session['viewport'],
            'phase': session.get('phase', 'ready'),
            'message': session.get('message', '按住滑块向右拖动完成验证'),
        })
        worker = asyncio.create_task(apply_input())
        while True:
            data = await websocket.receive_json()
            if data.get('type') == 'mouse_event':
                if session.get('phase', 'ready') != 'ready':
                    continue
                event = MouseEvent(session_id=session_id, **{
                    key: data[key] for key in ('event_type', 'x', 'y')
                })
                if event.event_type not in ('down', 'move', 'up'):
                    continue
                item = {'event_type': event.event_type, 'x': event.x, 'y': event.y}
                # Only replace adjacent moves: never drop or reorder down/up boundaries.
                if pending and event.event_type == 'move' and pending[-1]['event_type'] == 'move':
                    pending[-1] = item
                else:
                    if len(pending) >= 128:
                        raise ValueError('鼠标事件队列溢出，请重新开启验证')
                    pending.append(item)
                ready.set()
            elif data.get('type') == 'cancel':
                session['cancelled'] = True
                session['released'].set()
            elif data.get('type') == 'ping':
                await websocket.send_json({'type': 'pong'})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning(f"远程验证通道失败: {session_id}, {type(exc).__name__}")
    finally:
        if worker:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if captcha_controller.websocket_connections.get(session_id) is websocket:
            del captcha_controller.websocket_connections[session_id]
            session = captcha_controller.active_sessions.get(session_id)
            if session:
                try:
                    await session['page'].mouse.up()
                except Exception:
                    pass
        try:
            await websocket.close()
        except Exception:
            pass


# =============================================================================
# HTTP 端点 - REST API
# =============================================================================

@router.get("/sessions")
async def get_active_sessions():
    """获取所有活跃的验证会话"""
    sessions = []
    for session_id, data in captcha_controller.active_sessions.items():
        sessions.append({
            'session_id': session_id,
            'completed': data.get('completed', False),
            'has_websocket': session_id in captcha_controller.websocket_connections
        })
    
    return {
        'count': len(sessions),
        'sessions': sessions
    }


@router.get("/session/{session_id}")
async def get_session_info(session_id: str):
    """获取指定会话的信息"""
    if session_id not in captcha_controller.active_sessions:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    session_data = captcha_controller.active_sessions[session_id]
    
    return {
        'session_id': session_id,
        'screenshot': session_data['screenshot'],
        'captcha_info': session_data['captcha_info'],
        'viewport': session_data['viewport'],
        'completed': session_data.get('completed', False)
    }


@router.get("/screenshot/{session_id}")
async def get_screenshot(session_id: str):
    """获取最新截图"""
    screenshot = await captcha_controller.update_screenshot(session_id)
    
    if not screenshot:
        raise HTTPException(status_code=404, detail="无法获取截图")
    
    return {'screenshot': screenshot}


@router.post("/mouse_event")
async def handle_mouse_event(event: MouseEvent):
    """处理鼠标事件（HTTP方式，不推荐，建议使用WebSocket）"""
    success = await captcha_controller.handle_mouse_event(
        event.session_id,
        event.event_type,
        event.x,
        event.y
    )
    
    if not success:
        raise HTTPException(status_code=400, detail="处理失败")
    
    # 检查是否完成
    completed = captcha_controller.is_completed(event.session_id)
    
    return {
        'success': True,
        'completed': completed
    }


@router.post("/check_completion")
async def check_completion(request: SessionCheckRequest):
    """检查验证是否完成"""
    completed = captcha_controller.is_completed(request.session_id)
    
    return {
        'session_id': request.session_id,
        'completed': completed
    }


@router.delete("/session/{session_id}")
async def close_session(session_id: str):
    """关闭会话"""
    await captcha_controller.close_session(session_id)
    return {'success': True}


# =============================================================================
# 前端页面
# =============================================================================

@router.get("/status/{session_id}")
async def get_captcha_status(session_id: str):
    """
    获取验证状态
    用于前端轮询检查验证是否完成
    """
    try:
        is_completed = captcha_controller.is_completed(session_id)
        session_exists = captcha_controller.session_exists(session_id)
        
        return {
            "success": True,
            "completed": is_completed,
            "session_exists": session_exists,
            "session_id": session_id
        }
    except Exception as e:
        logger.error(f"获取验证状态失败: {e}")
        return {
            "success": False,
            "completed": False,
            "session_exists": False,
            "session_id": session_id,
            "error": str(e)
        }
