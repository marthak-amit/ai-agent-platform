"""
APScheduler setup for recurring background jobs.

Jobs:
  daily_briefing   — fires at 09:00 IST every day; sends WhatsApp morning summaries.
  daily_learning   — fires at 00:30 IST every day; auto-learns FAQs + saves order examples.
  weekly_quality   — fires at 23:30 IST every Sunday; scores agent quality and warns if < 80 %.
  abandoned_intent_followup — fires every 6h; nudges customers with an open draft order
                               (no payment yet) who have been idle 6h+, guarded to only
                               send free-form WhatsApp/Instagram text within Meta's 24h
                               customer-service window.
  drain_pending_comment_replies — fires every 5min; sends IG comment private-replies
                               that were queued because the client's IG account was
                               over Meta's 200/hour automated-DM cap when the comment
                               first arrived.
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
# "payment" is included: an Order row already exists as pending_payment there,
# customer just hasn't sent "paid" yet — the highest-value nudge target.
_FOLLOWUP_ELIGIBLE_STAGES = {"order_collection", "awaiting_final_confirmation", "payment"}
# Nudge once the draft order (no payment yet) has been idle this long.
_FOLLOWUP_MIN_IDLE_HOURS = 6
# Meta's free-form customer-service window closes 24h after the customer's
# last inbound message — never attempt a free-form send past this; flag it
# for a manual/template follow-up instead.
_FOLLOWUP_WINDOW_HOURS = 24
# No repeat follow-up for the same SKU within this many days.
_FOLLOWUP_COOLDOWN_DAYS = 7


async def _abandoned_intent_followup_job() -> None:
    """
    Scheduled job: send a single free-form nudge to customers with an open
    draft order (no payment yet) who have gone quiet for 6h+.

    Conditions (all must be true to send):
      a) conv.current_stage in order_collection / awaiting_final_confirmation / payment
      b) conv.last_followup_sku != conv.pending_product_sku (no same-SKU repeat)
      c) conv.followup_sent_at is None or > 7 days ago (per-customer cooldown)
      d) 6h+ elapsed since the customer's last INBOUND message
      e) pending_product_sku is set (we know what product to mention)

    Idle time and the 24h-window guard are both measured from the customer's
    last inbound message (role='user') — matching Meta's actual customer-
    service window definition, not the last message of any role. Past 24h
    idle, the free-form send is skipped and flagged (event=nudge_needs_template)
    rather than attempting a send Meta will reject.
    """
    from app.db import _get_session_factory

    factory = _get_session_factory()
    async with factory() as db:
        await _send_abandoned_intent_followups(db)


async def _send_abandoned_intent_followups(db) -> None:
    """Core logic for abandoned-intent follow-ups; separated for testability."""
    from datetime import timezone as _tz

    from sqlalchemy import select

    from app.models.client import Client
    from app.models.conversation import Conversation
    from app.models.message import Message
    from app.models.order import Order

    now = datetime.now(_tz.utc)
    min_idle = timedelta(hours=_FOLLOWUP_MIN_IDLE_HOURS)
    window = timedelta(hours=_FOLLOWUP_WINDOW_HOURS)
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

            # Condition d: idle time from the customer's last INBOUND message only.
            last_inbound_result = await db.execute(
                select(Message)
                .where(Message.conversation_id == conv.id, Message.role == "user")
                .order_by(Message.created_at.desc())
                .limit(1)
            )
            last_inbound = last_inbound_result.scalar_one_or_none()
            if last_inbound is None:
                continue

            last_ts = last_inbound.created_at
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=_tz.utc)
            idle = now - last_ts
            if idle < min_idle:
                continue
            if idle > window:
                # Outside Meta's 24h free-form window — flag for a manual/
                # template follow-up instead of sending (would be rejected).
                logger.warning(
                    "event=nudge_needs_template conv=%s sku=%s idle_hours=%.1f",
                    conv.id, sku, idle.total_seconds() / 3600,
                )
                continue

            client_result = await db.execute(
                select(Client).where(Client.id == conv.client_id)
            )
            client = client_result.scalar_one_or_none()
            if not client or not client.is_active:
                continue

            # Resolve product name for the message
            product_name = sku  # fallback to SKU if product not found
            try:
                from app.services.catalogue_service import find_product_by_sku
                _prod = await find_product_by_sku(db, client.id, sku)
                if _prod:
                    product_name = _prod.name
            except Exception as exc:
                logger.warning("Follow-up product name lookup failed: %s", exc)

            name = conv.customer_name or "there"

            if conv.current_stage == "payment":
                # An Order row already exists as pending_payment — resend UPI
                # payment instructions instead of a generic "come back" nudge.
                order_result = await db.execute(
                    select(Order)
                    .where(Order.conversation_id == conv.id, Order.status == "pending_payment")
                    .order_by(Order.created_at.desc())
                    .limit(1)
                )
                order = order_result.scalar_one_or_none()
                if not order or not client.upi_id:
                    continue
                from app.services.language_templates import format_price, get_template
                lang = getattr(conv, "last_customer_language", "english") or "english"
                followup_text = (
                    f"Hi {name}! ⏰ Just a reminder — your order is waiting for payment.\n\n"
                    + get_template(
                        lang, "upi_instructions",
                        total=format_price(order.total_amount),
                        order_number=order.order_number,
                        upi_id=client.upi_id,
                    )
                )
            else:
                # TODO: Replace with approved WhatsApp template (cart_reminder_v1 or similar)
                # once submitted to Meta — free-form business-initiated messages outside
                # the 24-hour window are REJECTED by Meta in production; the window guard
                # above keeps this send inside that window.
                followup_text = (
                    f"Hi {name}! 👋 Your {product_name} order is waiting — "
                    f"just pick up where you left off. "
                    f"Reply to continue or ask any questions! 😊"
                )

            # Send — channel-aware (mirrors followup_service.send_followups)
            try:
                from app.services import outbound
                from app.services.send_gate import MessageKind

                if conv.channel == "instagram":
                    if not client.instagram_account_id:
                        raise ValueError("No instagram_account_id for client — cannot send Instagram nudge.")
                    _sent = await outbound.ig_send_dm(
                        client.instagram_account_id,
                        conv.phone_number,
                        followup_text,
                        kind=MessageKind.NUDGE,
                        db=db,
                        client_id=conv.client_id,
                        conversation_id=conv.id,
                    )
                else:
                    _sent = await outbound.send_text(
                        conv.phone_number,
                        followup_text,
                        kind=MessageKind.NUDGE,
                        db=db,
                        client_id=conv.client_id,
                        conversation_id=conv.id,
                    )
                if _sent is None:
                    # Gate refused (opt-out / block / window re-closed since the
                    # scheduler's own check) — do not mark as followed-up.
                    continue
            except Exception as exc:
                logger.warning(
                    "Follow-up send failed for conv=%s phone=%s channel=%s: %s",
                    conv.id, conv.phone_number, conv.channel, exc,
                )
                continue

            # Persist tracking fields
            conv.last_followup_sku = sku
            conv.followup_sent_at = now
            await db.commit()
            sent_count += 1
            logger.info(
                "Abandoned-intent follow-up sent: conv=%s sku=%s phone=%s stage=%s",
                conv.id, sku, conv.phone_number, conv.current_stage,
            )

        except Exception as exc:
            logger.error("Follow-up processing error for conv=%s: %s", conv.id, exc)

    if sent_count:
        logger.info("Abandoned-intent follow-up job complete — sent %d messages.", sent_count)


async def _drain_pending_comment_replies_job() -> None:
    """Scheduled job: open a DB session and drain queued IG comment replies."""
    from app.db import _get_session_factory

    factory = _get_session_factory()
    async with factory() as db:
        await _drain_pending_comment_replies(db)


async def _drain_pending_comment_replies(db) -> None:
    """
    Send any ig_comment_replies rows still "pending" — comments whose reply
    was deferred because the client's IG account was over Meta's 200/hour
    automated-DM cap at the time. Re-checks the same rate limiter per row so
    a burst of queued rows still respects the cap rather than dumping all of
    them at once.
    """
    from sqlalchemy import select

    from app.models.client import Client
    from app.models.ig_comment_reply import IgCommentReply
    from app.services import ig_comment_service

    result = await db.execute(
        select(IgCommentReply)
        .where(IgCommentReply.status == "pending")
        .order_by(IgCommentReply.created_at.asc())
    )
    pending_rows = result.scalars().all()
    if not pending_rows:
        return

    drained = 0
    for row in pending_rows:
        try:
            client_result = await db.execute(select(Client).where(Client.id == row.client_id))
            client = client_result.scalar_one_or_none()
            if not client or not client.is_active or not client.instagram_account_id:
                continue

            if await ig_comment_service.is_comment_dm_rate_limited(client.instagram_account_id):
                # Still over the cap — leave it pending for the next drain cycle.
                continue

            await ig_comment_service.send_comment_reply(
                db, client, client.instagram_account_id, row
            )
            drained += 1
        except Exception as exc:
            logger.error("Comment-reply drain error for row=%s: %s", row.id, exc)

    if drained:
        logger.info("Comment-reply drain job complete — sent %d queued replies.", drained)


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
    scheduler.add_job(
        _drain_pending_comment_replies_job,
        CronTrigger(minute="*/5"),
        id="drain_pending_comment_replies",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "Scheduler started — daily briefing at 09:00 IST, "
        "daily learning at 00:30 IST, "
        "weekly quality check at 23:30 IST Sunday, "
        "open-order nudge check every 6 hours (6h-idle trigger, 24h window guard), "
        "comment-reply drain every 5 minutes."
    )


def stop_scheduler() -> None:
    """Gracefully shut down the scheduler. Called on app shutdown."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
