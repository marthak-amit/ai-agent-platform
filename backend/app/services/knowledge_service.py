"""
Knowledge base service.

Handles search, insertion, and retrieval of per-client Q&A entries.
Used by the webhook pipeline to inject proven answers into the system prompt,
and by the weekly auto-learning job to populate entries from conversation history.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_base import KnowledgeBase

logger = logging.getLogger(__name__)


async def search_knowledge(
    client_id: int,
    query: str,
    db: AsyncSession,
    limit: int = 3,
) -> list[KnowledgeBase]:
    """
    Keyword-overlap search over active, approved KB entries for a client.

    Scores each entry by the number of query words that appear in the
    question text. Returns up to *limit* best-matching entries, highest
    score first.  Returns an empty list when no words overlap.

    Args:
        client_id: Owning client ID.
        query:     Customer message text used as the search query.
        db:        Async DB session.
        limit:     Maximum number of entries to return.

    Returns:
        List of KnowledgeBase instances ordered by relevance score (desc).
    """
    result = await db.execute(
        select(KnowledgeBase).where(
            KnowledgeBase.client_id == client_id,
            KnowledgeBase.is_active == True,   # noqa: E712
            KnowledgeBase.is_approved == True,  # noqa: E712
        )
    )
    entries = result.scalars().all()

    query_words = set(query.lower().split())
    scored: list[tuple[int, KnowledgeBase]] = []
    for entry in entries:
        entry_words = set(entry.question.lower().split())
        overlap = len(query_words & entry_words)
        if overlap > 0:
            scored.append((overlap, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [e for _, e in scored[:limit]]
    logger.info(
        "KB search: found %d entr%s for query=%r",
        len(top),
        "y" if len(top) == 1 else "ies",
        query[:60],
    )
    return top


async def add_kb_entry(
    client_id: int,
    question: str,
    answer: str,
    source: str,
    db: AsyncSession,
    category: Optional[str] = None,
    language: str = "english",
    is_approved: bool = True,
) -> KnowledgeBase:
    """
    Insert a new knowledge base entry and flush it to the session.

    The caller is responsible for committing the session after this call
    (or the entry will be rolled back).

    Args:
        client_id:   Owning client ID.
        question:    The question text (keywords used for matching).
        answer:      The answer the agent should give.
        source:      "manual" | "auto_learned" | "pdf_upload"
        db:          Async DB session.
        category:    Optional category tag.
        language:    Language of the entry (default "english").
        is_approved: Whether the entry is immediately active (default True).

    Returns:
        The newly created KnowledgeBase instance (flushed, not committed).
    """
    entry = KnowledgeBase(
        client_id=client_id,
        question=question,
        answer=answer,
        source=source,
        category=category,
        language=language,
        is_approved=is_approved,
    )
    db.add(entry)
    await db.flush()
    logger.info(
        "KB entry added: client=%s source=%s q=%s",
        client_id, source, question[:60],
    )
    return entry


async def get_entry(
    db: AsyncSession, entry_id: int, client_id: int
) -> Optional[KnowledgeBase]:
    """Return a single KB entry owned by client_id, or None."""
    result = await db.execute(
        select(KnowledgeBase).where(
            KnowledgeBase.id == entry_id,
            KnowledgeBase.client_id == client_id,
        )
    )
    return result.scalar_one_or_none()


async def list_entries(
    db: AsyncSession,
    client_id: int,
    source: Optional[str] = None,
    category: Optional[str] = None,
    active_only: bool = False,
    skip: int = 0,
    limit: int = 100,
) -> list[KnowledgeBase]:
    """
    Return KB entries for a client with optional filters.

    Args:
        db:          Async DB session.
        client_id:   Owning client ID.
        source:      Filter by source ("manual" / "auto_learned" / "pdf_upload").
        category:    Filter by category.
        active_only: When True only return is_active=True entries.
        skip:        Pagination offset.
        limit:       Max rows.

    Returns:
        List of KnowledgeBase instances ordered by created_at desc.
    """
    from sqlalchemy import desc

    q = select(KnowledgeBase).where(KnowledgeBase.client_id == client_id)
    if source:
        q = q.where(KnowledgeBase.source == source)
    if category:
        q = q.where(KnowledgeBase.category == category)
    if active_only:
        q = q.where(KnowledgeBase.is_active == True)  # noqa: E712
    q = q.order_by(desc(KnowledgeBase.created_at)).offset(skip).limit(limit)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_stats(db: AsyncSession, client_id: int) -> dict:
    """
    Return aggregate knowledge base stats for the dashboard.

    Returns:
        Dict with total, manual_count, auto_count, active_count, most_used.
    """
    from sqlalchemy import func

    async def _count(*where) -> int:
        r = await db.execute(
            select(func.count()).select_from(KnowledgeBase).where(*where)
        )
        return r.scalar_one() or 0

    base = KnowledgeBase.client_id == client_id
    total = await _count(base)
    manual = await _count(base, KnowledgeBase.source == "manual")
    auto = await _count(base, KnowledgeBase.source == "auto_learned")
    active = await _count(base, KnowledgeBase.is_active == True)  # noqa: E712

    top_result = await db.execute(
        select(KnowledgeBase)
        .where(base, KnowledgeBase.is_active == True)  # noqa: E712
        .order_by(KnowledgeBase.usage_count.desc())
        .limit(1)
    )
    top = top_result.scalar_one_or_none()

    return {
        "total": total,
        "manual_count": manual,
        "auto_learned_count": auto,
        "active_count": active,
        "most_used_question": top.question if top else None,
        "most_used_count": top.usage_count if top else 0,
    }
