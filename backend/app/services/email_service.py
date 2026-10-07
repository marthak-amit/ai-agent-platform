"""
Outbound email with a provider abstraction.

  EmailProvider        anything with `async send(EmailMessage)`; raise on failure.
  ConsoleEmailProvider the DEFAULT: logs the message instead of sending it, so dev/staging/tests never email
                       real customers by accident (EMAIL_PROVIDER=console).
  SmtpEmailProvider    stdlib smtplib run in a worker thread (EMAIL_PROVIDER=smtp + SMTP_HOST ...).

`send_email()` is the one call site: it never raises (a notification must not break a payment or the scheduler)
and returns True only when the provider accepted the message. An unknown / misconfigured provider degrades to the
console provider with a warning.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, parseaddr
from typing import Protocol

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 10


@dataclass
class EmailAttachment:
    """A file attached to an email."""

    filename: str
    content: bytes
    mime_type: str = "application/pdf"


@dataclass
class EmailMessage:
    """One outbound email: plain-text body plus optional HTML alternative and attachments."""

    to: str
    subject: str
    text: str
    html: str | None = None
    attachments: list[EmailAttachment] = field(default_factory=list)


class EmailProvider(Protocol):
    """Transport for outbound email. Implementations raise on failure."""

    async def send(self, message: EmailMessage, sender: str) -> None:
        """Deliver *message* from *sender* (an RFC 5322 'Name <addr>' string)."""
        ...


class ConsoleEmailProvider:
    """Logs the email (recipient, subject, attachment names, text body) instead of sending it."""

    async def send(self, message: EmailMessage, sender: str) -> None:
        """Log the message at INFO."""
        logger.info(
            "EMAIL (console, not sent) from=%s to=%s subject=%r attachments=%s\n%s",
            sender, message.to, message.subject, [a.filename for a in message.attachments], message.text[:2000],
        )


def build_mime(message: EmailMessage, sender: str) -> MimeMessage:
    """Translate an EmailMessage into a stdlib MIME message (text, optional HTML alternative, attachments)."""
    mime = MimeMessage()
    mime["From"] = sender
    mime["To"] = message.to
    mime["Subject"] = message.subject
    mime.set_content(message.text)
    if message.html:
        mime.add_alternative(message.html, subtype="html")
    for att in message.attachments:
        maintype, _, subtype = att.mime_type.partition("/")
        mime.add_attachment(att.content, maintype=maintype or "application", subtype=subtype or "octet-stream",
                            filename=att.filename)
    return mime


class SmtpEmailProvider:
    """SMTP delivery (STARTTLS on 587 by default, implicit TLS on 465)."""

    def __init__(self, settings: Settings) -> None:
        """Capture the SMTP settings."""
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._username = settings.smtp_username
        self._password = settings.smtp_password
        self._use_tls = settings.smtp_use_tls

    def _send_blocking(self, message: EmailMessage, sender: str) -> None:
        """Blocking SMTP conversation (runs in a worker thread)."""
        mime = build_mime(message, sender)
        if self._port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(self._host, self._port, timeout=SMTP_TIMEOUT_SECONDS)
        else:
            server = smtplib.SMTP(self._host, self._port, timeout=SMTP_TIMEOUT_SECONDS)
        with server:
            if self._port != 465 and self._use_tls:
                server.starttls()
            if self._username:
                server.login(self._username, self._password)
            server.send_message(mime, from_addr=parseaddr(sender)[1] or sender, to_addrs=[message.to])

    async def send(self, message: EmailMessage, sender: str) -> None:
        """Send without blocking the event loop."""
        await asyncio.to_thread(self._send_blocking, message, sender)


def get_email_provider(settings: Settings | None = None) -> EmailProvider:
    """The configured provider; console when unset, unknown or an SMTP host is missing."""
    settings = settings or get_settings()
    name = (settings.email_provider or "console").strip().lower()
    if name == "smtp":
        if settings.smtp_host:
            return SmtpEmailProvider(settings)
        logger.warning("EMAIL_PROVIDER=smtp but SMTP_HOST is empty — falling back to the console provider")
    elif name != "console":
        logger.warning("Unknown EMAIL_PROVIDER %r — falling back to the console provider", name)
    return ConsoleEmailProvider()


def sender_address(settings: Settings | None = None) -> str:
    """The From header: EMAIL_FROM, normalised."""
    raw = (settings or get_settings()).email_from
    name, addr = parseaddr(raw)
    return formataddr((name, addr)) if addr else raw


async def send_email(message: EmailMessage) -> bool:
    """Send via the configured provider. Never raises; True when the provider accepted the message."""
    if not message.to or "@" not in message.to:
        logger.warning("email not sent: invalid recipient %r (subject %r)", message.to, message.subject)
        return False
    try:
        settings = get_settings()
        await get_email_provider(settings).send(message, sender_address(settings))
        return True
    except Exception:
        logger.exception("email send failed (to=%s subject=%r)", message.to, message.subject)
        return False
