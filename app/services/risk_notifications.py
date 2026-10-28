"""Notify when an account enters manual verification, independently of its runtime."""
import asyncio
from datetime import datetime

from loguru import logger

from app.services.notification_sender import send_notification


_pending = set()


RISK_TITLE = '平台风控 · 等待人工'
RISK_EVENT = 'captcha_manual'
RISK_HINT = ('账号已暂停闲鱼请求。请打开账号的原生浏览器完成登录或验证，'
             '并打开消息页；捕获新凭据后自动恢复。')
RENEWAL_TITLE = '凭据到期 · 自动续期失败'
RENEWAL_EVENT = 'cookie_invalid'
RENEWAL_HINT = ('消息 Token 已到期，原生浏览器自动续期没有成功，账号已暂停闲鱼请求。'
                '请打开原生浏览器检查登录状态，再点击「获取新凭据」。')
RECOVERED_TITLE = '凭据已自动恢复'
RECOVERED_EVENT = 'credential_recovered'
RECOVERED_HINT = '之前的自动续期失败已由系统自行恢复，账号已恢复闲鱼请求，无需处理。'


def schedule_manual_verification_notification(account_id: str, reason: str, title: str = RISK_TITLE,
                                              hint: str = RISK_HINT, event_type: str = RISK_EVENT) -> None:
    """Keep delivery alive even when the account task is paused immediately."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(f'【{account_id}】人工验证通知未发送：无运行中的事件循环')
        return
    task = loop.create_task(send_manual_verification_notification(account_id, reason, title, hint, event_type))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def send_manual_verification_notification(account_id: str, reason: str, title: str = RISK_TITLE,
                                                hint: str = RISK_HINT, event_type: str = RISK_EVENT) -> None:
    try:
        from app.db_manager import db_manager

        account = db_manager.get_cookie_details(account_id)
        if not account:
            return
        rules = db_manager.get_account_notifications(
            account_id, user_id=account['user_id'], event_type=event_type,
        )
        name = account.get('nickname') or account.get('remark') or account_id
        message = (
            f'{title}\n\n账号: {name}\n账号ID: {account_id}\n'
            f'时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n原因: {reason}\n\n{hint}'
        )
        sent_channels = set()
        for rule in rules:
            if not rule.get('enabled') or rule['channel_id'] in sent_channels:
                continue
            try:
                await send_notification(rule['channel_type'], rule['channel_config'], message)
                sent_channels.add(rule['channel_id'])
                logger.info(f'【{account_id}】人工验证通知发送成功，渠道 {rule["channel_id"]}')
            except Exception as exc:
                logger.warning(f'【{account_id}】人工验证通知发送失败，渠道 {rule["channel_id"]}: {type(exc).__name__}')
    except Exception as exc:
        logger.warning(f'【{account_id}】人工验证通知处理失败: {type(exc).__name__}')
