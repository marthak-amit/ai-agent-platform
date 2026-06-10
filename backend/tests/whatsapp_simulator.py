#!/usr/bin/env python3
"""
WhatsApp Simulator — test your AI agent locally without a real phone number.

Usage (from backend/ with venv active):
    python tests/whatsapp_simulator.py            # interactive chat
    python tests/whatsapp_simulator.py --demo     # run 5 canned scenarios
    python tests/whatsapp_simulator.py --fresh    # start a new conversation
    python tests/whatsapp_simulator.py --url http://localhost:8001  # custom port

Prerequisites:
    • Backend running:  uvicorn app.main:app --reload --port 8000
    • .env file present in backend/ with META_APP_SECRET set
    • (Optional) seed data loaded: python seed.py

How it works:
    1. Wraps your text in an exact Meta Cloud API webhook JSON payload
    2. Computes the HMAC-SHA256 X-Hub-Signature-256 header
    3. POSTs to POST /webhook (the same endpoint Meta calls)
    4. Queries the database directly for the AI's reply (the webhook saves it
       to DB before returning — no real WhatsApp API call is made to intercept)
    5. Prints the reply and loops
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

# Allow  `from app.*` imports when run from inside tests/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models.conversation import Conversation
from app.models.message import Message

# ── ANSI colour helpers ───────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BLUE   = "\033[94m"
MAGENTA = "\033[95m"


def _c(colour: str, text: str) -> str:
    return f"{colour}{text}{RESET}"


# ── Demo scenarios ────────────────────────────────────────────────────────────

DEMO_SCENARIOS: list[tuple[str, str]] = [
    (
        "Basic greeting",
        "hello",
    ),
    (
        "Product price enquiry",
        "Banarasi silk saree ka price kya hai?",
    ),
    (
        "Stock check",
        "Cotton saree kitni stock mein hai? Available hai kya?",
    ),
    (
        "Purchase intent",
        "order karna hai. Kanjivaram silk chahiye.",
    ),
    (
        "Hindi — budget query",
        "Mujhe ek saree gift karni hai apni maa ko. Budget ₹2000 hai. Kya option hai?",
    ),
    (
        "SKU lookup — product code query",
        "SR27754 wali saree ka price kya hai? Available hai kya?",
    ),
]

# ── Payload builder ───────────────────────────────────────────────────────────

def build_payload(user_text: str, from_phone: str, to_phone_id: str) -> str:
    """Build the exact JSON body Meta sends to your webhook."""
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "SIMULATOR_ENTRY_" + uuid.uuid4().hex[:8],
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
                                        "profile": {"name": "Simulator Customer"},
                                        "wa_id": from_phone,
                                    }
                                ],
                                "messages": [
                                    {
                                        "id": "sim_" + uuid.uuid4().hex,
                                        "from": from_phone,
                                        "timestamp": str(int(time.time())),
                                        "type": "text",
                                        "text": {"body": user_text},
                                    }
                                ],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )


def build_image_payload(media_id: str, from_phone: str, to_phone_id: str) -> str:
    """Build a Meta-style webhook JSON body for an image message."""
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "SIMULATOR_ENTRY_" + uuid.uuid4().hex[:8],
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
                                        "profile": {"name": "Simulator Customer"},
                                        "wa_id": from_phone,
                                    }
                                ],
                                "messages": [
                                    {
                                        "id": "sim_" + uuid.uuid4().hex,
                                        "from": from_phone,
                                        "timestamp": str(int(time.time())),
                                        "type": "image",
                                        "image": {
                                            "id": media_id,
                                            "mime_type": "image/jpeg",
                                        },
                                    }
                                ],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )


def sign_payload(body: bytes, secret: str) -> str:
    """Compute X-Hub-Signature-256 exactly as Meta does."""
    digest = hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


# ── DB query — get last AI reply for a phone number ───────────────────────────

async def get_last_reply(
    db_url: str,
    phone: str,
    after_ts: datetime,
) -> Optional[str]:
    """
    Return the latest model-role message for *phone* created after *after_ts*.

    The webhook saves the AI reply synchronously before returning 200, so by
    the time httpx.post() returns the reply is already in the database.
    """
    engine = create_async_engine(db_url, echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    reply = None
    try:
        async with Session() as session:
            result = await session.execute(
                select(Message)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(
                    Conversation.phone_number == phone,
                    Message.role == "assistant",
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


# ── Core send-and-receive ─────────────────────────────────────────────────────

async def send_image(
    image_path: str,
    phone: str,
    webhook_url: str,
    settings,
) -> Optional[str]:
    """
    Simulate an image message by uploading a local file to the webhook.

    Because the simulator cannot create a real Meta media_id, it embeds the
    image as a base64 data URI in a fake media_id string. The vision_service
    download step is bypassed in tests by patching; this function still sends
    the image payload so the webhook routing logic (type == "image") is exercised.

    The fake media_id encodes the base64 image directly so a dev can run a
    modified vision_service stub that decodes it without hitting Meta's CDN.
    """
    import base64

    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
    except OSError as exc:
        print(_c(RED, f"\n  [error] Cannot read image file: {exc}\n"))
        return None

    b64 = base64.b64encode(image_bytes).decode()
    # Encode image inline as the "media_id" so vision_service can be stubbed.
    fake_media_id = f"SIM_BASE64_{b64}"

    payload_str = build_image_payload(
        media_id=fake_media_id,
        from_phone=phone,
        to_phone_id=settings.whatsapp_phone_number_id,
    )
    body_bytes = payload_str.encode("utf-8")
    signature = sign_payload(body_bytes, settings.meta_app_secret)
    sent_at = datetime.now(timezone.utc)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                webhook_url,
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": signature,
                },
            )
    except httpx.ConnectError:
        print(_c(RED, "\n  [error] Cannot connect to the backend.\n"))
        return None
    except httpx.TimeoutException:
        print(_c(RED, "\n  [error] Request timed out.\n"))
        return None

    if response.status_code != 200:
        print(_c(RED, f"\n  [error] Webhook returned HTTP {response.status_code}: {response.text}\n"))
        return None

    reply = await get_last_reply(settings.database_url, phone, sent_at)
    if reply is None:
        print(_c(YELLOW, "  [warn] No AI reply saved — vision API may have errored (check server logs).\n"))
    return reply


async def send_message(
    user_text: str,
    phone: str,
    webhook_url: str,
    settings,
) -> Optional[str]:
    """
    POST one message to the webhook and return the AI's reply text, or None.
    """
    payload_str = build_payload(
        user_text=user_text,
        from_phone=phone,
        to_phone_id=settings.whatsapp_phone_number_id,
    )
    body_bytes = payload_str.encode("utf-8")
    signature  = sign_payload(body_bytes, settings.meta_app_secret)

    sent_at = datetime.now(timezone.utc)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                webhook_url,
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": signature,
                },
            )
    except httpx.ConnectError:
        print(
            _c(RED, "\n  [error] Cannot connect to the backend.\n")
            + _c(YELLOW, "  Make sure the server is running:\n")
            + _c(DIM,    "  uvicorn app.main:app --reload --port 8000\n")
        )
        return None
    except httpx.TimeoutException:
        print(_c(RED, "\n  [error] Request timed out — Gemini may be slow.\n"))
        return None

    if response.status_code == 401:
        print(
            _c(RED, "\n  [error] Signature rejected (HTTP 401).\n")
            + _c(YELLOW, "  Check META_APP_SECRET in backend/.env matches the running server.\n")
        )
        return None

    if response.status_code != 200:
        print(_c(RED, f"\n  [error] Webhook returned HTTP {response.status_code}: {response.text}\n"))
        return None

    # Webhook returned 200 — the reply is now in the DB.
    reply = await get_last_reply(settings.database_url, phone, sent_at)
    if reply is None:
        # Gemini may have failed — the webhook still returns 200.
        print(_c(YELLOW, "  [warn] No AI reply saved — Gemini API may have errored (check server logs).\n"))
    return reply


# ── Interactive + demo loops ──────────────────────────────────────────────────

def print_header(phone: str, webhook_url: str) -> None:
    print()
    print(_c(BOLD, "━" * 60))
    print(_c(BOLD + CYAN, "  WhatsApp Simulator"))
    print(_c(BOLD, "━" * 60))
    print(f"  Webhook  : {_c(DIM, webhook_url)}")
    print(f"  Your phone: {_c(MAGENTA, phone)}")
    print(f"  Type {_c(BOLD, '/quit')} to exit · {_c(BOLD, '/reset')} to new phone · {_c(BOLD, '/demo')} to run scenarios · {_c(BOLD, '/sales')} for full sales flow · {_c(BOLD, '/lang')} to test language detection · {_c(BOLD, '/image <path>')} to send an image")
    print(_c(BOLD, "━" * 60))
    print()


def print_turn(user_text: str, reply: Optional[str]) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print()
    print(f"  {_c(DIM, ts)}  {_c(BOLD + GREEN, 'You')}  {user_text}")
    if reply:
        # Word-wrap long replies at 70 chars for readability
        wrapped = []
        for line in reply.splitlines():
            while len(line) > 70:
                wrapped.append(line[:70])
                line = line[70:]
            wrapped.append(line)
        label = _c(BOLD + BLUE, "Agent")
        for i, wline in enumerate(wrapped):
            prefix = f"  {_c(DIM, ts)}  {label}  " if i == 0 else " " * (12 + len("Agent") + 2)
            print(f"{prefix}{wline}")


async def run_demo(phone: str, webhook_url: str, settings) -> None:
    print()
    n = len(DEMO_SCENARIOS)
    print(_c(BOLD + YELLOW, f"  Running {n} demo scenarios…"))
    print()
    for i, (label, text) in enumerate(DEMO_SCENARIOS, 1):
        print(_c(DIM, f"  [{i}/{n}] {label}"))
        reply = await send_message(text, phone, webhook_url, settings)
        print_turn(text, reply)
        if i < n:
            await asyncio.sleep(1.5)  # brief pause between scenarios
    print()
    print(_c(GREEN + BOLD, "  Demo complete. Switching to interactive mode…"))


async def get_conversation_stage(db_url: str, phone: str) -> Optional[str]:
    """Query the DB for the current_stage of this phone's conversation."""
    engine = create_async_engine(db_url, echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    stage = None
    try:
        async with Session() as session:
            result = await session.execute(
                select(Conversation)
                .where(Conversation.phone_number == phone)
                .order_by(Conversation.updated_at.desc())
                .limit(1)
            )
            conv = result.scalar_one_or_none()
            if conv and hasattr(conv, "current_stage"):
                stage = conv.current_stage
    finally:
        await engine.dispose()
    return stage


# Full sales flow script — proves end-to-end conversion works
_SALES_FLOW: list[tuple[str, str]] = [
    ("1/7 — Greeting", "hello"),
    ("2/7 — Product inquiry", "banarasi saree dikhao"),
    ("3/7 — Price question", "price kya hai"),
    ("4/7 — Objection: too expensive", "bahut mehnga hai"),
    ("5/7 — Buying intent", "theek hai lena hai"),
    ("6/7 — Order details", "naam: Priya Shah, address: Adajan Surat"),
    ("7/7 — Confirm", "confirm"),
]

from app.services.intent_service import detect_intent
from app.services.language_service import detect_language


async def run_sales_flow(phone: str, webhook_url: str, settings) -> None:
    """
    Run the full 7-step sales conversion flow and print diagnostics at each step.

    Shows detected stage, intent, language, and the AI reply for every turn.
    """
    print()
    print(_c(BOLD + MAGENTA, "━" * 60))
    print(_c(BOLD + MAGENTA, "  SALES CONVERSION FLOW TEST"))
    print(_c(BOLD + MAGENTA, "━" * 60))
    print(_c(DIM, f"  Phone : {phone}"))
    print(_c(DIM, f"  Steps : {len(_SALES_FLOW)}"))
    print()

    for step_label, message in _SALES_FLOW:
        print(_c(BOLD + CYAN, f"  ── Step {step_label}"))
        print(_c(DIM, f"  Message: {message}"))

        # Client-side intent + language detection (same logic as server)
        intent = detect_intent(message)
        lang = detect_language(message)
        print(
            f"  {_c(YELLOW, 'Intent:')} order={intent['is_order_intent']} "
            f"reject={intent['is_rejection']} off_topic={intent['is_off_topic']} "
            f"score={intent['order_score']} confidence={intent['confidence']}"
        )
        print(f"  {_c(YELLOW, 'Language:')} {lang}")

        reply = await send_message(message, phone, webhook_url, settings)
        print_turn(message, reply)

        # Fetch stage from DB after reply is saved
        stage = await get_conversation_stage(settings.database_url, phone)
        if stage:
            print(f"  {_c(MAGENTA, 'Stage detected:')} {stage}")

        # Lead status would require DB query — omitted for brevity
        print()
        await asyncio.sleep(1.0)

    print(_c(BOLD + GREEN, "  Sales flow complete!"))
    print(_c(DIM,    "  Review the stages above to verify full conversion flow works.\n"))


_LANG_TEST_CASES: list[tuple[str, str, str]] = [
    (
        "English — product query",
        "banarasi saree price",
        "english",
    ),
    (
        "Hinglish — Hindi roman",
        "banarasi saree ka price kya hai",
        "hindi_roman",
    ),
    (
        "Hindi Devanagari",
        "बनारसी साड़ी का दाम क्या है",
        "hindi_devanagari",
    ),
    (
        "Gujarati roman — kem cho",
        "kem cho, banarasi saree ni kimat ketlu che",
        "gujarati_roman",
    ),
]


async def run_lang_test(phone: str, webhook_url: str, settings) -> None:
    """
    Send four test messages (one per language) and verify the agent replies
    in the correct language.

    Correct language is judged locally by detect_language() applied to the
    AI reply — a rough heuristic good enough for smoke-testing.
    """
    print()
    print(_c(BOLD + YELLOW, "━" * 60))
    print(_c(BOLD + YELLOW, "  LANGUAGE DETECTION TEST"))
    print(_c(BOLD + YELLOW, "━" * 60))
    print()

    passed = 0
    failed = 0

    for label, message, expected_lang in _LANG_TEST_CASES:
        detected = detect_language(message)
        detection_ok = detected == expected_lang

        print(_c(BOLD + CYAN, f"  ── {label}"))
        print(_c(DIM, f'  Message  : "{message}"'))
        print(
            f"  Expected : {_c(GREEN, expected_lang)}  "
            f"Detected : {_c(GREEN if detection_ok else RED, detected)}"
            f"  {'✅' if detection_ok else '❌'}"
        )

        reply = await send_message(message, phone, webhook_url, settings)
        if reply:
            reply_lang = detect_language(reply)
            reply_ok = reply_lang == expected_lang
            print_turn(message, reply)
            print(
                f"  Reply lang: {_c(GREEN if reply_ok else RED, reply_lang)}"
                f"  {'✅ Correct' if reply_ok else '❌ Wrong language'}"
            )
            if detection_ok and reply_ok:
                passed += 1
            else:
                failed += 1
        else:
            print(_c(RED, "  No reply received — check server logs."))
            failed += 1

        print()
        await asyncio.sleep(1.0)

    total = passed + failed
    status = _c(GREEN + BOLD, f"{passed}/{total} passed") if failed == 0 else _c(RED + BOLD, f"{passed}/{total} passed, {failed} failed")
    print(_c(BOLD, "  Result: ") + status)
    print()


async def run_interactive(phone: str, webhook_url: str, settings) -> None:
    loop = asyncio.get_event_loop()

    while True:
        try:
            # run_in_executor so the async event loop isn't blocked by input()
            user_text = await loop.run_in_executor(
                None, lambda: input(_c(BOLD + GREEN, "\n  You: ")).strip()
            )
        except (EOFError, KeyboardInterrupt):
            print(_c(DIM, "\n  Bye!\n"))
            break

        if not user_text:
            continue

        cmd = user_text.lower()
        if cmd in ("/quit", "/exit", "/q"):
            print(_c(DIM, "\n  Bye!\n"))
            break
        if cmd == "/reset":
            phone = "91" + "".join([str(random.randint(0, 9)) for _ in range(10)])
            print(_c(YELLOW, f"\n  New session — phone: {phone}"))
            continue
        if cmd == "/demo":
            await run_demo(phone, webhook_url, settings)
            continue
        if cmd == "/sales":
            await run_sales_flow(phone, webhook_url, settings)
            continue
        if cmd == "/lang":
            await run_lang_test(phone, webhook_url, settings)
            continue
        if cmd == "/help":
            print(_c(DIM, "  Commands: /quit  /reset  /demo  /sales  /lang  /image <path>  /help"))
            continue
        if cmd.startswith("/image"):
            parts = user_text.split(maxsplit=1)
            if len(parts) < 2:
                print(_c(YELLOW, "  Usage: /image <path-to-image-file>"))
                continue
            image_path = parts[1].strip()
            print(_c(DIM, f"  Sending image: {image_path}"))
            reply = await send_image(image_path, phone, webhook_url, settings)
            print_turn(f"[image: {image_path}]", reply)
            continue

        reply = await send_message(user_text, phone, webhook_url, settings)
        print_turn(user_text, reply)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulate WhatsApp conversations with your local AI agent."
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run 5 canned test scenarios then switch to interactive mode.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Start with a new random phone number (new conversation).",
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8000",
        help="Base URL of the backend (default: http://localhost:8000).",
    )
    parser.add_argument(
        "--phone",
        default=None,
        help="Customer phone number to use (E.164 without +, e.g. 919900000001).",
    )
    args = parser.parse_args()

    try:
        settings = get_settings()
    except Exception as exc:
        print(_c(RED, f"\n  [error] Could not load settings: {exc}"))
        print(_c(YELLOW, "  Make sure backend/.env exists and is valid.\n"))
        sys.exit(1)

    webhook_url = args.url.rstrip("/") + "/webhook"

    if args.phone:
        phone = args.phone
    elif args.fresh:
        phone = "91" + "".join([str(random.randint(0, 9)) for _ in range(10)])
    else:
        phone = "919900000001"  # stable simulator number — re-uses conversation history

    print_header(phone, webhook_url)

    if args.demo:
        await run_demo(phone, webhook_url, settings)

    await run_interactive(phone, webhook_url, settings)


if __name__ == "__main__":
    asyncio.run(main())
