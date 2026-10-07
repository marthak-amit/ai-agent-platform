"""ORM models for platform operators (admin panel): AdminUser and AdminAuditLog."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# role -> permission keys. "superadmin" holds everything; the others are narrower job functions.
ADMIN_PERMISSIONS: dict[str, frozenset[str]] = {
    "superadmin": frozenset(
        {
            "overview.read", "clients.read", "clients.write", "clients.impersonate", "conversations.read",
            "billing.read", "billing.write", "plans.write", "usage.read",
            "system.read", "audit.read", "admins.manage",
        }
    ),
    "support": frozenset(
        {
            "overview.read", "clients.read", "clients.write", "clients.impersonate", "conversations.read",
            "billing.read", "usage.read", "system.read",
        }
    ),
    "billing": frozenset(
        {"overview.read", "clients.read", "billing.read", "billing.write", "plans.write", "usage.read"}
    ),
    "viewer": frozenset({"overview.read", "clients.read", "billing.read", "usage.read", "system.read"}),
}
ADMIN_ROLES = tuple(ADMIN_PERMISSIONS)


def permissions_for_role(role: str) -> frozenset[str]:
    """Permission keys granted to *role* (empty for an unknown role — fail closed)."""
    return ADMIN_PERMISSIONS.get(role, frozenset())


class AdminUser(Base):
    """
    A platform operator who may sign in to the /admin panel.

    Entirely separate from `users`/`clients` (the tenant login system) so a leaked tenant token can never
    reach admin routes and vice versa. `token_version` is embedded in every issued JWT; bumping it (logout,
    password change, deactivation, 2FA change) revokes all outstanding tokens for the account.
    `totp_secret_enc` is the Fernet-encrypted base32 TOTP secret (key derived from SECRET_KEY).
    """

    __tablename__ = "admin_users"
    __table_args__ = (CheckConstraint("role in ('superadmin','support','billing','viewer')", name="ck_admin_users_role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, default="viewer", server_default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    totp_secret_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    last_totp_step: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # replay guard

    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_ip: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    @property
    def permissions(self) -> frozenset[str]:
        """Permission keys derived from the role (never stored, so a role change takes effect immediately)."""
        return permissions_for_role(self.role)


class AdminAuditLog(Base):
    """
    Append-only record of everything an admin did (and of sign-in attempts).

    `actor` is the admin's email, or "admin-key" for the legacy shared-key path. `client_id` is a soft
    reference (no FK) so the trail survives client deletion. Never store secrets/tokens in `detail`.
    """

    __tablename__ = "admin_audit_log"
    __table_args__ = (
        Index("ix_admin_audit_log_created", "created_at"),
        Index("ix_admin_audit_log_client", "client_id", "created_at"),
        Index("ix_admin_audit_log_actor", "actor", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    admin_user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    target_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    client_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    detail: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict)
    ip: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
