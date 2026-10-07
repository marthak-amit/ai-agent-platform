"""
Shared FastAPI dependencies for every /admin route: who is calling and what they may do.

Two credentials are accepted:
  * `Authorization: Bearer <operator JWT>` — a signed-in AdminUser; permissions come from their role.
  * `X-Admin-Key: <ADMIN_SECRET_KEY>` — the legacy shared key (scripts / break-glass). It carries every
    permission, is attributed to "admin-key" (or the optional X-Admin-User header) in the audit log, can be
    switched off with ADMIN_API_KEY_ENABLED=false, and is ignored outside development while it is still the
    shipped default value.

`require_admin` keeps its historic meaning ("any authenticated admin") for routers that predate RBAC;
`require_perm("billing.write")` is the per-route check the panel endpoints use.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
from typing import Annotated, Callable, Optional

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models.admin_user import ADMIN_PERMISSIONS
from app.services import admin_audit, admin_auth_service
from app.services.admin_audit import AdminPrincipal
from app.services.admin_auth_service import AdminAuthError

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)
_DEFAULT_ADMIN_KEY = "change-me-admin-secret"
_ALL_PERMISSIONS = frozenset().union(*ADMIN_PERMISSIONS.values())
_warned_default_key = False


def client_ip(request: Request) -> str:
    """
    Caller IP for the login throttle and audit rows.

    Behind Railway's proxy `request.client.host` is the proxy, so with ADMIN_TRUST_PROXY_HEADERS (default on)
    the RIGHT-most X-Forwarded-For entry is used: that is the hop our own proxy appended, which a client
    cannot forge (they can only prepend). Malformed values fall back to the socket peer.
    """
    peer = request.client.host if request.client else "unknown"
    if not get_settings().admin_trust_proxy_headers:
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    last = forwarded.split(",")[-1].strip() if forwarded else ""
    try:
        return str(ipaddress.ip_address(last)) if last else peer
    except ValueError:
        return peer


def http_error(exc: AdminAuthError) -> HTTPException:
    """AdminAuthError -> HTTPException with a {code, message} body (the panel switches on `code`)."""
    headers = {"WWW-Authenticate": "Bearer"} if exc.http_status == status.HTTP_401_UNAUTHORIZED else None
    return HTTPException(exc.http_status, {"code": exc.code, "message": exc.message}, headers=headers)


def _legacy_key_valid(x_admin_key: Optional[str]) -> bool:
    """Constant-time check of X-Admin-Key, honouring ADMIN_API_KEY_ENABLED and the default-key guard."""
    global _warned_default_key
    settings = get_settings()
    if not x_admin_key or not settings.admin_api_key_enabled:
        return False
    if settings.environment.strip().lower() != "development" and settings.admin_secret_key == _DEFAULT_ADMIN_KEY:
        if not _warned_default_key:
            logger.error("ADMIN_SECRET_KEY is still the default value: X-Admin-Key auth is DISABLED. Set a real key.")
            _warned_default_key = True
        return False
    return secrets.compare_digest(x_admin_key.encode("utf-8"), settings.admin_secret_key.encode("utf-8"))


async def _resolve_principal(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials],
    x_admin_key: Optional[str],
    x_admin_user: Optional[str],
    db: AsyncSession,
    *, allow_password_change_pending: bool,
) -> AdminPrincipal:
    """Authenticate the request as an operator JWT or the legacy key; 401 otherwise."""
    ip = client_ip(request)
    if credentials is not None and credentials.credentials:
        try:
            admin = await admin_auth_service.get_admin_from_token(credentials.credentials, db)
        except AdminAuthError as exc:
            raise http_error(exc) from exc
        if admin.must_change_password and not allow_password_change_pending:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                {"code": "password_change_required", "message": "You must change your password before continuing."},
            )
        return AdminPrincipal(actor=admin.email, permissions=admin.permissions, ip=ip, admin=admin, via="jwt")
    if _legacy_key_valid(x_admin_key):
        actor = (x_admin_user or "").strip()[:100] or "admin-key"
        return AdminPrincipal(actor=actor, permissions=_ALL_PERMISSIONS, ip=ip, via="key")
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        {"code": "not_authenticated", "message": "Invalid or missing admin credentials."},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_admin_principal(
    request: Request,
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(_bearer)],
    db: AsyncSession = Depends(get_db),
    x_admin_key: Annotated[Optional[str], Header()] = None,
    x_admin_user: Annotated[Optional[str], Header()] = None,
) -> AdminPrincipal:
    """Authenticated caller (operator JWT or legacy key). Blocks accounts that must change their password."""
    return await _resolve_principal(
        request, credentials, x_admin_key, x_admin_user, db, allow_password_change_pending=False
    )


async def get_admin_principal_allow_pending(
    request: Request,
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(_bearer)],
    db: AsyncSession = Depends(get_db),
    x_admin_key: Annotated[Optional[str], Header()] = None,
    x_admin_user: Annotated[Optional[str], Header()] = None,
) -> AdminPrincipal:
    """Like get_admin_principal, but lets an account with must_change_password through (for /auth/me and change-password)."""
    return await _resolve_principal(
        request, credentials, x_admin_key, x_admin_user, db, allow_password_change_pending=True
    )


async def require_admin(principal: Annotated[AdminPrincipal, Depends(get_admin_principal)]) -> AdminPrincipal:
    """Any authenticated admin (operator or legacy key). Prefer `require_perm` for new routes."""
    return principal


def require_perm(*permissions: str) -> Callable[..., AdminPrincipal]:
    """
    Dependency factory: the caller must hold EVERY listed permission.

    A denied attempt is recorded in the audit log (own session) and answered with 403.
    """

    async def _check(principal: Annotated[AdminPrincipal, Depends(get_admin_principal)]) -> AdminPrincipal:
        missing = [p for p in permissions if not principal.can(p)]
        if missing:
            await admin_audit.record_standalone(
                action="permission_denied", actor=principal.actor, ip=principal.ip, success=False,
                detail={"required": list(permissions)},
            )
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                {"code": "forbidden", "message": f"Your role does not allow this action ({', '.join(missing)})."},
            )
        return principal

    return _check
