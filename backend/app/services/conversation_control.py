"""
Bot pause / resume for a conversation (human takeover).

`Conversation.ai_enabled` stays THE flag the pipeline reads (bot_paused is its
inverse); this module owns every write to it so the bookkeeping columns stay
consistent:

  bot_pause_source  'human_send'  — paused automatically because staff sent a
                                    message from the dashboard; auto-resumes
                                    after Client.bot_auto_resume_minutes idle.
                    'manual'      — staff toggled it; never auto-resumes.
                    NULL          — legacy/escalation pauses; never auto-resume.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.services import realtime_service

logger = logging.getLogger(__name__)

SOURCE_HUMAN_SEND = "human_send"
SOURCE_MANUAL = "manual"


def _utc(dt: datetime | None) -> datetime | None:
    """Coerce a naive timestamp to UTC-aware."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def auto_resume_at(conv, client) -> datetime | None:
    """
    When a human_send pause will auto-resume, or None if it never will.

    A pause auto-resumes `bot_auto_resume_minutes` after the last human
    activity; 0/None disables auto-resume entirely.
    """
    if conv.ai_enabled is not False or conv.bot_pause_source != SOURCE_HUMAN_SEND:
        return None
    minutes = getattr(client, "bot_auto_resume_minutes", None)
    last = _utc(conv.human_last_activity_at or conv.bot_paused_at)
    if not minutes or last is None:
        return None
    return last + timedelta(minutes=minutes)


def auto_resume_due(conv, client, now: datetime | None = None) -> bool:
    """True when a human_send pause has been idle long enough to resume."""
    due = auto_resume_at(conv, client)
    return due is not None and (now or datetime.now(timezone.utc)) >= due


async def pause_bot(db, client, conv, *, source: str, note: str | None = None) -> None:
    """Pause the bot. A human_send pause re-stamps activity so the idle timer restarts."""
    now = datetime.now(timezone.utc)
    if conv.ai_enabled is not False:
        conv.taken_over_at = now
        conv.bot_paused_at = now
        conv.bot_pause_source = source
    elif source == SOURCE_MANUAL:
        # An explicit manual pause upgrades an auto pause so it stops auto-resuming.
        conv.bot_pause_source = SOURCE_MANUAL
    conv.ai_enabled = False
    if note is not None:
        conv.taken_over_note = note
    if source == SOURCE_HUMAN_SEND:
        conv.human_last_activity_at = now
    await db.commit()
    realtime_service.publish(client.id, "conversation_updated", {"conversation_id": conv.id, "bot_paused": True})


async def resume_bot(db, client, conv) -> None:
    """
    Resume the bot with a clean slate.

    Attempt counters are reset — otherwise a stale count instantly re-trips the
    escalation cap on the very next message (resume → re-escalate loop).
    """
    conv.ai_enabled = True
    conv.taken_over_at = None
    conv.taken_over_note = None
    conv.bot_paused_at = None
    conv.bot_pause_source = None
    conv.human_last_activity_at = None
    conv.slot_attempt_count = 0
    conv.slot_attempt_slot = None
    conv.off_topic_count = 0
    await db.commit()
    realtime_service.publish(client.id, "conversation_updated", {"conversation_id": conv.id, "bot_paused": False})


async def maybe_auto_resume(db, client, conv) -> bool:
    """Resume the bot if its human_send pause has idled out. Returns True when resumed."""
    if auto_resume_due(conv, client):
        await resume_bot(db, client, conv)
        logger.info("Bot auto-resumed for conv=%s after idle.", conv.id)
        return True
    return False
