"""ORM model for an individual login identity belonging to a Client business."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

ROLES = {"owner", "manager", "staff"}

# Every permission key an invited user can ever hold. Anything not in this
# closed list has no corresponding checklist item and stays Owner-only.
PERMISSION_KEYS = {
    "catalog_edit",
    "comment_settings",
    "nudge_settings",
    "order_view",
    "manual_reply",
    "mark_packed",
    "manual_utility_send",
    "analytics_view",
}

# Pre-filled checklist state for each invite role template — still fully
# editable by the Owner after the invite is created.
MANAGER_PRESET_PERMISSIONS = [
    "catalog_edit",
    "comment_settings",
    "nudge_settings",
    "order_view",
    "manual_reply",
    "mark_packed",
    "manual_utility_send",
    "analytics_view",
]

STAFF_PRESET_PERMISSIONS = [
    "order_view",
    "manual_reply",
    "mark_packed",
    "manual_utility_send",
]


class User(Base):
    """
    One row per person who can log into a Client's SellerTalk24 dashboard.

    The first User for a Client is always role="owner", created alongside the
    Client at registration. Additional rows are created via the Team invite
    flow (see app/routers/team.py) with role="manager"/"staff".

    hashed_password is nullable: it is None between invite creation and the
    invitee accepting the invite and setting their own password.

    permissions is the checklist of keys in PERMISSION_KEYS this user has been
    granted. It is read for owner=False users only — an owner implicitly has
    every capability regardless of what this array contains. role is never
    read from this array for the hard-locked, Owner-only actions (billing,
    channel connect/disconnect, staff management, credential display) — those
    always check role == "owner" directly so a corrupted/tampered permissions
    array can never grant them.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    hashed_password: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String, default="staff", server_default="staff")
    permissions: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    client = relationship("Client")

    @property
    def is_owner(self) -> bool:
        """Whether this user holds the Owner role (bypasses the permissions checklist)."""
        return self.role == "owner"

    def has_permission(self, key: str) -> bool:
        """Return True if this user may perform the given checklist-gated action."""
        return self.is_owner or key in (self.permissions or [])
