"""SMTP email delivery with attachments."""

from __future__ import annotations

import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from fieldnote.config import Settings
from fieldnote.logging_setup import get_logger

log = get_logger(__name__)


class EmailError(Exception):
    pass


def build_message(
    settings: Settings, subject: str, text: str, html: str | None = None, attachments: list[Path] | None = None
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from or ""
    msg["To"] = ", ".join(settings.smtp_to)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    for path in attachments or []:
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    return msg


def send_email(
    settings: Settings, subject: str, text: str, html: str | None = None, attachments: list[Path] | None = None
) -> None:
    if not settings.has_email:
        raise EmailError("SMTP_HOST / SMTP_FROM / SMTP_TO not configured")
    msg = build_message(settings, subject, text, html, attachments)
    host, port = settings.smtp_host or "", settings.smtp_port
    try:
        if settings.smtp_security == "ssl":
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as smtp:
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password or "")
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                if settings.smtp_security == "starttls":
                    smtp.starttls(context=ssl.create_default_context())
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password or "")
                smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"SMTP delivery failed: {type(exc).__name__}") from exc
