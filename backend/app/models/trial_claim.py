"""ORM model recording which WhatsApp phone_number_ids have already used their one free trial."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TrialClaim(Base):
    """
    One row per WhatsApp phone_number_id that has ever started a trial (migration 0070).

    The UNIQUE constraint is the rule "one trial per phone_number_id": it survives the client disconnecting and
    reconnecting the number, or a second account connecting the same number. Rows are never deleted.
    """

    __tablename__ = "trial_claims"
    __table_args__ = (UniqueConstraint("phone_number_id", name="uq_trial_claims_phone_number_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_number_id: Mapped[str] = mapped_column(String, nullable=False)
    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
