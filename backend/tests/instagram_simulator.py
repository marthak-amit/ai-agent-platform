#!/usr/bin/env python3
"""
Instagram Simulator — test your AI agent locally without a real Instagram account.

Usage (from backend/ with venv active):
    python tests/instagram_simulator.py               # interactive mode
    python tests/instagram_simulator.py --demo        # 4 canned scenarios
    python tests/instagram_simulator.py --fresh       # new IGSID (fresh conversation)
    python tests/instagram_simulator.py --verbose     # always print full payload
    python tests/instagram_simulator.py --url http://localhost:8001

Message types (interactive):
    Just type text          → DM to your bot
    comment: POST_ID text   → Comment on a specific post
    story: text             → Story reply
    /image                  → Send an image DM (prompts for local file path)
    /demo                   → Run demo scenarios
    /reset                  → New IGSID (fresh conversation)
    /verbose  /quiet        → Toggle payload printing
    /quit                   → Exit

Prerequisites:
    • Backend running:  uvicorn app.main:app --reload --port 8000
    • Client on growth or pro plan (starter blocks Instagram)
    • (Optional) seed data:  python seed.py

How it works:
    1. Wraps your text in the exact Meta Graph API webhook JSON
    2. POSTs to POST /instagram (no signature needed — IG router has no HMAC check)
    3. Queries the DB directly for the AI reply (saved synchronously by the handler)
    4. Shows the reply, the payload sent, and a flow explanation for each event type
"""

from __future__ import annotations

import argparse
import asyncio
import base64
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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models.conversation import Conversation
from app.models.message import Message

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
WHITE  = "\033[97m"

def _c(col: str, t: str) -> str:
    return f"{col}{t}{R}"


# ── Fake IDs used across all events ──────────────────────────────────────────

SIM_IG_BUSINESS_ID = "17841400000000001"   # your Instagram Business Account ID
SIM_USERNAME       = "sim_customer"        # display name in comment payloads

# ── Demo scenarios ────────────────────────────────────────────────────────────

class EventType:
    DM      = "dm"
    COMMENT = "comment"
    STORY   = "story"
    IMAGE   = "image"


DEMO_SCENARIOS: list[tuple[str, str, str, str]] = [
    # (label, event_type, post_id_or_empty, text)
    (
        "First-time user DM",
        EventType.DM, "",
        "Hi! I came across your profile. Your sarees are beautiful! 😍",
    ),
    (
        "Comment on post — product price enquiry",
        EventType.COMMENT, "post_banarasi_launch_2024",
        "Yeh Banarasi silk saree kitne ki hai? 😍 COD available hai?",
    ),
    (
        "Story reply",
        EventType.STORY, "",
        "OMG this lehenga is stunning! Can I get it in red?",
    ),
    (
        "Returning user DM in Hindi",
        EventType.DM, "",
        "Maine pehle Georgette order ki thi. Kya aur colors available hain?",
    ),
]

# ── Payload builders ──────────────────────────────────────────────────────────

def build_dm_payload(sender_igsid: str, text: str) -> dict:
    """Exact JSON Meta sends for an Instagram DM."""
    return {
        "object": "instagram",
        "entry": [
            {
                "id": SIM_IG_BUSINESS_ID,
                "messaging": [
                    {
                        "sender":    {"id": sender_igsid},
                        "recipient": {"id": SIM_IG_BUSINESS_ID},
                        "timestamp": int(time.time()),
                        "message": {
                            "mid":  "sim_mid_" + uuid.uuid4().hex[:12],
                            "text": text,
                        },
                    }
                ],
            }
        ],
    }


def build_story_payload(sender_igsid: str, text: str) -> dict:
    """
    Instagram story replies arrive as a messaging event with an optional
    story attachment.  The current handler reads only message.text, so the
    story attachment object is included but the text drives the AI response.
    """
    return {
        "object": "instagram",
        "entry": [
            {
                "id": SIM_IG_BUSINESS_ID,
                "messaging": [
                    {
                        "sender":    {"id": sender_igsid},
                        "recipient": {"id": SIM_IG_BUSINESS_ID},
                        "timestamp": int(time.time()),
                        "message": {
                            "mid":  "sim_story_" + uuid.uuid4().hex[:12],
                            "text": text,
                            # Real Meta payload would also include:
                            # "attachments": [{"type": "story_mention",
                            #                  "payload": {"url": "...", "id": "..."}}]
                        },
                    }
                ],
            }
        ],
    }


def build_image_dm_payload(sender_igsid: str, image_b64: str) -> dict:
    """
    Simulate an Instagram image DM.

    Instagram delivers image DMs with type='image' and an attachments array
    that holds a payload.url pointing to the CDN.  In the simulator we embed
    a data-URI so the backend can fetch it without a real CDN token.
    """
    # Wrap the base64 data as a data-URI so the backend's httpx.get() call works.
    data_url = f"data:image/jpeg;base64,{image_b64}"
    return {
        "object": "instagram",
        "entry": [
            {
                "id": SIM_IG_BUSINESS_ID,
                "messaging": [
                    {
                        "sender":    {"id": sender_igsid},
                        "recipient": {"id": SIM_IG_BUSINESS_ID},
                        "timestamp": int(time.time()),
                        "message": {
                            "mid":  "sim_img_" + uuid.uuid4().hex[:12],
                            "type": "image",
                            "attachments": [
                                {
                                    "type": "image",
                                    "payload": {"url": data_url},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def build_comment_payload(sender_igsid: str, post_id: str, text: str) -> dict:
    """Exact JSON Meta sends when someone comments on one of your posts."""
    return {
        "object": "instagram",
        "entry": [
            {
                "id": SIM_IG_BUSINESS_ID,
                "changes": [
                    {
                        "field": "comments",
                        "value": {
                            "from": {
                                "id":       sender_igsid,
                                "username": SIM_USERNAME,
                            },
                            "media": {
                                "id": post_id or "sim_post_" + uuid.uuid4().hex[:8],
                            },
                            "id":   "sim_comment_" + uuid.uuid4().hex[:10],
                            "text": text,
                        },
                    }
                ],
            }
        ],
    }


def fmt_payload(payload: dict) -> str:
    """Pretty-print a payload dict with syntax-like colouring."""
    raw = json.dumps(payload, indent=2, ensure_ascii=False)
    lines = []
    for line in raw.splitlines():
        stripped = line.lstrip()
        if stripped.startswith('"') and '": ' in stripped:
            key, _, rest = stripped.partition('": ')
            indent = line[: len(line) - len(stripped)]
            lines.append(f"{indent}{_c(CYAN, key + chr(34))}: {_c(WHITE, rest)}")
        elif stripped in ("{", "}", "[", "]", "{,", "},", "],"):
            lines.append(_c(DIM, line))
        else:
            lines.append(_c(DIM, line))
    return "\n".join(lines)


# ── DB helpers ────────────────────────────────────────────────────────────────

async def get_last_reply(
    db_url: str,
    igsid: str,
    after_ts: datetime,
) -> Optional[str]:
    """Return the newest model reply for *igsid* created after *after_ts*."""
    engine = create_async_engine(db_url, echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    reply = None
    try:
        async with Session() as session:
            result = await session.execute(
                select(Message)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(
                    Conversation.phone_number == igsid,
                    Conversation.channel == "instagram",
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


async def count_prior_messages(db_url: str, igsid: str) -> int:
    """Return total messages (any role) for *igsid* on Instagram channel."""
    engine = create_async_engine(db_url, echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    count = 0
    try:
        async with Session() as session:
            result = await session.execute(
                select(func.count(Message.id))
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(
                    Conversation.phone_number == igsid,
                    Conversation.channel == "instagram",
                )
            )
            count = result.scalar() or 0
    finally:
        await engine.dispose()
    return count


# ── Core send + explain ───────────────────────────────────────────────────────

async def send_event(
    payload: dict,
    webhook_url: str,
) -> tuple[int, dict]:
    """POST payload to the Instagram webhook. Returns (status_code, body)."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                webhook_url,
                content=body,
                headers={"Content-Type": "application/json"},
            )
        return resp.status_code, resp.json()
    except httpx.ConnectError:
        return -1, {}
    except httpx.TimeoutException:
        return -2, {}


def print_separator(label: str = "") -> None:
    width = 60
    if label:
        pad = (width - len(label) - 2) // 2
        print(_c(DIM, "─" * pad) + f" {_c(BOLD, label)} " + _c(DIM, "─" * pad))
    else:
        print(_c(DIM, "─" * width))


def print_flow_explanation(
    event_type: str,
    is_first_time: bool,
    plan_restricted: bool,
    post_id: str = "",
) -> None:
    """Print a clear explanation of what would happen in production."""
    print_separator("Flow")

    if plan_restricted:
        print(f"  {_c(RED, '✗ Plan restricted')}  Instagram requires Growth or Pro plan.")
        print(f"    Starter plan clients are blocked at the router level.")
        print(f"    Upgrade via POST /plans/upgrade  {{\"plan_slug\": \"growth\"}}")
        return

    if event_type == EventType.IMAGE:
        print(f"  {_c(GREEN, '✓ Image DM received')}  Handler: _handle_image_dm()")
        print(f"  {_c(BLUE, '→ vision_service.download_instagram_media(url)')}")
        print(f"     Unlike WhatsApp (media_id → two API calls), Instagram")
        print(f"     embeds the CDN URL directly in the payload.")
        print(f"  {_c(BLUE, '→ vision_service.analyze_product_image(bytes, catalogue)')}")
        print(f"     Model: meta-llama/llama-4-scout-17b-16e-instruct (Groq)")
        print(f"     Cost:  ~₹0.07 per image")
        print(f"  {_c(BLUE, '→ DB')}      saves [image] + model reply")
        print(f"  {_c(BLUE, '→ instagram_service.send_dm()')}  vision reply sent to customer")
        return

    if event_type == EventType.DM:
        print(f"  {_c(GREEN, '✓ DM received')}  Handler: _handle_dm()")
        print(f"  {_c(BLUE, '→ Gemini')}  generates reply using conversation history")
        print(f"  {_c(BLUE, '→ DB')}      saves user + model messages")
        print(f"  {_c(BLUE, '→ Lead')}    tagged hot / warm / cold by lead_service")
        if is_first_time:
            print(f"  {_c(BLUE, '→ instagram_service.send_dm()')}")
            print()
            print(f"  {_c(YELLOW, '⚠  First-time user')}  — 24-hour messaging window")
            print(f"     Meta only allows DMs to users who messaged YOU first.")
            print(f"     Since this IGSID has no prior history, the DM might be")
            print(f"     blocked by Meta in production.  In dev, the send call")
            print(f"     fails silently (no real token) so the test still passes.")
        else:
            print(f"  {_c(BLUE, '→ instagram_service.send_dm()')}")
            print()
            print(f"  {_c(GREEN, '✓ Returning user')}  — messaging window already open.")
            print(f"     Prior DMs mean Meta will accept the outbound message.")

    elif event_type == EventType.STORY:
        print(f"  {_c(GREEN, '✓ Story reply received')}  Routed as DM (same handler: _handle_dm())")
        print(f"     Meta sends story replies through the messaging[] array,")
        print(f"     identical to DMs.  The story attachment is ignored by the")
        print(f"     current handler — only message.text is processed.")
        print(f"  {_c(BLUE, '→ Gemini')}  generates reply")
        print(f"  {_c(BLUE, '→ instagram_service.send_dm()')}  reply sent as DM")
        if is_first_time:
            print()
            print(f"  {_c(YELLOW, '⚠  24-hr window')}  Replying to a story mention technically")
            print(f"     opens a messaging window in Meta's policy, so the outbound")
            print(f"     DM reply is usually allowed even for first-time users.")

    elif event_type == EventType.COMMENT:
        print(f"  {_c(GREEN, '✓ Comment received')}  Handler: _handle_comment()")
        print(f"     Post ID: {_c(CYAN, post_id or 'auto-generated')}")
        print()
        print(f"  {_c(BLUE, '→ Public reply')}  {_c(YELLOW, 'NOT sent')} by current implementation.")
        print(f"     instagram_service.reply_to_comment() exists but is not")
        print(f"     called from _handle_comment().  Only a DM is sent.")
        print(f"     To add public replies: call reply_to_comment(comment_id)")
        print(f"     before or after send_dm() in instagram.py:_handle_comment.")
        print()
        print(f"  {_c(BLUE, '→ DM to commenter')}  instagram_service.send_dm()")
        if is_first_time:
            print()
            print(f"  {_c(YELLOW, '⚠  First-time commenter')}  — DM may be blocked.")
            print(f"     Commenter has never sent you a DM, so the 24-hour window")
            print(f"     is not open.  Meta may reject the outbound DM.")
            print(f"     Workaround: use a public comment reply instead of a DM")
            print(f"     (call reply_to_comment() which is always allowed).")
        else:
            print()
            print(f"  {_c(GREEN, '✓ Returning commenter')}  — prior DM history found.")
            print(f"     Messaging window is open; DM will be accepted by Meta.")


# ── Per-event orchestrator ────────────────────────────────────────────────────

async def run_event(
    event_type: str,
    text: str,
    igsid: str,
    post_id: str,
    webhook_url: str,
    settings,
    show_payload: bool,
) -> Optional[str]:
    """
    Build the right payload, POST it, retrieve the reply, and print the
    full explanation.  Returns the AI reply text, or None on failure.
    """
    # Build payload
    if event_type == EventType.IMAGE:
        payload = build_image_dm_payload(igsid, text)   # text carries base64 here
        event_label = "Image DM"
    elif event_type == EventType.DM:
        payload = build_dm_payload(igsid, text)
        event_label = "DM"
    elif event_type == EventType.STORY:
        payload = build_story_payload(igsid, text)
        event_label = "Story reply"
    else:
        payload = build_comment_payload(igsid, post_id, text)
        event_label = f"Comment on {_c(CYAN, post_id or 'post')}"

    # Check prior history BEFORE sending (determines first-time status)
    prior_count = await count_prior_messages(settings.database_url, igsid)
    is_first_time = prior_count == 0

    ts = datetime.now().strftime("%H:%M:%S")
    display_text = "[image]" if event_type == EventType.IMAGE else text
    print()
    print_separator(event_label)
    print(f"  {_c(DIM, ts)}  {_c(BOLD + MAGENTA, 'Customer')}  {display_text}")

    # Optionally print the full payload
    if show_payload:
        print()
        print_separator("Payload sent to POST /instagram")
        for line in fmt_payload(payload).splitlines():
            print("  " + line)

    sent_at = datetime.now(timezone.utc)
    status_code, body = await send_event(payload, webhook_url)

    # Error handling
    if status_code == -1:
        print(
            _c(RED, "\n  [error] Cannot connect to backend.\n")
            + _c(YELLOW, "  Start the server:  uvicorn app.main:app --reload --port 8000\n")
        )
        return None
    if status_code == -2:
        print(_c(RED, "\n  [error] Request timed out — Gemini may be slow.\n"))
        return None
    if status_code != 200:
        print(_c(RED, f"\n  [error] Webhook returned HTTP {status_code}: {body}\n"))
        return None

    plan_restricted = body.get("status") == "plan_restricted"

    # Retrieve reply from DB
    reply: Optional[str] = None
    if not plan_restricted:
        reply = await get_last_reply(settings.database_url, igsid, sent_at)

    # Print reply
    if reply:
        label = _c(BOLD + BLUE, "Agent")
        wrapped = []
        for line in reply.splitlines():
            while len(line) > 68:
                wrapped.append(line[:68])
                line = line[68:]
            wrapped.append(line)
        for i, wline in enumerate(wrapped):
            prefix = f"  {_c(DIM, ts)}  {label}  " if i == 0 else " " * (12 + len("Agent") + 2)
            print(f"{prefix}{wline}")
    elif not plan_restricted:
        print(_c(YELLOW, "  [warn] No reply saved — check server logs (Gemini may have errored)."))

    # Flow explanation
    print()
    print_flow_explanation(event_type, is_first_time, plan_restricted, post_id)
    print()

    return reply


# ── Demo runner ───────────────────────────────────────────────────────────────

async def run_demo(igsid: str, webhook_url: str, settings, show_payload: bool) -> None:
    print()
    print(_c(BOLD + YELLOW, "  Running 4 demo scenarios…"))

    for i, (label, event_type, post_id, text) in enumerate(DEMO_SCENARIOS, 1):
        print()
        print(_c(BOLD, f"  ── Scenario {i}/4: {label} ──"))
        await run_event(event_type, text, igsid, post_id, webhook_url, settings, show_payload)
        if i < len(DEMO_SCENARIOS):
            await asyncio.sleep(1.5)

    print(_c(GREEN + BOLD, "  Demo complete. Switching to interactive mode…"))


# ── Command parser ────────────────────────────────────────────────────────────

def parse_input(raw: str) -> tuple[str, str, str]:
    """
    Parse user input into (event_type, post_id, text).

    Formats:
        hello                         → DM
        comment: POST_ID text here    → Comment
        story: text here              → Story reply
    """
    stripped = raw.strip()
    lower = stripped.lower()

    if lower.startswith("comment:"):
        rest = stripped[len("comment:"):].strip()
        parts = rest.split(" ", 1)
        if len(parts) == 2:
            return EventType.COMMENT, parts[0], parts[1]
        # No post_id provided — use empty (simulator will generate one)
        return EventType.COMMENT, "", rest

    if lower.startswith("story:"):
        return EventType.STORY, "", stripped[len("story:"):].strip()

    return EventType.DM, "", stripped


# ── Interactive loop ──────────────────────────────────────────────────────────

def print_header(igsid: str, webhook_url: str) -> None:
    print()
    print(_c(BOLD, "━" * 62))
    print(_c(BOLD + MAGENTA, "  Instagram Simulator"))
    print(_c(BOLD, "━" * 62))
    print(f"  Webhook    : {_c(DIM, webhook_url)}")
    print(f"  Your IGSID : {_c(MAGENTA, igsid)}")
    print()
    print(f"  {_c(BOLD, 'Message types:')}")
    print(f"    {_c(CYAN, 'Just type')}               → DM to your bot")
    print(f"    {_c(CYAN, 'comment: POST_ID text')}   → Comment on a post")
    print(f"    {_c(CYAN, 'story: text')}             → Story reply")
    print()
    print(f"  {_c(BOLD, 'Commands:')}")
    print(f"    {_c(CYAN, '/image')}  {_c(CYAN, '/demo')} {_c(CYAN, '/reset')} {_c(CYAN, '/verbose')} {_c(CYAN, '/quiet')} {_c(CYAN, '/quit')}")
    print(_c(BOLD, "━" * 62))


async def run_interactive(
    igsid: str,
    webhook_url: str,
    settings,
    show_payload: bool,
) -> None:
    loop = asyncio.get_event_loop()

    while True:
        try:
            raw = await loop.run_in_executor(
                None,
                lambda: input(_c(BOLD + MAGENTA, "\n  You: ")).strip(),
            )
        except (EOFError, KeyboardInterrupt):
            print(_c(DIM, "\n  Bye!\n"))
            break

        if not raw:
            continue

        cmd = raw.lower()

        if cmd in ("/quit", "/exit", "/q"):
            print(_c(DIM, "\n  Bye!\n"))
            break

        if cmd == "/reset":
            igsid = str(random.randint(10**15, 10**16 - 1))
            print(_c(YELLOW, f"\n  New IGSID: {igsid}  (fresh conversation)"))
            continue

        if cmd == "/demo":
            await run_demo(igsid, webhook_url, settings, show_payload)
            continue

        if cmd == "/verbose":
            show_payload = True
            print(_c(YELLOW, "  Payload printing ON"))
            continue

        if cmd == "/quiet":
            show_payload = False
            print(_c(YELLOW, "  Payload printing OFF"))
            continue

        if cmd == "/image":
            try:
                path = await loop.run_in_executor(
                    None,
                    lambda: input(_c(BOLD + CYAN, "  Image path: ")).strip(),
                )
            except (EOFError, KeyboardInterrupt):
                continue
            if not path:
                print(_c(DIM, "  (no path entered — skipped)"))
                continue
            if not os.path.isfile(path):
                print(_c(RED, f"  [error] File not found: {path}"))
                continue
            with open(path, "rb") as fh:
                image_b64 = base64.b64encode(fh.read()).decode()
            print(_c(DIM, f"  Loaded {os.path.basename(path)} ({len(image_b64) // 1024} KB base64)"))
            await run_event(
                EventType.IMAGE, image_b64, igsid, "", webhook_url, settings, show_payload
            )
            continue

        if cmd == "/help":
            print(_c(DIM, "  Types:    dm (default)  |  comment: POST_ID text  |  story: text"))
            print(_c(DIM, "  Commands: /image  /demo  /reset  /verbose  /quiet  /quit"))
            continue

        event_type, post_id, text = parse_input(raw)
        if not text:
            print(_c(DIM, "  (empty message — skipped)"))
            continue

        await run_event(
            event_type, text, igsid, post_id, webhook_url, settings, show_payload
        )


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulate Instagram events for your local AI agent."
    )
    parser.add_argument("--demo",    action="store_true", help="Run 4 demo scenarios then interactive.")
    parser.add_argument("--fresh",   action="store_true", help="Start with a new random IGSID.")
    parser.add_argument("--verbose", action="store_true", help="Always print full JSON payload.")
    parser.add_argument("--url",     default="http://localhost:8000", help="Backend base URL.")
    parser.add_argument("--igsid",   default=None, help="Customer IGSID to use.")
    args = parser.parse_args()

    try:
        settings = get_settings()
    except Exception as exc:
        print(_c(RED, f"\n  [error] Cannot load settings: {exc}"))
        print(_c(YELLOW, "  Make sure backend/.env exists.\n"))
        sys.exit(1)

    webhook_url = args.url.rstrip("/") + "/instagram"

    if args.igsid:
        igsid = args.igsid
    elif args.fresh:
        igsid = str(random.randint(10**15, 10**16 - 1))
    else:
        igsid = "1234567890000001"   # stable simulator IGSID — reuses history

    show_payload = args.verbose

    print_header(igsid, webhook_url)

    if args.demo:
        await run_demo(igsid, webhook_url, settings, show_payload)

    await run_interactive(igsid, webhook_url, settings, show_payload)


if __name__ == "__main__":
    asyncio.run(main())
