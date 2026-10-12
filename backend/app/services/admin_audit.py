"""
Admin audit trail: one append-only `admin_audit_log` row per operator action.

`record()` only adds the row to the caller's session, so the audit entry commits atomically with the change
it describes. `record_standalone()` opens its own session for events that must persist even when the request
fails (sign-in failures, denied actions).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_user import AdminAuditLog, AdminUser

logger = logging.getLogger(__name__)

_SECRET_KEY_FRAGMENTS = ("password", "token", "secret", "otp", "authorization", "api_key")
_MAX_DETAIL_STR = 500


@dataclass
class AdminPrincipal:
    """Who is calling an admin endpoint: a signed-in operator, or the legacy shared X-Admin-Key."""

    actor: str
    permissions: frozenset[str]
    ip: str = "unknown"
    admin: Optional[AdminUser] = None
    via: str = "jwt"  # "jwt" | "key"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def admin_user_id(self) -> Optional[int]:
        """The operator's id, or None for the legacy key."""
        return self.admin.id if self.admin is not None else None

    def can(self, permission: str) -> bool:
        """Whether this principal holds *permission*."""
        return permission in self.permissions


def sanitize_detail(value: Any, _depth: int = 0) -> Any:
    """Drop secret-looking keys and truncate long strings so nothing sensitive reaches the audit log."""
    if _depth > 4:
        return "…"
    if isinstance(value, dict):
        return {
            str(k): ("[redacted]" if any(f in str(k).lower() for f in _SECRET_KEY_FRAGMENTS) else sanitize_detail(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_detail(v, _depth + 1) for v in list(value)[:50]]
    if isinstance(value, str):
        return value[:_MAX_DETAIL_STR]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_DETAIL_STR]


def _build_row(
    principal: AdminPrincipal | None,
    actor: str | None,
    ip: str | None,
    action: str,
    target_type: str | None,
    target_id: Any,
    client_id: int | None,
    reason: str,
    detail: dict[str, Any] | None,
    success: bool,
) -> AdminAuditLog:
    """Assemble an AdminAuditLog row from a principal (or an explicit actor for unauthenticated events)."""
    return AdminAuditLog(
        actor=(principal.actor if principal else actor) or "unknown",
        admin_user_id=principal.admin_user_id if principal else None,
        action=action,
        target_type=target_type,
        target_id=None if target_id is None else str(target_id),
        client_id=client_id,
        reason=(reason or "")[:1000],
        detail=sanitize_detail(detail or {}),
        ip=(principal.ip if principal else ip),
        success=success,
    )


def record(
    db: AsyncSession,
    principal: AdminPrincipal,
    action: str,
    *,
    target_type: str | None = None,
    target_id: Any = None,
    client_id: int | None = None,
    reason: str = "",
    detail: dict[str, Any] | None = None,
    success: bool = True,
) -> AdminAuditLog:
    """Add an audit row to *db* (the caller commits, so it lands atomically with the change)."""
    row = _build_row(principal, None, None, action, target_type, target_id, client_id, reason, detail, success)
    db.add(row)
    return row


async def record_standalone(
    *,
    action: str,
    actor: str,
    ip: str | None = None,
    target_type: str | None = None,
    target_id: Any = None,
    client_id: int | None = None,
    reason: str = "",
    detail: dict[str, Any] | None = None,
    success: bool = True,
) -> None:
    """Persist an audit row in its own session; never raises (auditing must not break the request)."""
    from app.db import _get_session_factory

    try:
        async with _get_session_factory()() as session:
            session.add(_build_row(None, actor, ip, action, target_type, target_id, client_id, reason, detail, success))
            await session.commit()
    except Exception:
        logger.exception("Failed to write admin audit row (action=%s actor=%s)", action, actor)
