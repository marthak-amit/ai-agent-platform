"""
Operator management and the audit trail (superadmin only unless noted).

  GET   /admin/users                       list operators                         admins.manage
  POST  /admin/users                       create an operator (temp password)     admins.manage
  PATCH /admin/users/{id}                  change name / role / active            admins.manage
  POST  /admin/users/{id}/reset-password   new temp password, revoke sessions     admins.manage
  POST  /admin/users/{id}/reset-2fa        remove 2FA, revoke sessions            admins.manage
  POST  /admin/users/{id}/unlock           clear a lockout                        admins.manage
  GET   /admin/audit-log                   searchable trail of admin actions      audit.read

Guards: you cannot change your own role/active flag, and the last active superadmin can never be demoted or
deactivated (so the panel can't lock everyone out). Role / active / password / 2FA changes revoke the
target's sessions.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.admin_user import ADMIN_ROLES, AdminUser
from app.routers.admin_deps import require_perm
from app.schemas.admin_panel import (
    AdminUserCreate,
    AdminUserCreated,
    AdminUserOut,
    AdminUserUpdate,
    AuditLogPage,
    OkOut,
)
from app.services import admin_audit, admin_auth_service, admin_panel_service, auth_service
from app.services.admin_audit import AdminPrincipal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin-users"])

ManagePrincipal = Annotated[AdminPrincipal, Depends(require_perm("admins.manage"))]


def _err(code: str, message: str, http_status: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    """HTTPException with the panel's {code, message} body."""
    return HTTPException(http_status, {"code": code, "message": message})


async def _get_or_404(db: AsyncSession, admin_id: int) -> AdminUser:
    """Load an operator or raise 404."""
    admin = await db.get(AdminUser, admin_id)
    if admin is None:
        raise _err("not_found", "Operator not found.", status.HTTP_404_NOT_FOUND)
    return admin


async def _active_superadmins(db: AsyncSession) -> int:
    """How many active superadmins exist."""
    return int(
        await db.scalar(select(func.count(AdminUser.id)).where(AdminUser.role == "superadmin", AdminUser.is_active.is_(True))) or 0
    )


@router.get("/admin/users", response_model=list[AdminUserOut])
async def list_admin_users(_: ManagePrincipal, db: AsyncSession = Depends(get_db)) -> list[AdminUser]:
    """All operators, oldest first."""
    return list((await db.execute(select(AdminUser).order_by(AdminUser.id))).scalars().all())


@router.post("/admin/users", response_model=AdminUserCreated, status_code=status.HTTP_201_CREATED)
async def create_admin_user(body: AdminUserCreate, principal: ManagePrincipal, db: AsyncSession = Depends(get_db)) -> AdminUserCreated:
    """Create an operator. The temporary password is returned once and must be changed at first sign-in."""
    temp = body.password or admin_panel_service.generate_temporary_password()
    try:
        admin = await admin_auth_service.create_admin(
            db, email=body.email, password=temp, name=body.name, role=body.role,
            created_by=principal.actor, must_change_password=True,
        )
    except ValueError as exc:
        raise _err("invalid", str(exc)) from exc
    admin_audit.record(
        db, principal, "admin_user.create", target_type="admin_user", target_id=admin.id,
        detail={"email": admin.email, "role": admin.role},
    )
    await db.commit()
    return AdminUserCreated(admin=AdminUserOut.model_validate(admin), temporary_password=temp)


@router.patch("/admin/users/{admin_id}", response_model=AdminUserOut)
async def update_admin_user(
    admin_id: int, body: AdminUserUpdate, principal: ManagePrincipal, db: AsyncSession = Depends(get_db)
) -> AdminUser:
    """Change name / role / active. Role or active changes revoke the target's sessions."""
    admin = await _get_or_404(db, admin_id)
    is_self = principal.admin is not None and principal.admin.id == admin.id
    before = {"name": admin.name, "role": admin.role, "is_active": admin.is_active}

    if body.role is not None and body.role != admin.role:
        if body.role not in ADMIN_ROLES:
            raise _err("invalid_role", f"Role must be one of: {', '.join(ADMIN_ROLES)}.")
        if is_self:
            raise _err("self_change", "You can't change your own role.", status.HTTP_403_FORBIDDEN)
        if admin.role == "superadmin" and admin.is_active and await _active_superadmins(db) <= 1:
            raise _err("last_superadmin", "At least one active superadmin is required.", status.HTTP_409_CONFLICT)
        admin.role = body.role
        admin_auth_service.revoke_sessions(admin)
    if body.is_active is not None and body.is_active != admin.is_active:
        if is_self:
            raise _err("self_change", "You can't deactivate yourself.", status.HTTP_403_FORBIDDEN)
        if not body.is_active and admin.role == "superadmin" and await _active_superadmins(db) <= 1:
            raise _err("last_superadmin", "At least one active superadmin is required.", status.HTTP_409_CONFLICT)
        admin.is_active = body.is_active
        admin_auth_service.revoke_sessions(admin)
    if body.name is not None:
        admin.name = body.name.strip()[:100]

    admin_audit.record(
        db, principal, "admin_user.update", target_type="admin_user", target_id=admin.id,
        detail={"before": before, "after": {"name": admin.name, "role": admin.role, "is_active": admin.is_active}},
    )
    await db.commit()
    return admin


@router.post("/admin/users/{admin_id}/reset-password", response_model=AdminUserCreated)
async def reset_admin_password(admin_id: int, principal: ManagePrincipal, db: AsyncSession = Depends(get_db)) -> AdminUserCreated:
    """Issue a new one-time password (forces a change at next sign-in), revoke sessions and clear any lockout."""
    admin = await _get_or_404(db, admin_id)
    temp = admin_panel_service.generate_temporary_password()
    admin.hashed_password = auth_service.hash_password(temp)
    admin.must_change_password = True
    admin.password_changed_at = admin_auth_service._now()
    admin.failed_login_count = 0
    admin.locked_until = None
    admin_auth_service.revoke_sessions(admin)
    admin_audit.record(db, principal, "admin_user.reset_password", target_type="admin_user", target_id=admin.id)
    await db.commit()
    return AdminUserCreated(admin=AdminUserOut.model_validate(admin), temporary_password=temp)


@router.post("/admin/users/{admin_id}/reset-2fa", response_model=OkOut)
async def reset_admin_2fa(admin_id: int, principal: ManagePrincipal, db: AsyncSession = Depends(get_db)) -> OkOut:
    """Remove an operator's 2FA (lost device) and revoke their sessions; they can re-enrol after signing in."""
    admin = await _get_or_404(db, admin_id)
    admin.totp_enabled = False
    admin.totp_secret_enc = None
    admin.last_totp_step = None
    admin_auth_service.revoke_sessions(admin)
    admin_audit.record(db, principal, "admin_user.reset_2fa", target_type="admin_user", target_id=admin.id)
    await db.commit()
    return OkOut(message="2FA removed.")


@router.post("/admin/users/{admin_id}/unlock", response_model=OkOut)
async def unlock_admin_user(admin_id: int, principal: ManagePrincipal, db: AsyncSession = Depends(get_db)) -> OkOut:
    """Clear a lockout caused by repeated failed sign-ins."""
    admin = await _get_or_404(db, admin_id)
    admin.failed_login_count = 0
    admin.locked_until = None
    admin_audit.record(db, principal, "admin_user.unlock", target_type="admin_user", target_id=admin.id)
    await db.commit()
    return OkOut(message="Unlocked.")


@router.get("/admin/audit-log", response_model=AuditLogPage)
async def audit_log(
    _: Annotated[AdminPrincipal, Depends(require_perm("audit.read"))],
    actor: str = Query("", max_length=254),
    action: str = Query("", max_length=100),
    client_id: Optional[int] = Query(None),
    success: Optional[bool] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Admin actions and sign-in attempts, newest first (filter by actor, action substring, client, outcome, time)."""
    return await admin_panel_service.list_audit_log(
        db, actor=actor, action=action, client_id=client_id, success=success, since=since, until=until,
        page=page, page_size=page_size,
    )
