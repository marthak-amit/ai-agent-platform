"""
Admin (operator) authentication: passwords, TOTP 2FA, lockout, and JWTs.

Security model
  * Operators live in `admin_users`, never in `users`/`clients`. Their JWTs carry `aud="admin"`, which the
    tenant decoder (auth_service.get_current_user, no audience) rejects — and tenant tokens have no `aud`,
    so they fail here too. A leaked token of either kind cannot cross over.
  * Every token embeds `ver` (AdminUser.token_version). Bumping it (logout, password change, deactivation,
    2FA change) revokes every outstanding token for that account.
  * Wrong email and wrong password are indistinguishable (same message, a dummy bcrypt check for unknown
    emails). N consecutive failures lock the account for a cool-down; a per-IP in-process throttle slows
    spraying across accounts (single-instance, same caveat as the rate limiter).
  * TOTP (RFC 6238, SHA-1, 6 digits, 30 s, ±1 step) with a replay guard. Secrets are Fernet-encrypted at
    rest with a key derived from SECRET_KEY.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import struct
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.admin_user import ADMIN_ROLES, AdminUser
from app.services import auth_service

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
AUDIENCE = "admin"
MIN_PASSWORD_LENGTH = 12
_TOTP_STEP_SECONDS = 30
_TOTP_DIGITS = 6
_IP_WINDOW_SECONDS = 600
_IP_MAX_ATTEMPTS = 30
# Verified against when the email is unknown, so response time doesn't reveal which emails exist.
_DUMMY_HASH = auth_service.hash_password("not-a-real-password-" + secrets.token_hex(8))
_ip_attempts: dict[str, deque[float]] = defaultdict(deque)


class AdminAuthError(Exception):
    """An authentication / authorisation failure with an HTTP status and a stable machine-readable code."""

    def __init__(self, code: str, message: str, http_status: int = 401) -> None:
        """Store the code (e.g. "invalid_credentials", "otp_required"), human message and HTTP status."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _password_ok(plain: str, hashed: str) -> bool:
    """bcrypt check that treats a malformed stored hash (e.g. a manually inserted row) as a mismatch, not a crash."""
    try:
        return auth_service.verify_password(plain, hashed)
    except ValueError:
        logger.warning("Admin account has a malformed password hash; treating as wrong password.")
        return False


def _now() -> datetime:
    """Current UTC time (a function so tests can patch it)."""
    return datetime.now(timezone.utc)


# ── passwords ────────────────────────────────────────────────────────────────

def normalize_email(email: str) -> str:
    """Lower-case and trim an email for storage / lookup."""
    return (email or "").strip().lower()


def validate_password_strength(password: str, email: str = "") -> None:
    """
    Enforce the operator password policy.

    Raises:
        ValueError: with a user-facing message when the password is too short, lacks a letter or digit,
            contains the email's local part, or is a trivially repeated character.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > 128:
        raise ValueError("Password must be at most 128 characters.")
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise ValueError("Password must contain at least one letter and one digit.")
    if len(set(password)) < 5:
        raise ValueError("Password is too repetitive.")
    local = normalize_email(email).split("@")[0]
    if len(local) >= 4 and local in password.lower():
        raise ValueError("Password must not contain your email name.")


async def create_admin(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    name: str = "",
    role: str = "viewer",
    created_by: str = "",
    must_change_password: bool = False,
) -> AdminUser:
    """
    Insert a new operator (does not commit).

    Raises:
        ValueError: invalid role/email, weak password, or the email already exists.
    """
    email = normalize_email(email)
    if "@" not in email or len(email) > 254:
        raise ValueError("A valid email is required.")
    if role not in ADMIN_ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ADMIN_ROLES)}.")
    validate_password_strength(password, email)
    if (await db.execute(select(AdminUser.id).where(AdminUser.email == email))).first() is not None:
        raise ValueError("An admin with this email already exists.")
    admin = AdminUser(
        email=email,
        name=(name or "").strip()[:100],
        hashed_password=auth_service.hash_password(password),
        role=role,
        is_active=True,
        created_by=created_by or None,
        must_change_password=must_change_password,
        password_changed_at=_now(),
        token_version=0,
        failed_login_count=0,
        totp_enabled=False,
    )
    db.add(admin)
    await db.flush()
    return admin


# ── TOTP ─────────────────────────────────────────────────────────────────────

def _fernet() -> Fernet:
    """Fernet keyed from SECRET_KEY (stable across restarts; rotating SECRET_KEY invalidates enrolled 2FA)."""
    digest = hashlib.sha256(("admin-totp:" + get_settings().secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def generate_totp_secret() -> str:
    """A fresh 160-bit base32 TOTP secret."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def encrypt_secret(secret: str) -> str:
    """Encrypt a TOTP secret for storage."""
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(token: str) -> str:
    """Decrypt a stored TOTP secret; raises ValueError when it can't be decrypted (e.g. SECRET_KEY rotated)."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Stored 2FA secret can't be decrypted.") from exc


def totp_code(secret: str, step: int) -> str:
    """The 6-digit code for a given 30-second step counter (RFC 4226 HOTP over the step)."""
    padded = secret + "=" * (-len(secret) % 8)
    key = base64.b32decode(padded.upper())
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = (struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**_TOTP_DIGITS)
    return str(value).zfill(_TOTP_DIGITS)


def verify_totp(secret: str, code: str, *, last_step: Optional[int] = None, at: Optional[float] = None) -> Optional[int]:
    """
    Check *code* against the current step ±1. Returns the matched step (store it as last_totp_step), or None.

    A step <= last_step is rejected so a captured code can't be replayed.
    """
    code = (code or "").strip().replace(" ", "")
    if len(code) != _TOTP_DIGITS or not code.isdigit():
        return None
    current = int((at if at is not None else time.time()) // _TOTP_STEP_SECONDS)
    matched: Optional[int] = None
    for step in (current - 1, current, current + 1):
        if hmac.compare_digest(totp_code(secret, step), code):
            matched = step
    if matched is None or (last_step is not None and matched <= last_step):
        return None
    return matched


def provisioning_uri(secret: str, email: str) -> str:
    """otpauth:// URI that authenticator apps accept (rendered as a QR code by the panel)."""
    issuer = get_settings().admin_totp_issuer
    label = quote(f"{issuer}:{email}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&digits={_TOTP_DIGITS}&period={_TOTP_STEP_SECONDS}"


# ── tokens ───────────────────────────────────────────────────────────────────

def create_admin_token(admin: AdminUser, *, expires_delta: Optional[timedelta] = None) -> str:
    """Signed operator JWT (audience "admin", carries the account's token_version)."""
    settings = get_settings()
    expire = _now() + (expires_delta or timedelta(minutes=settings.admin_token_expire_minutes))
    claims = {"sub": f"admin:{admin.id}", "aud": AUDIENCE, "ver": admin.token_version, "exp": expire}
    return jwt.encode(claims, settings.secret_key, algorithm=ALGORITHM)


async def get_admin_from_token(token: str, db: AsyncSession) -> AdminUser:
    """
    Resolve an operator JWT to its AdminUser.

    Raises:
        AdminAuthError: invalid/expired token, wrong audience, unknown/inactive account, or revoked version.
    """
    try:
        payload = jwt.decode(token, get_settings().secret_key, algorithms=[ALGORITHM], audience=AUDIENCE)
    except JWTError as exc:
        raise AdminAuthError("invalid_token", "Invalid or expired admin session.") from exc
    sub = str(payload.get("sub", ""))
    if not sub.startswith("admin:") or not sub[6:].isdigit():
        raise AdminAuthError("invalid_token", "Invalid or expired admin session.")
    admin = await db.get(AdminUser, int(sub[6:]))
    if admin is None or not admin.is_active or payload.get("ver") != admin.token_version:
        raise AdminAuthError("invalid_token", "Invalid or expired admin session.")
    return admin


# ── login ────────────────────────────────────────────────────────────────────

def _throttle_ip(ip: str) -> None:
    """Reject when one IP has made too many login attempts recently."""
    now = time.monotonic()
    window = _ip_attempts[ip]
    while window and window[0] < now - _IP_WINDOW_SECONDS:
        window.popleft()
    if len(window) >= _IP_MAX_ATTEMPTS:
        raise AdminAuthError("too_many_attempts", "Too many sign-in attempts. Try again later.", 429)
    window.append(now)


def reset_throttle() -> None:
    """Clear the per-IP throttle (tests)."""
    _ip_attempts.clear()


async def authenticate(
    db: AsyncSession, *, email: str, password: str, otp: Optional[str], ip: str
) -> tuple[AdminUser, Optional[str]]:
    """
    Verify credentials (+ TOTP when enrolled) and return (admin, failure_reason_for_audit).

    On success the failure counter is cleared, last-login stamped, and the second element is None.
    The caller commits. On failure raises AdminAuthError after updating the lockout counters (the caller
    must still commit those — use `authenticate_and_commit`'s pattern: catch, commit, re-raise).

    Raises:
        AdminAuthError: invalid_credentials (401), account_locked (423), otp_required (401), invalid_otp (401),
            too_many_attempts (429).
    """
    settings = get_settings()
    _throttle_ip(ip)
    admin = (await db.execute(select(AdminUser).where(AdminUser.email == normalize_email(email)))).scalar_one_or_none()
    now = _now()

    if admin is None:
        _password_ok(password, _DUMMY_HASH)
        raise AdminAuthError("invalid_credentials", "Invalid email or password.")

    if admin.locked_until is not None and admin.locked_until > now:
        raise AdminAuthError("account_locked", "Account temporarily locked after repeated failures. Try again later.", 423)

    password_ok = _password_ok(password, admin.hashed_password)
    if not password_ok or not admin.is_active:
        if not password_ok:
            admin.failed_login_count += 1
            if admin.failed_login_count >= settings.admin_max_failed_logins:
                admin.locked_until = now + timedelta(minutes=settings.admin_lockout_minutes)
                admin.failed_login_count = 0
        raise AdminAuthError("invalid_credentials", "Invalid email or password.")

    if admin.totp_enabled:
        if not otp:
            raise AdminAuthError("otp_required", "Enter the 6-digit code from your authenticator app.")
        try:
            secret = decrypt_secret(admin.totp_secret_enc or "")
        except ValueError as exc:
            raise AdminAuthError("invalid_otp", "2FA is misconfigured for this account; ask a superadmin to reset it.") from exc
        step = verify_totp(secret, otp, last_step=admin.last_totp_step)
        if step is None:
            admin.failed_login_count += 1
            if admin.failed_login_count >= settings.admin_max_failed_logins:
                admin.locked_until = now + timedelta(minutes=settings.admin_lockout_minutes)
                admin.failed_login_count = 0
            raise AdminAuthError("invalid_otp", "Invalid authentication code.")
        admin.last_totp_step = step

    admin.failed_login_count = 0
    admin.locked_until = None
    admin.last_login_at = now
    admin.last_login_ip = ip
    return admin, None


def revoke_sessions(admin: AdminUser) -> None:
    """Invalidate every token issued to this account (bump token_version)."""
    admin.token_version += 1
