"""
APScheduler setup for recurring background jobs.

Jobs:
  daily_briefing   — fires at 09:00 IST every day; sends WhatsApp morning summaries.
  daily_learning   — fires at 00:30 IST every day; auto-learns FAQs + saves order examples.
  weekly_quality   — fires at 23:30 IST every Sunday; scores agent quality and warns if < 80 %.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")


async def _daily_briefing_job() -> None:
    """Scheduled job: open a DB session and dispatch all client briefings."""
    from app.db import _get_session_factory
    from app.services import briefing_service

    factory = _get_session_factory()
    async with factory() as db:
        await briefing_service.send_daily_briefings(db)


async def _daily_learning_job() -> None:
    """Scheduled job: process yesterday's conversations and populate the knowledge base."""
    from app.db import _get_session_factory

    factory = _get_session_factory()
    async with factory() as db:
        await _auto_learn_from_conversations(db)


async def _auto_learn_from_conversations(db) -> None:
    """
    For each active client, mine yesterday's conversations for:
      1. Questions asked 2+ times → saved as KB entries.
      2. Unique conversations that led to a confirmed order → saved as examples.

    Only persists entries not already present to avoid duplicates.
    Logs a daily summary at the end.
    """
    from sqlalchemy import func, select

    from app.models.client import Client
    from app.models.conversation import Conversation
    from app.models.knowledge_base import KnowledgeBase
    from app.models.message import Message
    from app.models.order import Order
    from app.services import knowledge_service

    result = await db.execute(
        select(Client).where(Client.is_active == True)  # noqa: E712
    )
    clients = result.scalars().all()

    # Look back 2 days to capture yesterday's full window regardless of timezone drift
    since = datetime.now(timezone.utc) - timedelta(days=2)

    for client in clients:
        try:
            conv_result = await db.execute(
                select(Conversation).where(
                    Conversation.client_id == client.id,
                    Conversation.created_at > since,
                )
            )
            conversations = conv_result.scalars().all()

            if not conversations:
                continue

            # ── Phase 1: FAQ mining (threshold: 2+ occurrences) ──────────────
            question_answers: dict[str, list[str]] = {}

            for conv in conversations:
                msg_result = await db.execute(
                    select(Message)
                    .where(Message.conversation_id == conv.id)
                    .order_by(Message.id)
                )
                messages = msg_result.scalars().all()

                for i, msg in enumerate(messages[:-1]):
                    if msg.role != "user":
                        continue
                    next_msg = messages[i + 1]
                    if next_msg.role != "assistant":
                        continue
                    q = msg.content.strip()
                    a = next_msg.content.strip()
                    if len(q) <= 5 or len(a) <= 10:
                        continue
                    question_answers.setdefault(q, []).append(a)

            kb_added = 0
            for question, answers in question_answers.items():
                if len(answers) < 2:  # lowered from 3 → 2
                    continue

                best_answer = Counter(answers).most_common(1)[0][0]

                existing = await db.execute(
                    select(KnowledgeBase).where(
                        KnowledgeBase.client_id == client.id,
                        KnowledgeBase.question == question,
                    )
                )
                if existing.scalar_one_or_none():
                    continue

                await knowledge_service.add_kb_entry(
                    client_id=client.id,
                    question=question,
                    answer=best_answer,
                    source="auto_learned",
                    db=db,
                )
                kb_added += 1
                logger.info("Auto-learned KB: client=%s q=%s", client.id, question[:60])

            # ── Phase 2: Save unique successful order conversations as examples ─
            # A successful conversation is one with a confirmed order that hasn't
            # already been saved (we use source="order_example" as the marker).
            order_result = await db.execute(
                select(Order.conversation_id).where(
                    Order.client_id == client.id,
                    Order.created_at > since,
                )
            )
            order_conv_ids = {row[0] for row in order_result if row[0] is not None}

            examples_added = 0
            for conv_id in order_conv_ids:
                # Skip if we already saved this conversation as an example
                existing_ex = await db.execute(
                    select(KnowledgeBase).where(
                        KnowledgeBase.client_id == client.id,
                        KnowledgeBase.source == "order_example",
                        KnowledgeBase.answer.contains(f"conv:{conv_id}"),
                    )
                )
                if existing_ex.scalar_one_or_none():
                    continue

                msg_result = await db.execute(
                    select(Message)
                    .where(Message.conversation_id == conv_id)
                    .order_by(Message.id)
                    .limit(8)
                )
                messages = list(msg_result.scalars().all())
                if len(messages) < 4:
                    continue

                # Build a compact example transcript (first 4 turns)
                lines = []
                for m in messages[:8]:
                    role_label = "Customer" if m.role == "user" else "Agent"
                    lines.append(f"{role_label}: {m.content.strip()}")
                transcript = "\n".join(lines)

                # Use first user message as the "question" key
                first_q = next((m.content.strip() for m in messages if m.role == "user"), "")
                if not first_q:
                    continue

                await knowledge_service.add_kb_entry(
                    client_id=client.id,
                    question=first_q[:200],
                    answer=f"[conv:{conv_id}]\n{transcript}",
                    source="order_example",
                    db=db,
                )
                examples_added += 1
                logger.info("Order example saved: client=%s conv=%s", client.id, conv_id)

            await db.commit()

            # ── Total KB count for summary ────────────────────────────────────
            count_result = await db.execute(
                select(func.count()).select_from(KnowledgeBase).where(
                    KnowledgeBase.client_id == client.id,
                    KnowledgeBase.is_active == True,  # noqa: E712
                )
            )
            total_kb = count_result.scalar_one() or 0

            logger.info(
                "Daily learning complete:\n"
                " - New KB entries added: %d\n"
                " - New conversation examples: %d\n"
                " - Total KB entries: %d",
                kb_added,
                examples_added,
                total_kb,
            )

        except Exception as exc:
            logger.error("Daily learning failed for client=%s: %s", client.id, exc)


async def _weekly_quality_job() -> None:
    """Scheduled job: run agent quality tests and warn if score drops below 80 %."""
    import os
    import subprocess
    import sys

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(backend_dir, "scripts", "test_agent_quality.py")

    try:
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=backend_dir,
        )
        output = result.stdout + result.stderr
        logger.info("Weekly quality check output:\n%s", output)
        if result.returncode != 0:
            logger.warning(
                "QUALITY_ALERT: weekly agent quality check FAILED (exit %d). "
                "Score is below 80%% — review test_agent_quality.py output above.",
                result.returncode,
            )
        else:
            logger.info("Weekly quality check passed (score >= 80%%).")
    except subprocess.TimeoutExpired:
        logger.error("Weekly quality check timed out after 120 s.")
    except Exception as exc:
        logger.error("Weekly quality check error: %s", exc)


# ── Abandoned-intent follow-up constants ──────────────────────────────────────
# Only trigger for real purchase-intent stages — not for casual browsing.
_FOLLOWUP_ELIGIBLE_STAGES = {"order_collection", "awaiting_final_confirmation"}
# Send follow-up only when last activity was 24–48 hours ago.
_FOLLOWUP_MIN_IDLE_HOURS = 24
_FOLLOWUP_MAX_IDLE_HOURS = 48
# No repeat follow-up for the same SKU within this many days.
_FOLLOWUP_COOLDOWN_DAYS = 7


async def _abandoned_intent_followup_job() -> None:
    """
    Scheduled job: send a single free-form follow-up to customers who started an
    order but went quiet (24–48 hrs idle, real purchase-intent stage).

    Conditions (all must be true to send):
      a) conv.current_stage in order_collection / awaiting_final_confirmation
      b) conv.last_followup_sku != conv.pending_product_sku (no same-SKU repeat)
      c) conv.followup_sent_at is None or > 7 days ago (per-customer cooldown)
      d) Last message activity was 24–48 hours ago
      e) pending_product_sku is set (we know what product to mention)

    NOTE: This sends a free-form WhatsApp message. This ONLY works for
    sandbox/test numbers within an active 24-hour customer-initiated window.
    TODO: Replace with approved WhatsApp template (cart_reminder_v1 or similar)
    once submitted to Meta — free-form business-initiated messages outside the
    24-hour window will be REJECTED by Meta in production.
    """
    from app.db import _get_session_factory

    factory = _get_session_factory()
    async with factory() as db:
        await _send_abandoned_intent_followups(db)


async def _send_abandoned_intent_followups(db) -> None:
    """Core logic for abandoned-intent follow-ups; separated for testability."""
    from datetime import timezone as _tz

    from sqlalchemy import select

    from app.models.conversation import Conversation
    from app.models.message import Message

    now = datetime.now(_tz.utc)
    min_idle = timedelta(hours=_FOLLOWUP_MIN_IDLE_HOURS)
    max_idle = timedelta(hours=_FOLLOWUP_MAX_IDLE_HOURS)
    cooldown = timedelta(days=_FOLLOWUP_COOLDOWN_DAYS)

    # Load all eligible conversations (stage + has pending SKU)
    result = await db.execute(
        select(Conversation).where(
            Conversation.current_stage.in_(list(_FOLLOWUP_ELIGIBLE_STAGES)),
            Conversation.pending_product_sku.isnot(None),
            Conversation.ai_enabled == True,  # noqa: E712
        )
    )
    candidates = result.scalars().all()
    sent_count = 0

    for conv in candidates:
        try:
            sku = conv.pending_product_sku

            # Condition b: skip if same SKU was already followed up
            if conv.last_followup_sku == sku:
                continue

            # Condition c: skip if followed up within the cooldown window
            if conv.followup_sent_at and (now - conv.followup_sent_at) < cooldown:
                continue

            # Condition d: check last message timestamp
            last_msg_result = await db.execute(
                select(Message)
                .where(Message.conversation_id == conv.id)
                .order_by(Message.created_at.desc())
                .limit(1)
            )
            last_msg = last_msg_result.scalar_one_or_none()
            if last_msg is None:
                continue

            last_ts = last_msg.created_at
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=_tz.utc)
            idle = now - last_ts
            if idle < min_idle or idle > max_idle:
                continue

            # Resolve product name for the message
            product_name = sku  # fallback to SKU if product not found
            try:
                from app.models.client import Client
                from app.services.catalogue_service import find_product_by_sku

                # Look up client from conversation (need client_id)
                # Conversations don't store client_id directly; look up via customer
                from app.services.customer_service import get_customer as _get_cust
                # Skip product name lookup if client unknown — send with SKU
                client_result = await db.execute(
                    select(Client).where(Client.is_active == True).limit(1)  # noqa: E712
                )
                _client = client_result.scalar_one_or_none()
                if _client:
                    _prod = await find_product_by_sku(db, _client.id, sku)
                    if _prod:
                        product_name = _prod.name
            except Exception as exc:
                logger.warning("Follow-up product name lookup failed: %s", exc)

            # Build the follow-up message
            name = conv.customer_name or "there"
            # TODO: Replace with approved WhatsApp template (cart_reminder_v1 or similar)
            # once submitted to Meta — free-form business-initiated messages outside
            # the 24-hour window will be REJECTED by Meta in production; this free-text
            # version only works in sandbox/test numbers within active 24-hr windows.
            followup_text = (
                f"Hi {name}! 👋 Your {product_name} order is waiting — "
                f"just pick up where you left off. "
                f"Reply to continue or ask any questions! 😊"
            )

            # Send via WhatsApp
            try:
                from app.services.whatsapp_service import send_text_message
                await send_text_message(
                    to_phone_number=conv.phone_number,
                    message_text=followup_text,
                )
            except Exception as exc:
                logger.warning(
                    "Follow-up send failed for conv=%s phone=%s: %s",
                    conv.id, conv.phone_number, exc,
                )
                continue

            # Persist tracking fields
            conv.last_followup_sku = sku
            conv.followup_sent_at = now
            await db.commit()
            sent_count += 1
            logger.info(
                "Abandoned-intent follow-up sent: conv=%s sku=%s phone=%s",
                conv.id, sku, conv.phone_number,
            )

        except Exception as exc:
            logger.error("Follow-up processing error for conv=%s: %s", conv.id, exc)

    if sent_count:
        logger.info("Abandoned-intent follow-up job complete — sent %d messages.", sent_count)


def start_scheduler() -> None:
    """Register all jobs and start the scheduler. Called once on app startup."""
    scheduler.add_job(
        _daily_briefing_job,
        CronTrigger(hour=9, minute=0),
        id="daily_briefing",
        replace_existing=True,
    )
    scheduler.add_job(
        _daily_learning_job,
        CronTrigger(hour=0, minute=30, timezone="Asia/Kolkata"),
        id="daily_learning",
        replace_existing=True,
    )
    scheduler.add_job(
        _weekly_quality_job,
        CronTrigger(day_of_week="sun", hour=23, minute=30, timezone="Asia/Kolkata"),
        id="weekly_quality",
        replace_existing=True,
    )
    scheduler.add_job(
        _abandoned_intent_followup_job,
        CronTrigger(hour="*/6", minute=0, timezone="Asia/Kolkata"),
        id="abandoned_intent_followup",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "Scheduler started — daily briefing at 09:00 IST, "
        "daily learning at 00:30 IST, "
        "weekly quality check at 23:30 IST Sunday, "
        "abandoned-intent follow-up every 6 hours."
    )


def stop_scheduler() -> None:
    """Gracefully shut down the scheduler. Called on app shutdown."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
