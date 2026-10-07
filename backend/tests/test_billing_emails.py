"""Unit tests (no database) for the email abstraction and the billing email templates."""

from __future__ import annotations

import logging
import smtplib

import pytest

from app.config import get_settings
from app.services import email_service
from app.services.billing.emails import KINDS, render_email
from app.services.email_service import (
    ConsoleEmailProvider,
    EmailAttachment,
    EmailMessage,
    SmtpEmailProvider,
    build_mime,
    get_email_provider,
    send_email,
)

CTX = {
    "payment_success": dict(name="Riya", plan="Growth", amount="Rs. 14,158.82", invoice_number="ST24/2026-27/0001",
                            start="07 Oct 2026", end="06 Nov 2026"),
    "payment_failed": dict(name="Riya", plan="Growth", amount="Rs. 14,158.82", reason="Card declined"),
    "expiring": dict(name="Riya", plan="Growth", days=3, end="10 Oct 2026"),
    "grace_started": dict(name="Riya", grace_ends="10 Oct 2026"),
    "expired": dict(name="Riya"),
    "refund_revoked": dict(name="Riya", plan="Growth", amount="Rs. 14,158.82", grace_ends="10 Oct 2026"),
}


def _settings(**kw):
    return get_settings().model_copy(update=kw)


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_renders_subject_text_and_html(kind):
    subject, text, html = render_email(kind, CTX[kind])

    assert subject and "Riya" in text and "/billing" in text and "<a href=" in html


def test_payment_success_mentions_the_invoice_and_period():
    subject, text, _ = render_email("payment_success", CTX["payment_success"])

    assert "Growth plan is active" in subject
    assert "ST24/2026-27/0001" in text and "07 Oct 2026" in text and "06 Nov 2026" in text


def test_expiring_subject_pluralises():
    assert "3 days" in render_email("expiring", CTX["expiring"])[0]
    assert "1 day" in render_email("expiring", {**CTX["expiring"], "days": 1})[0]
    assert "1 days" not in render_email("expiring", {**CTX["expiring"], "days": 1})[0]


def test_html_escapes_customer_supplied_values():
    _, _, html = render_email("expired", {"name": "<script>alert(1)</script>"})

    assert "<script>" not in html and "&lt;script&gt;" in html


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        render_email("nope", {})


def test_default_provider_is_console_and_never_needs_config():
    assert isinstance(get_email_provider(_settings(email_provider="console")), ConsoleEmailProvider)
    assert isinstance(get_email_provider(_settings(email_provider="")), ConsoleEmailProvider)


def test_smtp_without_a_host_and_unknown_providers_fall_back_to_console(caplog):
    with caplog.at_level(logging.WARNING):
        assert isinstance(get_email_provider(_settings(email_provider="smtp", smtp_host="")), ConsoleEmailProvider)
        assert isinstance(get_email_provider(_settings(email_provider="carrier-pigeon")), ConsoleEmailProvider)

    assert "SMTP_HOST is empty" in caplog.text and "Unknown EMAIL_PROVIDER" in caplog.text


def test_smtp_with_a_host_selects_the_smtp_provider():
    assert isinstance(get_email_provider(_settings(email_provider="smtp", smtp_host="smtp.example.com")), SmtpEmailProvider)


def test_build_mime_carries_text_html_and_attachment():
    msg = EmailMessage(to="a@b.com", subject="Hi", text="plain", html="<b>rich</b>",
                       attachments=[EmailAttachment("inv.pdf", b"%PDF-1.4 x")])

    mime = build_mime(msg, "SellerTalk24 <billing@sellertalk24.com>")

    assert mime["To"] == "a@b.com" and mime["Subject"] == "Hi"
    types = [p.get_content_type() for p in mime.walk()]
    assert "text/plain" in types and "text/html" in types and "application/pdf" in types
    assert [p.get_filename() for p in mime.iter_attachments()] == ["inv.pdf"]


class FakeSMTP:
    """Records the SMTP conversation instead of opening a socket."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout, self.calls = host, port, timeout, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, mime, from_addr=None, to_addrs=None):
        self.calls.append(("send", from_addr, tuple(to_addrs), mime["Subject"]))


@pytest.mark.asyncio
async def test_smtp_provider_uses_starttls_login_and_envelope_addresses(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    provider = SmtpEmailProvider(_settings(smtp_host="smtp.example.com", smtp_port=587, smtp_username="u", smtp_password="p", smtp_use_tls=True))

    await provider.send(EmailMessage(to="a@b.com", subject="S", text="t"), "SellerTalk24 <billing@sellertalk24.com>")

    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.example.com", 587, email_service.SMTP_TIMEOUT_SECONDS)
    assert smtp.calls == ["starttls", ("login", "u", "p"), ("send", "billing@sellertalk24.com", ("a@b.com",), "S")]


@pytest.mark.asyncio
async def test_send_email_returns_false_instead_of_raising_when_the_provider_fails(monkeypatch, caplog):
    class Boom:
        async def send(self, message, sender):
            raise ConnectionError("smtp down")

    monkeypatch.setattr(email_service, "get_email_provider", lambda settings=None: Boom())

    with caplog.at_level(logging.ERROR):
        assert await send_email(EmailMessage(to="a@b.com", subject="S", text="t")) is False
    assert "email send failed" in caplog.text


@pytest.mark.asyncio
async def test_send_email_rejects_an_invalid_recipient_without_calling_the_provider(monkeypatch):
    called = []
    monkeypatch.setattr(email_service, "get_email_provider", lambda settings=None: called.append(1))

    assert await send_email(EmailMessage(to="not-an-email", subject="S", text="t")) is False
    assert called == []


@pytest.mark.asyncio
async def test_console_provider_logs_the_message_and_send_email_reports_success(caplog):
    with caplog.at_level(logging.INFO):
        ok = await send_email(EmailMessage(to="a@b.com", subject="Receipt", text="hello", attachments=[EmailAttachment("x.pdf", b"1")]))

    assert ok is True
    assert "EMAIL (console, not sent)" in caplog.text and "Receipt" in caplog.text and "x.pdf" in caplog.text


def test_refund_revoked_email_names_the_refund_and_the_grace_end():
    subject, text, _ = render_email("refund_revoked", CTX["refund_revoked"])

    assert "refunded" in subject and "Growth" in subject
    assert "Rs. 14,158.82" in text and "10 Oct 2026" in text and "credit note" in text
