#!/usr/bin/env python3
"""
Agent quality tester — sends known inputs through the live webhook and scores replies.

Usage (from backend/ with venv active, server running on port 8000):
    python scripts/test_agent_quality.py
    python scripts/test_agent_quality.py --url http://localhost:8001
    python scripts/test_agent_quality.py --phone +919876543210  # custom test phone

The script creates a fresh conversation per test, posts the message through the
real /webhook endpoint (same path Meta uses), then reads the AI reply from DB.

Exit code:
    0  — score >= 80 %
    1  — score <  80 % (production not recommended)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import uuid
from typing import Optional

import httpx

# Allow `from app.*` imports when run from inside scripts/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ── Quality test definitions ──────────────────────────────────────────────────

QUALITY_TESTS: list[dict] = [
    {
        "name": "Language purity — English reply contains no Hindi filler",
        "input": "show me sarees",
        "must_contain": ["₹"],
        "must_not_contain": ["ji", " haan", " nahi", " hai ", " mein ", " chahiye"],
        "note": "English query must produce a clean English reply with a price",
    },
    {
        "name": "No invented product attributes (no random colours)",
        "input": "SR27754",
        "must_contain": ["2,450", "SR27754"],
        "must_not_contain": [" red", " blue", " green", " navy", " pink"],
        "note": "SKU lookup must not hallucinate colour variants not in catalogue",
    },
    {
        "name": "Quantity asked before name — order intent",
        "input": "I want to order",
        "must_contain_any": ["pieces", "how many", "quantity", "kitne"],
        "must_not_contain": ["your name", "aapka naam", "address"],
        "note": "Agent must ask quantity first, never name or address",
    },
    {
        "name": "Off-topic blocked — cricket",
        "input": "what is cricket?",
        "must_contain_any": ["only help", "products", "sirf", "madad"],
        "must_not_contain": ["cricket is", "sport", "india won", "ipl"],
        "note": "Off-topic questions must be redirected to products",
    },
    {
        "name": "Off-topic blocked — general AI question",
        "input": "what is artificial intelligence?",
        "must_not_contain": ["artificial intelligence is", "machine learning", "deep learning"],
        "note": "General knowledge questions must be deflected",
    },
    {
        "name": "Price inquiry returns ₹ symbol",
        "input": "banarasi saree price",
        "must_contain": ["₹"],
        "must_not_contain": ["[price]", "[amount]", "contact us for price"],
        "note": "Agent must always state the real price with ₹ symbol",
    },
    {
        "name": "No competitor mention",
        "input": "compare with Myntra",
        "must_not_contain": ["myntra is", "myntra has", "cheaper on myntra"],
        "note": "Never compare or validate competitor platforms",
    },
    {
        "name": "UPI ID never a placeholder",
        "input": "how do I pay?",
        "must_not_contain": ["[upi id]", "[amount]", "team will share", "contact us for upi"],
        "note": "Payment instructions must include real UPI ID and amount",
    },
]

# ── Webhook helpers ───────────────────────────────────────────────────────────

def _build_whatsapp_payload(phone: str, message: str, wa_id: str) -> dict:
    """Wrap message text in the exact Meta Cloud API webhook JSON shape."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "ENTRY_ID",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550000001",
                                "phone_number_id": "PHONE_ID",
                            },
                            "contacts": [{"profile": {"name": "QA Tester"}, "wa_id": wa_id}],
                            "messages": [
                                {
                                    "from": phone,
                                    "id": f"wamid.{uuid.uuid4().hex}",
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": message},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def _sign_payload(payload_bytes: bytes, secret: str) -> str:
    """Compute the X-Hub-Signature-256 header value."""
    mac = hmac.new(secret.encode(), payload_bytes, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


async def send_test_message(
    message: str,
    base_url: str,
    phone: str,
    meta_secret: str,
) -> Optional[str]:
    """
    POST a test message to /webhook and return the AI reply text fetched from DB.

    Returns None if the request failed or no reply was found within the timeout.
    """
    wa_id = phone.lstrip("+").replace(" ", "")
    payload = _build_whatsapp_payload(phone=phone, message=message, wa_id=wa_id)
    payload_bytes = json.dumps(payload).encode()
    signature = _sign_payload(payload_bytes, meta_secret)

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(
                f"{base_url}/webhook",
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": signature,
                },
            )
            if resp.status_code not in (200, 202):
                logger.warning("Webhook returned %s: %s", resp.status_code, resp.text[:200])
                return None
        except httpx.RequestError as exc:
            logger.error("Request failed: %s", exc)
            return None

    # Give the async handler time to write the reply to DB
    await asyncio.sleep(3)

    # Fetch the reply from DB directly
    from sqlalchemy import select, text
    from app.db import _get_session_factory
    from app.models.conversation import Conversation
    from app.models.message import Message

    factory = _get_session_factory()
    async with factory() as db:
        result = await db.execute(
            select(Conversation).where(
                Conversation.whatsapp_number == phone
            ).order_by(Conversation.id.desc())
        )
        conv = result.scalars().first()
        if not conv:
            return None

        msg_result = await db.execute(
            select(Message)
            .where(
                Message.conversation_id == conv.id,
                Message.role.in_(["assistant", "model"]),
            )
            .order_by(Message.id.desc())
            .limit(1)
        )
        last_reply = msg_result.scalars().first()
        return last_reply.content if last_reply else None


# ── Scoring logic ─────────────────────────────────────────────────────────────

def _check_test(test: dict, reply: str) -> tuple[bool, list[str]]:
    """
    Evaluate a single test against the agent reply.

    Returns (passed: bool, failure_reasons: list[str]).
    """
    failures: list[str] = []
    reply_lower = reply.lower()

    for word in test.get("must_contain", []):
        if word.lower() not in reply_lower:
            failures.append(f"missing required term '{word}'")

    any_terms = test.get("must_contain_any", [])
    if any_terms and not any(t.lower() in reply_lower for t in any_terms):
        failures.append(f"none of {any_terms} found in reply")

    for word in test.get("must_not_contain", []):
        if word.lower() in reply_lower:
            failures.append(f"forbidden term '{word}' present")

    return (len(failures) == 0, failures)


async def run_quality_tests(base_url: str, phone: str, meta_secret: str) -> float:
    """
    Run all QUALITY_TESTS and print a score report.

    Returns the percentage score (0–100).
    """
    passed = 0
    failed = 0
    total = len(QUALITY_TESTS)

    print(f"\n{'═' * 60}")
    print(f"  Agent Quality Test  —  {total} checks")
    print(f"  Server: {base_url}")
    print(f"{'═' * 60}\n")

    for i, test in enumerate(QUALITY_TESTS, 1):
        print(f"[{i}/{total}] {test['name']}")
        print(f"  Input: {test['input']!r}")

        reply = await send_test_message(
            message=test["input"],
            base_url=base_url,
            phone=phone,
            meta_secret=meta_secret,
        )

        if reply is None:
            print("  ⚠️  No reply received — skipping (is the server running?)\n")
            failed += 1
            continue

        print(f"  Reply: {reply[:120]!r}{'…' if len(reply) > 120 else ''}")

        ok, reasons = _check_test(test, reply)
        if ok:
            passed += 1
            print("  ✅ PASS\n")
        else:
            failed += 1
            for reason in reasons:
                print(f"  ❌ FAIL — {reason}")
            if test.get("note"):
                print(f"  📌 {test['note']}")
            print()

        # Small gap between tests to avoid rate-limiting
        await asyncio.sleep(1)

    score = (passed / total) * 100
    bar = "█" * passed + "░" * failed
    status = "✅ PRODUCTION READY" if score >= 80 else "⚠️  NOT READY — fix failures first"
    print(f"{'═' * 60}")
    print(f"  Score: {score:.0f}%  [{bar}]  ({passed}/{total} passed)")
    print(f"  {status}")
    print(f"{'═' * 60}\n")

    if score < 80:
        logger.warning(
            "QUALITY_ALERT: agent score %.0f%% is below 80%% threshold (%d/%d passed). "
            "Review failed tests above before deploying.",
            score, passed, total,
        )

    return score


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    """Parse CLI args and run the quality suite."""
    parser = argparse.ArgumentParser(description="Agent quality tester")
    parser.add_argument(
        "--url", default="http://localhost:8000", help="Backend base URL"
    )
    parser.add_argument(
        "--phone",
        default="+919000000001",
        help="Test phone number (must match a seeded client)",
    )
    args = parser.parse_args()

    from app.config import get_settings
    settings = get_settings()
    meta_secret = settings.meta_app_secret

    if not meta_secret:
        print("ERROR: META_APP_SECRET not set in .env — cannot sign webhook payloads.")
        sys.exit(1)

    score = asyncio.run(
        run_quality_tests(
            base_url=args.url,
            phone=args.phone,
            meta_secret=meta_secret,
        )
    )
    sys.exit(0 if score >= 80 else 1)


if __name__ == "__main__":
    main()
