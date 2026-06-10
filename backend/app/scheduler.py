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
    scheduler.start()
    logger.info(
        "Scheduler started — daily briefing at 09:00 IST, "
        "daily learning at 00:30 IST, "
        "weekly quality check at 23:30 IST Sunday."
    )


def stop_scheduler() -> None:
    """Gracefully shut down the scheduler. Called on app shutdown."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
