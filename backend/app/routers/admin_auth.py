"""
Admin panel authentication (separate from the tenant /auth router).

  POST /admin/auth/login            email + password (+ TOTP code once enrolled) -> operator JWT
  GET  /admin/auth/me               the signed-in operator and their permissions
  POST /admin/auth/logout           revokes every token of this account (bumps token_version)
  POST /admin/auth/change-password  needs the current password; revokes other sessions; returns a fresh token
  POST /admin/auth/totp/setup       start 2FA enrolment (returns the secret / otpauth URI)
  POST /admin/auth/totp/enable      confirm with a current code -> 2FA on
  POST /admin/auth/totp/disable     password + current code -> 2FA off

Every sign-in outcome (success or failure) is written to admin_audit_log. Failure responses never reveal
whether the email exists.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.routers.admin_deps import client_ip, get_admin_principal_allow_pending, http_error
from app.schemas.admin_panel import (
    AdminLoginRequest,
    AdminMeOut,
    AdminTokenOut,
    ChangePasswordRequest,
    OkOut,
    TotpConfirmRequest,
    TotpDisableRequest,
    TotpSetupOut,
)
from app.services import admin_audit, admin_auth_service, auth_service
from app.services.admin_audit import AdminPrincipal
from app.services.admin_auth_service import AdminAuthError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/auth", tags=["admin-auth"])


def me_out(principal: AdminPrincipal) -> AdminMeOut:
    """Profile payload for the panel (works for the legacy-key pseudo-operator too)."""
    admin = principal.admin
    return AdminMeOut(
        id=admin.id if admin else None,
        email=principal.actor,
        name=admin.name if admin else "",
        role=admin.role if admin else "superadmin",
        permissions=sorted(principal.permissions),
        totp_enabled=bool(admin.totp_enabled) if admin else False,
        must_change_password=bool(admin.must_change_password) if admin else False,
        last_login_at=admin.last_login_at if admin else None,
        via=principal.via,
    )


def _token_out(admin) -> AdminTokenOut:
    """Issue a token for *admin* and wrap it with the profile."""
    token = admin_auth_service.create_admin_token(admin)
    principal = AdminPrincipal(actor=admin.email, permissions=admin.permissions, admin=admin)
    return AdminTokenOut(
        access_token=token, expires_in=get_settings().admin_token_expire_minutes * 60, admin=me_out(principal)
    )


def _require_operator(principal: AdminPrincipal) -> None:
    """Self-service account routes only make sense for a real operator, not the shared key."""
    if principal.admin is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "operator_required", "message": "This action needs a signed-in operator account."},
        )


@router.post("/login", response_model=AdminTokenOut)
async def login(body: AdminLoginRequest, request: Request, db: AsyncSession = Depends(get_db)) -> AdminTokenOut:
    """Authenticate an operator. 401 `otp_required` means: resend with the 6-digit code."""
    ip = client_ip(request)
    email = admin_auth_service.normalize_email(body.email)
    try:
        admin, _ = await admin_auth_service.authenticate(db, email=email, password=body.password, otp=body.otp, ip=ip)
    except ProgrammingError as exc:
        # The usual cause is a database that hasn't had migration 0066 applied; say so instead of a bare 500.
        logger.exception("Admin sign-in failed with a database error")
        await db.rollback()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {
                "code": "admin_not_initialised",
                "message": "The admin tables are missing in this database. Run `alembic upgrade head`, then create an "
                "operator with `python scripts/create_admin.py`.",
            },
        ) from exc
    except AdminAuthError as exc:
        # Persist lockout counters set during the failed attempt, then audit in a separate session.
        await db.commit()
        if exc.code != "otp_required":
            await admin_audit.record_standalone(
                action="auth.login_failed", actor=email or "unknown", ip=ip, success=False, detail={"code": exc.code}
            )
        raise http_error(exc) from exc
    admin_audit.record(
        db, AdminPrincipal(actor=admin.email, permissions=admin.permissions, ip=ip, admin=admin), "auth.login",
        target_type="admin_user", target_id=admin.id,
    )
    await db.commit()
    return _token_out(admin)


@router.get("/me", response_model=AdminMeOut)
async def me(principal: Annotated[AdminPrincipal, Depends(get_admin_principal_allow_pending)]) -> AdminMeOut:
    """The signed-in operator, their role's permissions, and whether a password change is pending."""
    return me_out(principal)


@router.post("/logout", response_model=OkOut)
async def logout(
    principal: Annotated[AdminPrincipal, Depends(get_admin_principal_allow_pending)], db: AsyncSession = Depends(get_db)
) -> OkOut:
    """Sign out everywhere: bumping token_version invalidates every outstanding token for this account."""
    if principal.admin is not None:
        admin_auth_service.revoke_sessions(principal.admin)
        admin_audit.record(db, principal, "auth.logout", target_type="admin_user", target_id=principal.admin.id)
        await db.commit()
    return OkOut(message="Signed out.")


@router.post("/change-password", response_model=AdminTokenOut)
async def change_password(
    body: ChangePasswordRequest,
    principal: Annotated[AdminPrincipal, Depends(get_admin_principal_allow_pending)],
    db: AsyncSession = Depends(get_db),
) -> AdminTokenOut:
    """Change the operator's own password; all other sessions are revoked and a fresh token is returned."""
    _require_operator(principal)
    admin = principal.admin
    assert admin is not None
    if not auth_service.verify_password(body.current_password, admin.hashed_password):
        await admin_audit.record_standalone(
            action="auth.change_password_failed", actor=admin.email, ip=principal.ip, success=False
        )
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"code": "wrong_password", "message": "Current password is incorrect."})
    if body.new_password == body.current_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"code": "weak_password", "message": "Choose a different password."})
    try:
        admin_auth_service.validate_password_strength(body.new_password, admin.email)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"code": "weak_password", "message": str(exc)}) from exc
    admin.hashed_password = auth_service.hash_password(body.new_password)
    admin.must_change_password = False
    admin.password_changed_at = admin_auth_service._now()
    admin_auth_service.revoke_sessions(admin)
    admin_audit.record(db, principal, "auth.change_password", target_type="admin_user", target_id=admin.id)
    await db.commit()
    return _token_out(admin)


@router.post("/totp/setup", response_model=TotpSetupOut)
async def totp_setup(
    principal: Annotated[AdminPrincipal, Depends(get_admin_principal_allow_pending)], db: AsyncSession = Depends(get_db)
) -> TotpSetupOut:
    """Generate a pending TOTP secret (not active until /totp/enable confirms a code). Re-calling replaces it."""
    _require_operator(principal)
    admin = principal.admin
    assert admin is not None
    if admin.totp_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "totp_already_enabled", "message": "2FA is already enabled."})
    secret = admin_auth_service.generate_totp_secret()
    admin.totp_secret_enc = admin_auth_service.encrypt_secret(secret)
    admin.last_totp_step = None
    await db.commit()
    return TotpSetupOut(secret=secret, otpauth_uri=admin_auth_service.provisioning_uri(secret, admin.email))


@router.post("/totp/enable", response_model=OkOut)
async def totp_enable(
    body: TotpConfirmRequest,
    principal: Annotated[AdminPrincipal, Depends(get_admin_principal_allow_pending)],
    db: AsyncSession = Depends(get_db),
) -> OkOut:
    """Confirm enrolment with a current code. Other sessions are revoked; sign in again with a code next time."""
    _require_operator(principal)
    admin = principal.admin
    assert admin is not None
    if admin.totp_enabled or not admin.totp_secret_enc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "totp_not_pending", "message": "Start 2FA setup first."})
    step = admin_auth_service.verify_totp(admin_auth_service.decrypt_secret(admin.totp_secret_enc), body.code)
    if step is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"code": "invalid_otp", "message": "Invalid authentication code."})
    admin.totp_enabled = True
    admin.last_totp_step = step
    admin_auth_service.revoke_sessions(admin)
    admin_audit.record(db, principal, "auth.totp_enabled", target_type="admin_user", target_id=admin.id)
    await db.commit()
    return OkOut(message="2FA enabled. Please sign in again.")


@router.post("/totp/disable", response_model=OkOut)
async def totp_disable(
    body: TotpDisableRequest,
    principal: Annotated[AdminPrincipal, Depends(get_admin_principal_allow_pending)],
    db: AsyncSession = Depends(get_db),
) -> OkOut:
    """Turn 2FA off (needs the password and a current code)."""
    _require_operator(principal)
    admin = principal.admin
    assert admin is not None
    if not admin.totp_enabled or not admin.totp_secret_enc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "totp_not_enabled", "message": "2FA is not enabled."})
    if not auth_service.verify_password(body.password, admin.hashed_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"code": "wrong_password", "message": "Password is incorrect."})
    step = admin_auth_service.verify_totp(
        admin_auth_service.decrypt_secret(admin.totp_secret_enc), body.code, last_step=admin.last_totp_step
    )
    if step is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"code": "invalid_otp", "message": "Invalid authentication code."})
    admin.totp_enabled = False
    admin.totp_secret_enc = None
    admin.last_totp_step = None
    admin_auth_service.revoke_sessions(admin)
    admin_audit.record(db, principal, "auth.totp_disabled", target_type="admin_user", target_id=admin.id)
    await db.commit()
    return OkOut(message="2FA disabled. Please sign in again.")
