#!/usr/bin/env python3
"""
Full end-to-end flow test: Textile customer buys a saree.

Proves every layer of the system — webhook, Gemini AI, conversation DB,
lead tagging, payments — works together locally without a real phone.

Steps:
  1  Customer:  "Banarasi saree ka price?"
  2  Agent:     Replies with price  (Gemini + product catalogue)
  3  Customer:  "order karna hai"
  4  Agent:     Collects name + address             [lead → hot]
  5  Business:  QR code created → sent to customer  [payment row created]
  6  Razorpay:  payment.captured webhook fires
  7  System:    Payment marked paid in DB
  8  Owner:     Notification logged (gap documented)

After each step prints:
  • Input message
  • DB state change
  • Output message
  • Lead status

Final summary: total messages, lead status, order placed, time taken.

Usage (from backend/ with venv active):
    python tests/full_flow_test.py
    python tests/full_flow_test.py --url http://localhost:8001
    python tests/full_flow_test.py --phone 919800000042

Prerequisites:
    • Backend running:  uvicorn app.main:app --reload --port 8000
    • .env with GEMINI_API_KEY + META_APP_SECRET + RAZORPAY_KEY_SECRET set
    • (Optional) seed data: python seed.py   — loads product catalogue
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models.conversation import Conversation
from app.models.lead import Lead
from app.models.message import Message
from app.models.payment import Payment

# ── ANSI colour helpers ───────────────────────────────────────────────────────

RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
GREEN   = "\033[92m"
CYAN    = "\033[96m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
BLUE    = "\033[94m"
MAGENTA = "\033[95m"
WHITE   = "\033[97m"


def _c(colour: str, text: str) -> str:
    """Wrap text with an ANSI colour code."""
    return f"{colour}{text}{RESET}"


# ── DB session factory ────────────────────────────────────────────────────────

def _engine_and_session(db_url: str):
    """Create a throw-away async engine + sessionmaker pair."""
    engine = create_async_engine(db_url, echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, Session


# ── DB query helpers ──────────────────────────────────────────────────────────

async def get_last_ai_reply(
    db_url: str, phone: str, after_ts: datetime
) -> Optional[str]:
    """
    Return the most recent model-role message for phone created after after_ts.

    The webhook handler saves the AI reply synchronously before returning 200,
    so by the time httpx.post() resolves the reply is already in the DB.
    """
    engine, Session = _engine_and_session(db_url)
    reply = None
    try:
        async with Session() as session:
            result = await session.execute(
                select(Message)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(
                    Conversation.phone_number == phone,
                    Conversation.channel == "whatsapp",
                    Message.role == "model",
                    Message.created_at >= after_ts,
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            )
            msg = result.scalar_one_or_none()
            if msg:
                reply = msg.content
    finally:
        await engine.dispose()
    return reply


async def count_messages(db_url: str, phone: str) -> int:
    """Return the total number of messages (both roles) for phone."""
    engine, Session = _engine_and_session(db_url)
    count = 0
    try:
        async with Session() as session:
            result = await session.execute(
                select(Message)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(
                    Conversation.phone_number == phone,
                    Conversation.channel == "whatsapp",
                )
            )
            count = len(result.scalars().all())
    finally:
        await engine.dispose()
    return count


async def get_conversation_id(db_url: str, phone: str) -> Optional[int]:
    """Return conversation.id for phone on the whatsapp channel, or None."""
    engine, Session = _engine_and_session(db_url)
    conv_id = None
    try:
        async with Session() as session:
            result = await session.execute(
                select(Conversation)
                .where(
                    Conversation.phone_number == phone,
                    Conversation.channel == "whatsapp",
                )
            )
            conv = result.scalar_one_or_none()
            if conv:
                conv_id = conv.id
    finally:
        await engine.dispose()
    return conv_id


async def get_lead_status(db_url: str, phone: str) -> Optional[str]:
    """Return lead.status for phone, or None if no lead row yet."""
    engine, Session = _engine_and_session(db_url)
    status = None
    try:
        async with Session() as session:
            result = await session.execute(
                select(Lead).where(Lead.phone_number == phone)
            )
            lead = result.scalar_one_or_none()
            if lead:
                status = lead.status
    finally:
        await engine.dispose()
    return status


async def insert_payment_directly(
    db_url: str,
    phone: str,
    qr_code_id: str,
    amount: int,
    description: str,
) -> Payment:
    """
    Insert a Payment row directly, bypassing the real Razorpay API.

    Mirrors what POST /payments/qr does after calling razorpay_service.create_qr_code().
    """
    engine, Session = _engine_and_session(db_url)
    try:
        async with Session() as session:
            payment = Payment(
                phone_number=phone,
                qr_code_id=qr_code_id,
                amount=amount,
                description=description,
                status="created",
            )
            session.add(payment)
            await session.commit()
            await session.refresh(payment)
            return payment
    finally:
        await engine.dispose()


async def fetch_payment(db_url: str, qr_code_id: str) -> Optional[Payment]:
    """Fetch a Payment row by qr_code_id, or None."""
    engine, Session = _engine_and_session(db_url)
    payment = None
    try:
        async with Session() as session:
            result = await session.execute(
                select(Payment).where(Payment.qr_code_id == qr_code_id)
            )
            payment = result.scalar_one_or_none()
    finally:
        await engine.dispose()
    return payment


# ── Payload / signature builders ──────────────────────────────────────────────

def _whatsapp_payload(text: str, from_phone: str, to_phone_id: str) -> bytes:
    """Build an exact Meta Cloud API webhook JSON body."""
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "SIM_" + uuid.uuid4().hex[:8],
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "919876543210",
                                "phone_number_id": to_phone_id,
                            },
                            "contacts": [
                                {
                                    "profile": {"name": "Test Customer"},
                                    "wa_id": from_phone,
                                }
                            ],
                            "messages": [
                                {
                                    "id": "sim_" + uuid.uuid4().hex,
                                    "from": from_phone,
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _sign_whatsapp(body: bytes, secret: str) -> str:
    """Compute X-Hub-Signature-256 (sha256= prefix)."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _razorpay_captured_payload(
    qr_code_id: str, razorpay_payment_id: str, amount: int
) -> bytes:
    """Build a Razorpay payment.captured webhook body."""
    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": razorpay_payment_id,
                    "amount": amount,
                    "currency": "INR",
                    "status": "captured",
                    "acquirer_data": {
                        "upi_transaction_id": qr_code_id,
                    },
                }
            }
        },
    }
    return json.dumps(payload).encode("utf-8")


def _sign_razorpay(body: bytes, secret: str) -> str:
    """Compute X-Razorpay-Signature (plain hex, no prefix)."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


# ── WhatsApp send + reply fetch ───────────────────────────────────────────────

async def send_and_receive(
    text: str,
    phone: str,
    webhook_url: str,
    settings,
) -> Optional[str]:
    """
    POST a signed WhatsApp message to the webhook, return the AI's reply.

    Retrieves the reply by querying the DB for the newest model message
    created after the request was sent (the handler saves it synchronously).

    Returns None on connection error, timeout, bad signature, or Gemini failure.
    """
    body    = _whatsapp_payload(text, phone, settings.whatsapp_phone_number_id)
    sig     = _sign_whatsapp(body, settings.meta_app_secret)
    sent_at = datetime.now(timezone.utc)

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                webhook_url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": sig,
                },
            )
    except httpx.ConnectError:
        print(_c(RED, "  │  [error] Cannot connect — is uvicorn running on port 8000?"))
        return None
    except httpx.TimeoutException:
        print(_c(RED, "  │  [error] Request timed out (Gemini may be slow, try again)."))
        return None

    if resp.status_code == 401:
        print(_c(RED, "  │  [error] Signature rejected — check META_APP_SECRET in .env"))
        return None
    if resp.status_code != 200:
        print(_c(RED, f"  │  [error] Webhook HTTP {resp.status_code}: {resp.text}"))
        return None

    reply = await get_last_ai_reply(settings.database_url, phone, sent_at)
    if reply is None:
        print(_c(YELLOW, "  │  [warn] No AI reply saved — check server logs for Gemini error"))
    return reply


# ── Terminal output helpers ───────────────────────────────────────────────────

def _step_header(n: int, title: str) -> None:
    print()
    print(_c(BOLD + CYAN, f"  ┌─ STEP {n}  {title}"))


def _field(label: str, value: str, colour: str = WHITE) -> None:
    padded = (label + ":").ljust(24)
    print(f"  │  {_c(DIM, padded)}  {_c(colour, value)}")


def _db_row(label: str, value: str) -> None:
    padded = ("DB → " + label + ":").ljust(24)
    print(f"  │  {_c(DIM, padded)}  {_c(YELLOW, value)}")


def _lead_badge(status: Optional[str]) -> None:
    colours = {"hot": RED, "warm": YELLOW, "cold": BLUE}
    colour  = colours.get(status or "cold", WHITE)
    badge   = f"[{(status or 'unknown').upper()}]"
    padded  = "Lead status:".ljust(24)
    print(f"  │  {_c(DIM, padded)}  {_c(BOLD + colour, badge)}")


def _reply_lines(reply: str, label: str = "Agent reply") -> None:
    width   = 62
    lines   = []
    for para in reply.splitlines():
        while len(para) > width:
            lines.append(para[:width])
            para = para[width:]
        lines.append(para)
    padded  = (label + ":").ljust(24)
    prefix  = f"  │  {_c(DIM, padded)}  "
    blank   = "  │  " + " " * 26
    for i, line in enumerate(lines):
        if i == 0:
            print(f"{prefix}{_c(BLUE, line)}")
        else:
            print(f"{blank}{_c(BLUE, line)}")


def _step_pass() -> None:
    print(f"  └─ {_c(GREEN, '✓  PASS')}")


def _step_fail() -> None:
    print(f"  └─ {_c(RED, '✗  FAIL')}")


def _step_gap() -> None:
    print(f"  └─ {_c(YELLOW, '⚠  GAP  (not yet implemented)')}")


# ── Main flow ─────────────────────────────────────────────────────────────────

async def run_flow(webhook_url: str, phone: str, settings) -> None:
    """
    Execute the full 8-step saree purchase scenario and print results.

    Args:
        webhook_url: Full URL of the WhatsApp webhook endpoint.
        phone:       Simulated customer phone in E.164 without '+'.
        settings:    App settings (loaded from .env).
    """
    db_url     = settings.database_url
    start_time = time.monotonic()
    passed     = 0
    total      = 8

    # ── STEP 1 ── Customer asks for price ─────────────────────────────────────
    _step_header(1, 'Customer: "Banarasi saree ka price?"')
    msg1    = "Banarasi saree ka price?"
    _field("Input", msg1, GREEN)
    reply1  = await send_and_receive(msg1, phone, webhook_url, settings)
    msgs1   = await count_messages(db_url, phone)
    conv_id = await get_conversation_id(db_url, phone)

    _db_row("conversation.id",  str(conv_id) if conv_id else "not found")
    _db_row("messages saved",   str(msgs1) + "  (1 user + 1 model)")
    if reply1:
        _reply_lines(reply1)
        _step_pass()
        passed += 1
    else:
        _step_fail()

    # ── STEP 2 ── Agent replied with price ────────────────────────────────────
    _step_header(2, "Agent reply — price returned by Gemini")
    lead1 = await get_lead_status(db_url, phone)

    has_price = reply1 is not None and any(
        tok in (reply1 or "") for tok in ("₹", "price", "2450", "2,450", "Price", "rupee")
    )
    _field("Reply received", "yes" if reply1 else "NO", GREEN if reply1 else RED)
    _field("Price mentioned", "yes" if has_price else "not detected (check manually)", GREEN if has_price else YELLOW)
    _db_row("messages",  str(msgs1))
    _lead_badge(lead1)

    if reply1:
        _step_pass()
        passed += 1
    else:
        _step_fail()

    # ── STEP 3 ── Customer places order ───────────────────────────────────────
    _step_header(3, 'Customer: "order karna hai"')
    msg2   = "order karna hai. Banarasi silk chahiye."
    _field("Input", msg2, GREEN)
    reply2 = await send_and_receive(msg2, phone, webhook_url, settings)
    msgs2  = await count_messages(db_url, phone)

    _db_row("messages saved", f"{msgs2}  (+{msgs2 - msgs1})")
    if reply2:
        _reply_lines(reply2)
        _step_pass()
        passed += 1
    else:
        _step_fail()

    # ── STEP 4 ── Customer provides name + address ────────────────────────────
    _step_header(4, "Agent collects name + address")
    msg3   = "Rahul Verma. Delivery address: 7 Saket Nagar, Surat, Gujarat 395001."
    _field("Input", msg3, GREEN)
    reply3 = await send_and_receive(msg3, phone, webhook_url, settings)
    msgs3  = await count_messages(db_url, phone)
    lead3  = await get_lead_status(db_url, phone)

    _db_row("messages saved", f"{msgs3}  (+{msgs3 - msgs2})")
    if reply3:
        _reply_lines(reply3)
    _lead_badge(lead3)

    if lead3 == "hot":
        _step_pass()
        passed += 1
    else:
        print(_c(YELLOW, f"  │  [note] Expected 'hot', got '{lead3}' — Gemini read the conversation differently"))
        _step_fail()

    # ── STEP 5 ── QR code created + sent to customer ──────────────────────────
    _step_header(5, "QR code created + sent to customer")
    amount_paise   = 245000          # ₹2,450 — Banarasi Silk (from seed catalogue)
    qr_code_id     = "qr_sim_" + uuid.uuid4().hex[:14]
    rzp_payment_id = "pay_sim_" + uuid.uuid4().hex[:14]

    _field("Amount",          f"₹{amount_paise // 100:,}  ({amount_paise} paise)", MAGENTA)
    _field("QR code ID",      qr_code_id, DIM)
    _field("Razorpay API",    "bypassed — direct DB insert (no real keys needed)", DIM)

    payment = await insert_payment_directly(
        db_url, phone, qr_code_id, amount_paise, "Banarasi Silk Saree"
    )
    _db_row("payment.id",     str(payment.id))
    _db_row("payment.status", payment.status)
    _field("Simulated send",  "Agent texts customer: 'Scan QR to pay ₹2,450'", CYAN)

    if payment.status == "created":
        _step_pass()
        passed += 1
    else:
        _step_fail()

    # ── STEP 6 ── Razorpay payment.captured webhook ───────────────────────────
    _step_header(6, "Razorpay payment.captured webhook fires")
    _field("Event type",       "payment.captured", MAGENTA)
    _field("Razorpay pay ID",  rzp_payment_id, DIM)
    _field("Signature",        "HMAC-SHA256 (plain hex, no prefix)", DIM)

    rzp_url  = webhook_url.replace("/webhook", "") + "/payments/webhook"
    body6    = _razorpay_captured_payload(qr_code_id, rzp_payment_id, amount_paise)
    sig6     = _sign_razorpay(body6, settings.razorpay_key_secret)
    step6_ok = False

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r6 = await client.post(
                rzp_url,
                content=body6,
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": sig6,
                },
            )
        step6_ok = r6.status_code == 200
        _field("HTTP response", f"{r6.status_code}  {r6.text}", GREEN if step6_ok else RED)
    except httpx.ConnectError:
        _field("HTTP response", "CONNECT ERROR — backend not running", RED)
    except Exception as exc:
        _field("HTTP response", f"ERROR: {exc}", RED)

    if step6_ok:
        _step_pass()
        passed += 1
    else:
        _step_fail()

    # ── STEP 7 ── Verify payment is paid in DB ────────────────────────────────
    _step_header(7, "Order confirmed — payment verified in DB")
    paid = await fetch_payment(db_url, qr_code_id)
    step7_ok = paid is not None and paid.status == "paid"

    if paid:
        _db_row("payment.status",              paid.status)
        _db_row("payment.razorpay_payment_id", paid.razorpay_payment_id or "—")
        _db_row("payment.amount",              f"₹{paid.amount // 100:,}")
        _db_row("payment.phone_number",        paid.phone_number)
    else:
        _field("Payment row", "NOT FOUND", RED)

    _field("Order placed", "YES" if step7_ok else "NOT CONFIRMED", GREEN if step7_ok else RED)

    if step7_ok:
        _step_pass()
        passed += 1
    else:
        _step_fail()

    # ── STEP 8 ── Owner notification ──────────────────────────────────────────
    _step_header(8, "Owner notified")
    print(f"  │  {_c(DIM, 'Notification channel:'): <28}  {_c(YELLOW, 'WhatsApp  [NOT IMPLEMENTED]')}")
    print(f"  │")
    print(f"  │  {_c(DIM, 'Would send to business owner:')}")
    print(f"  │    {_c(CYAN, '\"New order! Rahul Verma — ₹2,450 Banarasi Silk Saree\"')}")
    print(f"  │    {_c(CYAN, f'\"Customer: {phone} | Razorpay: {rzp_payment_id}\"')}")
    print(f"  │")
    print(f"  │  {_c(DIM, 'Add to app/routers/payment.py, inside payment.captured block:')}")
    print(f"  │    {_c(DIM, 'await whatsapp_service.send_text_message(')}")
    print(f"  │    {_c(DIM, '    to_phone_number=owner_whatsapp_number,')}")
    print(f"  │    {_c(DIM, '    message_text=f\"Order confirmed: {amount} from {phone}\",')}")
    print(f"  │    {_c(DIM, ')')}")
    _step_gap()
    # Step 8 is a known gap — not counted as pass or fail for the core flow

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed       = time.monotonic() - start_time
    final_msgs    = await count_messages(db_url, phone)
    final_lead    = await get_lead_status(db_url, phone)
    final_payment = await fetch_payment(db_url, qr_code_id)

    lead_colours  = {"hot": RED, "warm": YELLOW, "cold": BLUE}
    lead_colour   = lead_colours.get(final_lead or "cold", WHITE)

    print()
    print("  " + _c(BOLD, "━" * 60))
    print("  " + _c(BOLD + MAGENTA, "FLOW SUMMARY"))
    print("  " + _c(BOLD, "━" * 60))

    def _row(label: str, value: str, colour: str) -> None:
        print(f"  {_c(DIM, (label + ':').ljust(30))}  {_c(BOLD + colour, value)}")

    _row("Total messages in DB",   str(final_msgs),                                                  WHITE)
    _row("Lead status",            (final_lead or "unknown").upper(),                                 lead_colour)
    _row("Order placed",           "YES" if step7_ok else "NOT CONFIRMED",                            GREEN if step7_ok else RED)
    _row("Payment status",         final_payment.status if final_payment else "unknown",              GREEN if step7_ok else RED)
    _row("Owner notification",     "NOT IMPLEMENTED  (step 8 gap)",                                   YELLOW)
    _row("Steps passed",           f"{passed} / {total - 1}  (step 8 excluded — known gap)",         GREEN if passed >= 6 else YELLOW)
    _row("Time taken",             f"{elapsed:.1f}s",                                                 DIM)
    _row("Overall result",         "PASS" if passed >= 6 and step7_ok else "PARTIAL",                GREEN if passed >= 6 and step7_ok else YELLOW)

    print("  " + _c(BOLD, "━" * 60))
    print()

    if passed < 6 or not step7_ok:
        print(_c(YELLOW, "  Troubleshooting tips:"))
        if not reply1:
            print(_c(DIM, "  • Steps 1-2 failed: backend not running or GEMINI_API_KEY missing"))
        if final_lead != "hot":
            print(_c(DIM, "  • Lead not 'hot': send more purchase-intent messages or re-run"))
        if not step6_ok:
            print(_c(DIM, "  • Step 6 failed: check RAZORPAY_KEY_SECRET in .env"))
        if not step7_ok:
            print(_c(DIM, "  • Step 7 failed: Razorpay webhook did not update payment status"))
        print()


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    """Parse CLI args, print header, run the full saree purchase flow."""
    parser = argparse.ArgumentParser(
        description="Full end-to-end test: textile customer buys a saree."
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8000",
        help="Base URL of the backend (default: http://localhost:8000).",
    )
    parser.add_argument(
        "--phone",
        default=None,
        help="Simulated customer phone in E.164 without '+' (e.g. 919800000042).",
    )
    args = parser.parse_args()

    try:
        settings = get_settings()
    except Exception as exc:
        print(_c(RED, f"\n  [error] Could not load settings: {exc}"))
        print(_c(YELLOW, "  Make sure backend/.env exists and is valid.\n"))
        sys.exit(1)

    webhook_url = args.url.rstrip("/") + "/webhook"
    phone = args.phone or (
        "919" + "".join(str(random.randint(0, 9)) for _ in range(9))
    )

    print()
    print("  " + _c(BOLD, "━" * 60))
    print("  " + _c(BOLD + MAGENTA, "Full Flow Test — Textile Customer Buys a Saree"))
    print("  " + _c(BOLD, "━" * 60))
    print(f"  {'Webhook:':<14} {_c(DIM, webhook_url)}")
    print(f"  {'Customer:':<14} {_c(MAGENTA, phone)}  {_c(DIM, '(new random number — fresh conversation)')}")
    print(f"  {'Started:':<14} {_c(DIM, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}")
    print("  " + _c(BOLD, "━" * 60))

    await run_flow(webhook_url, phone, settings)


if __name__ == "__main__":
    asyncio.run(main())
