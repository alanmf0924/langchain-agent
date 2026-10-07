from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path
from secrets import token_urlsafe

from app.customer.config import get_customer_auth_settings

logger = logging.getLogger(__name__)


async def send_account_email(recipient: str, subject: str, body: str) -> None:
    """发送账户邮件；日志模式绝不记录含 token 的正文或链接。"""
    settings = get_customer_auth_settings()
    if settings.mail_mode == "log":
        logger.info("Customer account email suppressed in log mode for recipient=%s", recipient)
        return
    message = EmailMessage()
    message["From"] = settings.mail_from
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    if settings.mail_mode == "file":
        await asyncio.to_thread(write_message_to_outbox, message)
        return
    await asyncio.to_thread(send_smtp_message, message)


def send_smtp_message(message: EmailMessage) -> None:
    """SMTP 是唯一生产邮件适配器；网络 I/O 在线程中执行，避免阻塞事件循环。"""
    settings = get_customer_auth_settings()
    if settings.mail_use_tls:
        with smtplib.SMTP_SSL(settings.mail_host, settings.mail_port, timeout=8) as client:
            client.login(settings.mail_username, settings.mail_password)
            client.send_message(message)
        return
    with smtplib.SMTP(settings.mail_host, settings.mail_port, timeout=8) as client:
        client.starttls()
        client.login(settings.mail_username, settings.mail_password)
        client.send_message(message)


def write_message_to_outbox(message: EmailMessage) -> None:
    """仅用于本地联调：邮件正文写入开发者指定的私有 outbox，而不是应用日志。"""
    outbox = Path(get_customer_auth_settings().mail_outbox_dir)
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / f"customer-account-{token_urlsafe(12)}.eml").write_bytes(message.as_bytes())
