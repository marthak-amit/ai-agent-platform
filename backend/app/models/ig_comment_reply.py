"""ORM model for the IG comment → private-reply DM auto-trigger log."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class IgCommentReply(Base):
    """
    One row per Instagram comment the auto-reply trigger has processed.

    Serves three purposes: comment_id dedup (Meta retries webhooks), the
    rate-limit queue (rows land here as 'pending' when the 200/hour cap is
    hit, drained later by the scheduler), and the analytics source-of-truth
    for today's-count / conversion stats.

    status: 'sent' / 'pending' / 'failed' / 'skipped'.
    """

    __tablename__ = "ig_comment_replies"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), nullable=False, index=True)

    comment_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    commenter_igsid: Mapped[str] = mapped_column(String, nullable=False)
    media_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Persisted so the scheduler's drain job can run the SAME comment text
    # through handle_inbound_message later, for rows queued past the rate cap.
    comment_text: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(String, default="pending", nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
