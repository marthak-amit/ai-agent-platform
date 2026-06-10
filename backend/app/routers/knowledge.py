"""
Knowledge base API router (protected — requires JWT).

Provides the React dashboard with CRUD endpoints for per-client Q&A entries
and aggregate stats. Auto-learned entries are created by the weekly scheduler;
manual entries can be created via POST /knowledge.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.routers.auth import get_current_client
from app.services import knowledge_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/knowledge", tags=["knowledge"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class KBEntryOut(BaseModel):
    """Knowledge base entry returned to the dashboard."""

    id: int
    client_id: int
    question: str
    answer: str
    source: str
    category: Optional[str]
    language: str
    usage_count: int
    helpful_count: int
    is_active: bool
    is_approved: bool
    created_at: Optional[str]
    updated_at: Optional[str]

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_safe(cls, e) -> "KBEntryOut":
        """Convert KnowledgeBase ORM to schema, stringifying datetimes."""
        def _fmt(dt):
            return dt.isoformat() if dt else None

        return cls(
            id=e.id,
            client_id=e.client_id,
            question=e.question,
            answer=e.answer,
            source=e.source,
            category=e.category,
            language=e.language,
            usage_count=e.usage_count,
            helpful_count=e.helpful_count,
            is_active=e.is_active,
            is_approved=e.is_approved,
            created_at=_fmt(e.created_at),
            updated_at=_fmt(e.updated_at),
        )


class KBEntryCreate(BaseModel):
    """Payload for creating a manual KB entry."""

    question: str
    answer: str
    category: Optional[str] = None
    language: str = "english"


class KBEntryUpdate(BaseModel):
    """Fields that can be updated on an existing entry."""

    question: Optional[str] = None
    answer: Optional[str] = None
    category: Optional[str] = None
    language: Optional[str] = None


class KBStatsOut(BaseModel):
    """Aggregate knowledge base stats."""

    total: int
    manual_count: int
    auto_learned_count: int
    active_count: int
    most_used_question: Optional[str]
    most_used_count: int


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("", response_model=list[KBEntryOut])
async def list_knowledge(
    source: Optional[str] = Query(None, description="manual | auto_learned | pdf_upload"),
    category: Optional[str] = Query(None),
    active_only: bool = Query(False),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    client=Depends(get_current_client),
):
    """Return all KB entries for the authenticated client."""
    entries = await knowledge_service.list_entries(
        db,
        client_id=client.id,
        source=source,
        category=category,
        active_only=active_only,
        skip=skip,
        limit=limit,
    )
    return [KBEntryOut.from_orm_safe(e) for e in entries]


@router.get("/stats", response_model=KBStatsOut)
async def get_stats(
    db: AsyncSession = Depends(get_db),
    client=Depends(get_current_client),
):
    """Return aggregate KB stats for the dashboard."""
    return await knowledge_service.get_stats(db, client.id)


@router.post("", response_model=KBEntryOut, status_code=201)
async def create_entry(
    payload: KBEntryCreate,
    db: AsyncSession = Depends(get_db),
    client=Depends(get_current_client),
):
    """Add a new manual KB entry."""
    entry = await knowledge_service.add_kb_entry(
        client_id=client.id,
        question=payload.question,
        answer=payload.answer,
        source="manual",
        db=db,
        category=payload.category,
        language=payload.language,
    )
    await db.commit()
    await db.refresh(entry)
    return KBEntryOut.from_orm_safe(entry)


@router.put("/{entry_id}", response_model=KBEntryOut)
async def update_entry(
    entry_id: int,
    payload: KBEntryUpdate,
    db: AsyncSession = Depends(get_db),
    client=Depends(get_current_client),
):
    """Edit an existing KB entry (question, answer, category, or language)."""
    entry = await knowledge_service.get_entry(db, entry_id, client.id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    if payload.question is not None:
        entry.question = payload.question
    if payload.answer is not None:
        entry.answer = payload.answer
    if payload.category is not None:
        entry.category = payload.category
    if payload.language is not None:
        entry.language = payload.language

    await db.commit()
    await db.refresh(entry)
    return KBEntryOut.from_orm_safe(entry)


@router.delete("/{entry_id}", status_code=204)
async def delete_entry(
    entry_id: int,
    db: AsyncSession = Depends(get_db),
    client=Depends(get_current_client),
):
    """Permanently delete a KB entry."""
    entry = await knowledge_service.get_entry(db, entry_id, client.id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    await db.delete(entry)
    await db.commit()


@router.patch("/{entry_id}/toggle", response_model=KBEntryOut)
async def toggle_entry(
    entry_id: int,
    db: AsyncSession = Depends(get_db),
    client=Depends(get_current_client),
):
    """Enable or disable a KB entry without deleting it."""
    entry = await knowledge_service.get_entry(db, entry_id, client.id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    entry.is_active = not entry.is_active
    await db.commit()
    await db.refresh(entry)
    return KBEntryOut.from_orm_safe(entry)
