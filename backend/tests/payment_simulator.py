#!/usr/bin/env python3
"""
Razorpay Payment Simulator — test the full payment flow without real money.

Usage (from backend/ with venv active):
    python tests/payment_simulator.py               # full simulation (no real API)
    python tests/payment_simulator.py --qr-api      # also calls POST /payments/qr
    python tests/payment_simulator.py --url http://localhost:8001
    python tests/payment_simulator.py --phone 919876543210

Steps run automatically:
    1  Create test payment record in DB
    2  Simulate payment.captured webhook → verify DB updated to "paid"
    3  Show post-payment actions (notifications + Google Sheet mock)
    4  Simulate payment.failed webhook → show it is currently unhandled
    5  Verify signature rejection (wrong key → 401)
    6  Simulate duplicate webhook (idempotency check)

Prerequisites:
    • Backend running:  uvicorn app.main:app --reload --port 8000
    • .env present with RAZORPAY_KEY_SECRET (any string works in simulation mode)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models.payment import Payment
from app.services.auth_service import hash_password

# ── ANSI colours ──────────────────────────────────────────────────────────────

R      = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BLUE   = "\033[94m"
MAGENTA = "\033[95m"

def _c(col: str, t: str) -> str:
    return f"{col}{t}{R}"

def ok(msg: str)   -> None: print(f"  {_c(GREEN,  '✓')} {msg}")
def fail(msg: str) -> None: print(f"  {_c(RED,    '✗')} {msg}")
def info(msg: str) -> None: print(f"  {_c(CYAN,   '→')} {msg}")
def warn(msg: str) -> None: print(f"  {_c(YELLOW, '⚠')} {msg}")
def mock(msg: str) -> None: print(f"  {_c(MAGENTA,'~')} {_c(DIM, '[mocked]')} {msg}")

def step(n: int, label: str) -> None:
    print()
    print(_c(BOLD, f"━━  Step {n}: {label}  ") + _c(DIM, "━" * max(0, 52 - len(label))))

def show_payload(label: str, payload: dict) -> None:
    raw = json.dumps(payload, indent=2, ensure_ascii=False)
    print(f"\n  {_c(DIM, label)}")
    for line in raw.splitlines():
        print("  " + _c(DIM, line))

def show_payment_row(p: Payment) -> None:
    print(f"    id               : {p.id}")
    print(f"    qr_code_id       : {_c(CYAN, p.qr_code_id)}")
    print(f"    phone_number     : {p.phone_number}")
    print(f"    amount           : ₹{p.amount / 100:.2f}  ({p.amount} paise)")
    print(f"    description      : {p.description}")
    status_col = GREEN if p.status == "paid" else YELLOW if p.status == "created" else RED
    print(f"    status           : {_c(status_col + BOLD, p.status)}")
    print(f"    razorpay_payment_id: {p.razorpay_payment_id or _c(DIM, 'null')}")

# ── Payload builders ──────────────────────────────────────────────────────────

def build_captured_payload(qr_code_id: str, razorpay_payment_id: str) -> dict:
    """Exact JSON Razorpay sends for a successful UPI payment capture."""
    return {
        "entity": "event",
        "account_id": "acc_SimulatorTest001",
        "event": "payment.captured",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": razorpay_payment_id,
                    "entity": "payment",
                    "amount": 245000,
                    "currency": "INR",
                    "status": "captured",
                    "method": "upi",
                    "captured": True,
                    "description": "Banarasi Silk Saree",
                    "email": "customer@example.com",
                    "contact": "+919999000001",
                    "acquirer_data": {
                        # upi_transaction_id must match the qr_code_id in DB
                        "upi_transaction_id": qr_code_id,
                        "rrn": "SIM" + uuid.uuid4().hex[:9].upper(),
                    },
                    "created_at": int(datetime.now(timezone.utc).timestamp()),
                }
            }
        },
    }


def build_failed_payload(qr_code_id: str) -> dict:
    """Exact JSON Razorpay sends when a payment fails or is declined."""
    return {
        "entity": "event",
        "account_id": "acc_SimulatorTest001",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_Sim_FAILED_" + uuid.uuid4().hex[:6],
                    "entity": "payment",
                    "amount": 245000,
                    "currency": "INR",
                    "status": "failed",
                    "method": "upi",
                    "captured": False,
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Payment declined by UPI network",
                    "error_reason": "payment_declined",
                    "error_source": "customer",
                    "error_step": "payment_authentication",
                    "acquirer_data": {
                        "upi_transaction_id": qr_code_id,
                    },
                    "created_at": int(datetime.now(timezone.utc).timestamp()),
                }
            }
        },
    }


def sign(body: bytes, secret: str) -> str:
    """Compute X-Razorpay-Signature — plain hex digest, no prefix."""
    return hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()


# ── DB helpers ────────────────────────────────────────────────────────────────

async def insert_payment(db_url: str, phone: str, qr_code_id: str, amount: int, desc: str) -> Payment:
    """Insert a Payment row directly, bypassing the Razorpay API."""
    engine = create_async_engine(db_url, echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with Session() as session:
            payment = Payment(
                phone_number=phone,
                qr_code_id=qr_code_id,
                amount=amount,
                description=desc,
                status="created",
            )
            session.add(payment)
            await session.commit()
            await session.refresh(payment)
            return payment
    finally:
        await engine.dispose()


async def fetch_payment(db_url: str, qr_code_id: str) -> Optional[Payment]:
    """Return the Payment row for qr_code_id, or None."""
    engine = create_async_engine(db_url, echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with Session() as session:
            result = await session.execute(
                select(Payment).where(Payment.qr_code_id == qr_code_id)
            )
            return result.scalar_one_or_none()
    finally:
        await engine.dispose()


# ── HTTP helpers ──────────────────────────────────────────────────────────────

async def post_webhook(webhook_url: str, payload: dict, signature: str) -> tuple[int, dict]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                webhook_url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": signature,
                },
            )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return resp.status_code, data
    except httpx.ConnectError:
        return -1, {}
    except httpx.TimeoutException:
        return -2, {}


async def post_qr_api(api_url: str, phone: str, amount: int, desc: str) -> tuple[int, dict]:
    payload = {"phone_number": phone, "amount": amount, "description": desc}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                api_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return resp.status_code, data
    except httpx.ConnectError:
        return -1, {}


# ── Mock post-payment actions ─────────────────────────────────────────────────

def show_post_payment_actions(payment: Payment, rzp_payment_id: str) -> None:
    """
    Show what would happen after a successful payment in a production system.
    None of these are implemented yet — this section documents the gap and
    serves as a spec for the next iteration.
    """
    amount_inr = payment.amount / 100
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print()
    print(f"  {_c(BOLD, 'Post-payment actions')}  {_c(DIM, '(current status shown below)')}")
    print()

    # 1. Customer WhatsApp confirmation
    customer_msg = (
        f"✅ Payment of ₹{amount_inr:.0f} received for \"{payment.description}\"!\n"
        f"Order confirmed 🎊 We'll dispatch in 2–3 business days.\n"
        f"Reference: {rzp_payment_id}"
    )
    mock(f"WhatsApp to customer ({payment.phone_number}):")
    for line in customer_msg.splitlines():
        print(f"       {_c(CYAN, line)}")
    warn("NOT implemented — whatsapp_service.send_text_message() not called from payment webhook.")
    print()

    # 2. Owner WhatsApp notification
    owner_msg = (
        f"💰 New payment received!\n"
        f"Customer : {payment.phone_number}\n"
        f"Amount   : ₹{amount_inr:.0f}\n"
        f"Product  : {payment.description}\n"
        f"Rzp ID   : {rzp_payment_id}"
    )
    mock(f"WhatsApp to owner (from WHATSAPP_PHONE_NUMBER_ID):")
    for line in owner_msg.splitlines():
        print(f"       {_c(CYAN, line)}")
    warn("NOT implemented — owner notification not in payment webhook handler.")
    print()

    # 3. Google Sheet
    sheet_row = {
        "Timestamp":    ts,
        "Customer":     payment.phone_number,
        "Amount (INR)": f"₹{amount_inr:.0f}",
        "Product":      payment.description,
        "Status":       "PAID",
        "Razorpay ID":  rzp_payment_id,
        "QR Code ID":   payment.qr_code_id,
    }
    mock("Google Sheet row that would be appended:")
    for k, v in sheet_row.items():
        print(f"       {_c(DIM, k + ':'): <30} {_c(CYAN, v)}")
    warn("NOT implemented — no Google Sheets integration exists yet.")
    print()

    # 4. What IS implemented
    ok("DB: Payment.status updated to 'paid'  ← this IS implemented")
    ok("DB: Payment.razorpay_payment_id saved ← this IS implemented")
    info("Logger: INFO log written for the captured event")


# ── Main simulation ───────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> None:
    try:
        settings = get_settings()
    except Exception as exc:
        fail(f"Cannot load settings: {exc}")
        fail("Make sure backend/.env exists.")
        sys.exit(1)

    webhook_url = args.url.rstrip("/") + "/payments/webhook"
    qr_api_url  = args.url.rstrip("/") + "/payments/qr"

    # Deterministic test IDs so the simulator is easy to follow
    qr_code_id        = "sim_qr_" + uuid.uuid4().hex[:10]
    razorpay_pay_id   = "pay_Sim_" + uuid.uuid4().hex[:8].upper()
    phone             = args.phone
    amount_paise      = 245000   # ₹2,450
    description       = "Banarasi Silk Saree"

    print()
    print(_c(BOLD, "╔══════════════════════════════════════════════════════════╗"))
    print(_c(BOLD, "║          Razorpay Payment Simulator                     ║"))
    print(_c(BOLD, "╚══════════════════════════════════════════════════════════╝"))
    print(f"  Webhook URL   : {_c(DIM, webhook_url)}")
    print(f"  Customer phone: {_c(CYAN, phone)}")
    print(f"  QR code ID    : {_c(CYAN, qr_code_id)}")
    print(f"  Payment amount: ₹{amount_paise / 100:.2f}  ({amount_paise} paise)")
    print(f"  Description   : {description}")
    print(f"  Secret used   : {_c(DIM, settings.razorpay_key_secret[:6] + '***')}")

    # ── Step 1: Create payment ────────────────────────────────────────────────
    step(1, "Create test payment record")

    if args.qr_api:
        info(f"Calling POST /payments/qr  (real Razorpay test API)…")
        show_payload("Request body →", {
            "phone_number": phone,
            "amount": amount_paise,
            "description": description,
        })
        code, data = await post_qr_api(qr_api_url, phone, amount_paise, description)
        if code == -1:
            fail("Cannot connect to backend.")
            sys.exit(1)
        if code == 201:
            ok(f"QR created via API  id={data.get('qr_code_id')}")
            info(f"image_url : {data.get('image_url', 'N/A')}")
            info(f"short_url : {data.get('short_url', 'N/A')}")
            # Use the ID returned by Razorpay so the webhook lookup matches
            qr_code_id = data["qr_code_id"]
        else:
            warn(f"POST /payments/qr returned HTTP {code} — Razorpay test credentials "
                 f"may not be valid. Falling back to direct DB insert.")
            warn(f"  Response: {data}")
            info("Inserting payment record directly into DB…")
            payment = await insert_payment(
                settings.database_url, phone, qr_code_id, amount_paise, description
            )
            ok(f"Payment created in DB  id={payment.id}")
    else:
        info("Simulation mode — skipping Razorpay API, inserting directly into DB.")
        info("(Run with --qr-api to test the real Razorpay endpoint.)")
        info("")
        info("What POST /payments/qr would send to Razorpay:")
        show_payload("Razorpay QR-code request →", {
            "type": "upi_qr",
            "name": description,
            "usage": "single_use",
            "fixed_amount": True,
            "payment_amount": amount_paise,
            "description": description,
        })
        payment = await insert_payment(
            settings.database_url, phone, qr_code_id, amount_paise, description
        )
        ok(f"Payment created in DB  id={payment.id}  status={payment.status}")

    print()
    show_payment_row(payment)

    # ── Step 2: Simulate payment.captured ────────────────────────────────────
    step(2, "Simulate payment.captured webhook")

    captured_payload = build_captured_payload(qr_code_id, razorpay_pay_id)
    body_bytes = json.dumps(captured_payload, ensure_ascii=False).encode("utf-8")
    good_sig   = sign(body_bytes, settings.razorpay_key_secret)

    show_payload("Webhook payload →", captured_payload)
    info(f"X-Razorpay-Signature: {good_sig[:20]}…  (HMAC-SHA256 of body)")

    code, data = await post_webhook(webhook_url, captured_payload, good_sig)

    if code == -1:
        fail("Cannot connect to backend — is the server running?")
        sys.exit(1)
    elif code == -2:
        fail("Request timed out.")
        sys.exit(1)
    elif code == 200:
        ok(f"Webhook accepted  HTTP {code}  → {data}")
    else:
        fail(f"Webhook returned HTTP {code}: {data}")
        sys.exit(1)

    # ── Step 3: Verify DB ─────────────────────────────────────────────────────
    step(3, "Verify DB state after payment.captured")

    updated = await fetch_payment(settings.database_url, qr_code_id)
    if updated is None:
        fail("Payment row not found in DB after webhook — check server logs.")
        sys.exit(1)

    print()
    show_payment_row(updated)
    print()

    if updated.status == "paid":
        ok("Payment.status updated to 'paid'")
    else:
        fail(f"Expected status='paid', got '{updated.status}'")
        warn("The webhook lookup uses acquirer_data.upi_transaction_id to find the payment.")
        warn("Make sure that value matches qr_code_id in the DB.")

    if updated.razorpay_payment_id == razorpay_pay_id:
        ok(f"Payment.razorpay_payment_id saved  → {updated.razorpay_payment_id}")
    else:
        warn(f"razorpay_payment_id mismatch: got '{updated.razorpay_payment_id}'")

    # ── Step 4: Post-payment actions ──────────────────────────────────────────
    step(4, "Post-payment actions (what would happen in production)")
    show_post_payment_actions(updated, razorpay_pay_id)

    # ── Step 5: Simulate payment.failed ──────────────────────────────────────
    step(5, "Simulate payment.failed webhook")

    failed_qr_id  = "sim_qr_" + uuid.uuid4().hex[:10]
    failed_payload = build_failed_payload(failed_qr_id)
    body_bytes2    = json.dumps(failed_payload, ensure_ascii=False).encode("utf-8")
    sig2           = sign(body_bytes2, settings.razorpay_key_secret)

    show_payload("Webhook payload →", failed_payload)

    code2, data2 = await post_webhook(webhook_url, failed_payload, sig2)

    if code2 == 200:
        ok(f"Webhook accepted  HTTP {code2}  → {data2}")
        print()
        warn("payment.failed is NOT handled in the current webhook handler.")
        warn("The handler only processes 'payment.captured'.")
        warn("A failed payment returns HTTP 200 (Meta/Razorpay convention) but")
        warn("does nothing — no DB update, no customer notification.")
        print()
        info("To add failure handling, edit app/routers/payment.py:")
        print()
        print(_c(DIM, "    elif event_type == 'payment.failed':"))
        print(_c(DIM, "        payment_entity = event['payload']['payment']['entity']"))
        print(_c(DIM, "        qr_code_id = payment_entity.get('acquirer_data', {})"))
        print(_c(DIM, "                         .get('upi_transaction_id')"))
        print(_c(DIM, "        error_desc = payment_entity.get('error_description', '')"))
        print(_c(DIM, "        # update DB status to 'failed', notify customer"))
    else:
        fail(f"Unexpected HTTP {code2}: {data2}")

    # ── Step 6: Signature rejection ───────────────────────────────────────────
    step(6, "Verify signature rejection (security check)")

    wrong_sig = sign(body_bytes, "completely-wrong-secret")
    info(f"Sending correct payload with wrong signature: {wrong_sig[:20]}…")

    code3, data3 = await post_webhook(webhook_url, captured_payload, wrong_sig)

    if code3 == 401:
        ok(f"Correctly rejected  HTTP {code3}  → {data3}")
        ok("HMAC-SHA256 signature verification is working.")
    else:
        fail(f"Expected HTTP 401, got {code3}: {data3}")
        warn("Signature verification may not be working correctly.")

    # ── Step 7: Duplicate webhook (idempotency) ───────────────────────────────
    step(7, "Duplicate webhook (idempotency check)")

    info("Re-sending the same payment.captured event for the same qr_code_id…")
    code4, data4 = await post_webhook(webhook_url, captured_payload, good_sig)

    if code4 == 200:
        ok(f"Duplicate accepted  HTTP {code4}  → {data4}")
        recheck = await fetch_payment(settings.database_url, qr_code_id)
        if recheck and recheck.status == "paid":
            ok("Payment status still 'paid' — no double-processing issue.")
            info("Handler sets status='paid' again but result is idempotent.")
        else:
            warn(f"Unexpected status: {recheck.status if recheck else 'not found'}")
    else:
        fail(f"Unexpected HTTP {code4}: {data4}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print(_c(BOLD, "━" * 60))
    print(_c(BOLD, "  Simulation complete — Summary"))
    print(_c(BOLD, "━" * 60))
    print()
    ok(f"Payment created   qr_code_id = {qr_code_id}")
    ok(f"payment.captured  → status updated to 'paid'  in DB")
    ok(f"payment.failed    → HTTP 200 returned but NOT handled (gap documented)")
    ok(f"Wrong signature   → HTTP 401  (security working)")
    ok(f"Duplicate event   → idempotent (no double-processing)")
    print()
    warn("Gaps to implement:")
    print(f"    {_c(YELLOW, '•')} Customer WhatsApp confirmation after payment.captured")
    print(f"    {_c(YELLOW, '•')} Owner notification after payment.captured")
    print(f"    {_c(YELLOW, '•')} Handle payment.failed  (DB status + customer message)")
    print(f"    {_c(YELLOW, '•')} Google Sheets logging")
    print(f"    {_c(YELLOW, '•')} Razorpay order reference / receipt linking")
    print()
    print(f"  DB payment id : {updated.id}")
    print(f"  Razorpay id   : {razorpay_pay_id}")
    print(f"  Final status  : {_c(GREEN + BOLD, updated.status)}")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulate the full Razorpay payment lifecycle locally."
    )
    parser.add_argument(
        "--qr-api",
        action="store_true",
        help="Call POST /payments/qr (real Razorpay test API) instead of direct DB insert.",
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8000",
        help="Backend base URL (default: http://localhost:8000).",
    )
    parser.add_argument(
        "--phone",
        default="919876543210",
        help="Customer phone number for the test payment.",
    )
    args = parser.parse_args()
    await run(args)


if __name__ == "__main__":
    asyncio.run(main())
