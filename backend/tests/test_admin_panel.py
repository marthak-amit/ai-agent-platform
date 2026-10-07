"""
Unit tests for the admin panel security layer (no database): password policy, TOTP, tokens, lockout,
RBAC, audit redaction, and the "every /admin route is authenticated" guard.

DB-backed behaviour (migrations, queries, audit rows) is covered by tests/replay/test_admin_panel_e2e.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.admin_user import ADMIN_PERMISSIONS, ADMIN_ROLES, AdminUser, permissions_for_role
from app.services import admin_audit, admin_auth_service, admin_panel_service, auth_service
from app.services.admin_auth_service import AdminAuthError

KEY_HEADERS = {"X-Admin-Key": "test-admin-key"}


_PASSWORD_HASH = auth_service.hash_password("Correct-horse-42")  # bcrypt is slow: hash once for the module


def _admin(**kw) -> AdminUser:
    """An in-memory operator with a real bcrypt hash of 'Correct-horse-42'."""
    defaults = dict(
        id=7, email="ops@example.com", name="Ops", role="viewer", is_active=True, totp_enabled=False,
        failed_login_count=0, locked_until=None, token_version=0, must_change_password=False,
        hashed_password=_PASSWORD_HASH,
    )
    defaults.update(kw)
    return AdminUser(**defaults)


def _bearer(admin: AdminUser) -> dict:
    """Authorization header carrying a real operator JWT."""
    return {"Authorization": f"Bearer {admin_auth_service.create_admin_token(admin)}"}


def _db_returning(admin: AdminUser | None) -> AsyncMock:
    """A mock session whose email lookup and primary-key get both yield *admin*."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = admin
    db.execute = AsyncMock(return_value=result)
    db.get = AsyncMock(return_value=admin)
    db.commit = AsyncMock()
    db.add = MagicMock()
    return db


# ── password policy ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "password,email",
    [
        ("short1A", ""),
        ("alllettersnodigits", ""),
        ("1234567890123", ""),
        ("aaaaaaaaaaaa1", ""),
        ("opsteam-2026-xyz", "opsteam@example.com"),
    ],
)
def test_validate_password_strength_rejects_weak(password, email):
    """Too short, missing a class, repetitive, or containing the email name are all refused."""
    with pytest.raises(ValueError):
        admin_auth_service.validate_password_strength(password, email)


def test_validate_password_strength_accepts_good():
    """A long mixed password passes."""
    admin_auth_service.validate_password_strength("Correct-horse-42", "someone@example.com")


def test_generate_temporary_password_satisfies_policy():
    """Generated one-time passwords always pass the policy."""
    for _ in range(20):
        admin_auth_service.validate_password_strength(admin_panel_service.generate_temporary_password())


# ── TOTP ─────────────────────────────────────────────────────────────────────

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32("12345678901234567890"), RFC 6238 appendix B


def test_totp_matches_rfc6238_vector():
    """At T=59s the SHA-1 reference code is ...287082 (last 6 digits of 94287082)."""
    assert admin_auth_service.totp_code(RFC_SECRET, 59 // 30) == "287082"


def test_verify_totp_window_and_replay_guard():
    """±1 step is accepted, a stale/replayed step is not, garbage is not."""
    now = 1_700_000_000
    step = now // 30
    code = admin_auth_service.totp_code(RFC_SECRET, step)
    assert admin_auth_service.verify_totp(RFC_SECRET, code, at=now) == step
    assert admin_auth_service.verify_totp(RFC_SECRET, code, at=now + 30) == step  # one step late
    assert admin_auth_service.verify_totp(RFC_SECRET, code, at=now + 120) is None  # outside window
    assert admin_auth_service.verify_totp(RFC_SECRET, code, last_step=step, at=now) is None  # replay
    assert admin_auth_service.verify_totp(RFC_SECRET, "abcdef", at=now) is None
    assert admin_auth_service.verify_totp(RFC_SECRET, "", at=now) is None


def test_totp_secret_encryption_roundtrip(mock_settings):
    """Secrets are stored encrypted and decrypt back; the ciphertext doesn't contain the secret."""
    secret = admin_auth_service.generate_totp_secret()
    enc = admin_auth_service.encrypt_secret(secret)
    assert secret not in enc
    assert admin_auth_service.decrypt_secret(enc) == secret
    with pytest.raises(ValueError):
        admin_auth_service.decrypt_secret("not-a-token")


def test_provisioning_uri_shape(mock_settings):
    """The otpauth URI names the issuer and carries the secret."""
    uri = admin_auth_service.provisioning_uri("ABCDEF", "ops@example.com")
    assert uri.startswith("otpauth://totp/") and "secret=ABCDEF" in uri and "issuer=" in uri


# ── tokens ───────────────────────────────────────────────────────────────────

async def test_admin_token_roundtrip(mock_settings):
    """A freshly issued token resolves to its account."""
    admin = _admin()
    token = admin_auth_service.create_admin_token(admin)
    got = await admin_auth_service.get_admin_from_token(token, _db_returning(admin))
    assert got.id == admin.id


async def test_admin_token_revoked_by_version_bump_or_deactivation(mock_settings):
    """Bumping token_version or deactivating the account invalidates outstanding tokens."""
    admin = _admin()
    token = admin_auth_service.create_admin_token(admin)
    admin_auth_service.revoke_sessions(admin)
    with pytest.raises(AdminAuthError):
        await admin_auth_service.get_admin_from_token(token, _db_returning(admin))
    fresh = _admin()
    token2 = admin_auth_service.create_admin_token(fresh)
    fresh.is_active = False
    with pytest.raises(AdminAuthError):
        await admin_auth_service.get_admin_from_token(token2, _db_returning(fresh))


async def test_admin_token_expired_rejected(mock_settings):
    """An expired token is refused."""
    admin = _admin()
    token = admin_auth_service.create_admin_token(admin, expires_delta=timedelta(seconds=-5))
    with pytest.raises(AdminAuthError):
        await admin_auth_service.get_admin_from_token(token, _db_returning(admin))


async def test_tenant_and_admin_tokens_do_not_cross(mock_settings):
    """A tenant JWT is refused by the admin decoder, and an admin JWT by the tenant decoder."""
    admin = _admin()
    tenant_token = auth_service.create_access_token({"sub": "owner@biz.com"})
    with pytest.raises(AdminAuthError):
        await admin_auth_service.get_admin_from_token(tenant_token, _db_returning(admin))
    admin_token = admin_auth_service.create_admin_token(admin)
    with pytest.raises(ValueError):
        await auth_service.get_current_user(admin_token, AsyncMock())


# ── authenticate / lockout ───────────────────────────────────────────────────

async def test_authenticate_success_resets_counters(mock_settings):
    """Good credentials clear failures and stamp last login."""
    admin = _admin(failed_login_count=3)
    got, _ = await admin_auth_service.authenticate(
        _db_returning(admin), email="OPS@example.com", password="Correct-horse-42", otp=None, ip="1.2.3.4"
    )
    assert got is admin and admin.failed_login_count == 0 and admin.last_login_ip == "1.2.3.4"


async def test_authenticate_unknown_email_is_indistinguishable(mock_settings):
    """Unknown email and wrong password produce the same code and message."""
    with pytest.raises(AdminAuthError) as unknown:
        await admin_auth_service.authenticate(_db_returning(None), email="x@y.com", password="whatever-1234", otp=None, ip="ip-a")
    with pytest.raises(AdminAuthError) as wrong:
        await admin_auth_service.authenticate(_db_returning(_admin()), email="ops@example.com", password="nope", otp=None, ip="ip-b")
    assert (unknown.value.code, unknown.value.message) == (wrong.value.code, wrong.value.message)


async def test_authenticate_locks_after_repeated_failures(mock_settings):
    """The Nth consecutive failure locks the account; even the right password is then refused (423)."""
    admin = _admin()
    db = _db_returning(admin)
    for _ in range(mock_settings.admin_max_failed_logins):
        with pytest.raises(AdminAuthError):
            await admin_auth_service.authenticate(db, email=admin.email, password="bad", otp=None, ip="ip-lock")
    assert admin.locked_until is not None and admin.locked_until > datetime.now(timezone.utc)
    with pytest.raises(AdminAuthError) as locked:
        await admin_auth_service.authenticate(db, email=admin.email, password="Correct-horse-42", otp=None, ip="ip-lock")
    assert locked.value.code == "account_locked" and locked.value.http_status == 423


async def test_authenticate_requires_valid_otp_when_enrolled(mock_settings):
    """With 2FA on: no code -> otp_required, wrong code -> invalid_otp, right code -> ok (and replay refused)."""
    secret = admin_auth_service.generate_totp_secret()
    admin = _admin(totp_enabled=True, totp_secret_enc=admin_auth_service.encrypt_secret(secret))
    db = _db_returning(admin)
    with pytest.raises(AdminAuthError) as need:
        await admin_auth_service.authenticate(db, email=admin.email, password="Correct-horse-42", otp=None, ip="ip-otp")
    assert need.value.code == "otp_required"
    with pytest.raises(AdminAuthError) as bad:
        await admin_auth_service.authenticate(db, email=admin.email, password="Correct-horse-42", otp="000000", ip="ip-otp")
    assert bad.value.code == "invalid_otp"
    import time

    code = admin_auth_service.totp_code(secret, int(time.time() // 30))
    await admin_auth_service.authenticate(db, email=admin.email, password="Correct-horse-42", otp=code, ip="ip-otp")
    with pytest.raises(AdminAuthError):  # same code again = replay
        await admin_auth_service.authenticate(db, email=admin.email, password="Correct-horse-42", otp=code, ip="ip-otp")


async def test_ip_throttle(mock_settings):
    """One IP hammering the login gets 429 before any DB work."""
    admin_auth_service.reset_throttle()
    db = _db_returning(None)
    code = None
    for _ in range(admin_auth_service._IP_MAX_ATTEMPTS + 1):
        try:
            await admin_auth_service.authenticate(db, email="a@b.com", password="x", otp=None, ip="9.9.9.9")
        except AdminAuthError as exc:
            code = exc.code
    assert code == "too_many_attempts"
    admin_auth_service.reset_throttle()


# ── RBAC ─────────────────────────────────────────────────────────────────────

def test_role_permissions():
    """superadmin ⊇ every other role; viewer has no write; unknown roles get nothing."""
    sup = permissions_for_role("superadmin")
    for role in ADMIN_ROLES:
        assert permissions_for_role(role) <= sup
    viewer = permissions_for_role("viewer")
    assert not [p for p in viewer if p.endswith(".write") or p in ("clients.impersonate", "admins.manage", "audit.read")]
    assert permissions_for_role("hacker") == frozenset()
    assert "admins.manage" not in permissions_for_role("support") and "audit.read" not in permissions_for_role("billing")
    assert set(ADMIN_PERMISSIONS) == set(ADMIN_ROLES)


def test_sanitize_detail_redacts_secrets_and_truncates():
    """Secret-looking keys never reach the audit log; long strings are cut."""
    out = admin_audit.sanitize_detail({"password": "x", "api_key": "y", "nested": {"access_token": "z", "ok": 1}, "long": "a" * 5000})
    assert out["password"] == "[redacted]" and out["api_key"] == "[redacted]"
    assert out["nested"]["access_token"] == "[redacted]" and out["nested"]["ok"] == 1
    assert len(out["long"]) == 500


# ── HTTP-level auth guard ────────────────────────────────────────────────────

def _admin_routes(client) -> list[tuple[str, str]]:
    """Every (method, path-with-ids-filled) under /admin from the OpenAPI document."""
    routes = []
    for path, ops in client.get("/openapi.json").json()["paths"].items():
        if not path.startswith("/admin"):
            continue
        filled = path
        for part in [p for p in path.split("/") if p.startswith("{")]:
            filled = filled.replace(part, "1")
        routes += [(m.upper(), filled) for m in ops]
    return routes


def test_every_admin_route_rejects_unauthenticated_requests(client):
    """Nothing under /admin answers without credentials, except the login itself."""
    routes = [(m, p) for m, p in _admin_routes(client) if p != "/admin/auth/login"]
    assert len(routes) > 30  # sanity: the document really lists the admin surface
    for method, path in routes:
        resp = client.request(method, path)
        assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"


def test_tenant_token_cannot_reach_admin_routes(client):
    """A valid tenant JWT is not an admin credential."""
    tenant = auth_service.create_access_token({"sub": "owner@biz.com"})
    resp = client.get("/admin/panel/overview", headers={"Authorization": f"Bearer {tenant}"})
    assert resp.status_code == 401


def test_viewer_cannot_mutate_but_can_read(client, mock_db):
    """A viewer operator gets 403 on writes (and the denial is audited) but 200 on permitted reads."""
    admin = _admin(role="viewer")
    mock_db.get = AsyncMock(return_value=admin)
    with patch("app.services.admin_audit.record_standalone", new=AsyncMock()) as denied:
        for method, path in [
            ("PUT", "/admin/clients/1/suspend"),
            ("POST", "/admin/billing/subscriptions/1/revoke"),
            ("GET", "/admin/users"),
            ("GET", "/admin/audit-log"),
            ("POST", "/admin/panel/clients/1/impersonate"),
        ]:
            resp = client.request(method, path, headers=_bearer(admin), json={"reason": "valid reason", "days": 1})
            assert resp.status_code == 403, f"{method} {path} -> {resp.status_code} {resp.text}"
        assert denied.await_count == 5
    with patch("app.services.admin_panel_service.get_overview", new=AsyncMock(return_value={
        "clients": {}, "revenue": {}, "messages": {}, "llm_30d": {}, "attention": {}, "signups_14d": [], "llm_daily_14d": [],
    })):
        assert client.get("/admin/panel/overview", headers=_bearer(admin)).status_code == 200


def test_legacy_key_still_authenticates(client):
    """The shared X-Admin-Key keeps working for scripts (full permissions)."""
    with patch("app.services.admin_panel_service.get_overview", new=AsyncMock(return_value={
        "clients": {}, "revenue": {}, "messages": {}, "llm_30d": {}, "attention": {}, "signups_14d": [], "llm_daily_14d": [],
    })):
        assert client.get("/admin/panel/overview", headers=KEY_HEADERS).status_code == 200


def test_legacy_key_disabled_by_flag(client, mock_settings):
    """ADMIN_API_KEY_ENABLED=false switches the shared key off."""
    mock_settings.admin_api_key_enabled = False
    assert client.get("/admin/panel/overview", headers=KEY_HEADERS).status_code == 401


def test_default_admin_key_refused_outside_development(client, mock_settings):
    """The shipped default key is never accepted in a non-development environment."""
    mock_settings.admin_secret_key = "change-me-admin-secret"
    mock_settings.environment = "production"
    resp = client.get("/admin/panel/overview", headers={"X-Admin-Key": "change-me-admin-secret"})
    assert resp.status_code == 401


def test_must_change_password_blocks_everything_but_account_routes(client, mock_db):
    """An operator flagged must_change_password can reach /me but nothing else."""
    admin = _admin(role="superadmin", must_change_password=True)
    mock_db.get = AsyncMock(return_value=admin)
    blocked = client.get("/admin/users", headers=_bearer(admin))
    assert blocked.status_code == 403 and blocked.json()["detail"]["code"] == "password_change_required"
    me = client.get("/admin/auth/me", headers=_bearer(admin))
    assert me.status_code == 200 and me.json()["must_change_password"] is True


# ── login endpoint ───────────────────────────────────────────────────────────

def test_login_success_returns_token_and_profile(client, mock_db):
    """POST /admin/auth/login issues a token whose profile carries the role's permissions."""
    admin = _admin(role="support")
    result = MagicMock()
    result.scalar_one_or_none.return_value = admin
    mock_db.execute = AsyncMock(return_value=result)
    resp = client.post("/admin/auth/login", json={"email": "ops@example.com", "password": "Correct-horse-42"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer" and body["admin"]["role"] == "support"
    assert "clients.write" in body["admin"]["permissions"] and "admins.manage" not in body["admin"]["permissions"]


def test_login_failure_is_generic_and_audited(client, mock_db):
    """A wrong password gets a generic 401 and a standalone audit row."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = _admin()
    mock_db.execute = AsyncMock(return_value=result)
    with patch("app.services.admin_audit.record_standalone", new=AsyncMock()) as audit:
        resp = client.post("/admin/auth/login", json={"email": "ops@example.com", "password": "wrong-password-1"})
    assert resp.status_code == 401 and resp.json()["detail"]["code"] == "invalid_credentials"
    assert audit.await_args.kwargs["action"] == "auth.login_failed" and audit.await_args.kwargs["success"] is False


def test_login_prompts_for_otp_when_enrolled(client, mock_db):
    """With 2FA on and no code supplied the API answers 401 otp_required (so the UI can ask for it)."""
    admin = _admin(totp_enabled=True, totp_secret_enc=admin_auth_service.encrypt_secret(admin_auth_service.generate_totp_secret()))
    result = MagicMock()
    result.scalar_one_or_none.return_value = admin
    mock_db.execute = AsyncMock(return_value=result)
    with patch("app.services.admin_audit.record_standalone", new=AsyncMock()) as audit:
        resp = client.post("/admin/auth/login", json={"email": "ops@example.com", "password": "Correct-horse-42"})
    assert resp.status_code == 401 and resp.json()["detail"]["code"] == "otp_required"
    audit.assert_not_awaited()  # asking for the second factor is not a failure


# ── caller IP ────────────────────────────────────────────────────────────────

def _request(xff: str | None, peer: str = "10.0.0.1"):
    """A minimal stand-in for fastapi.Request with a peer address and optional X-Forwarded-For."""
    req = MagicMock()
    req.client.host = peer
    req.headers = {"x-forwarded-for": xff} if xff is not None else {}
    return req


def test_client_ip_uses_rightmost_forwarded_hop(mock_settings):
    """The hop our proxy appended (right-most) wins over client-supplied prefixes; junk falls back to the peer."""
    from app.routers.admin_deps import client_ip

    assert client_ip(_request("6.6.6.6, 203.0.113.9")) == "203.0.113.9"
    assert client_ip(_request(None)) == "10.0.0.1"
    assert client_ip(_request("not-an-ip")) == "10.0.0.1"
    mock_settings.admin_trust_proxy_headers = False
    assert client_ip(_request("203.0.113.9")) == "10.0.0.1"


# ── regressions: sign-in must never 500 on bad stored data / un-migrated DB ──

async def test_authenticate_malformed_hash_is_just_a_wrong_password(mock_settings):
    """A manually inserted row with a non-bcrypt 'hash' yields invalid_credentials, not a crash."""
    admin = _admin(hashed_password="admin123")
    with pytest.raises(AdminAuthError) as exc:
        await admin_auth_service.authenticate(_db_returning(admin), email=admin.email, password="admin123", otp=None, ip="ip-bad-hash")
    assert exc.value.code == "invalid_credentials"
    assert admin.failed_login_count == 1


def test_login_returns_actionable_503_when_admin_tables_are_missing(client, mock_db):
    """If migration 0066 hasn't been applied the API says so (503 admin_not_initialised) instead of a bare 500."""
    from sqlalchemy.exc import ProgrammingError

    mock_db.rollback = AsyncMock()
    boom = ProgrammingError("select ...", {}, Exception('relation "admin_users" does not exist'))
    with patch("app.services.admin_auth_service.authenticate", new=AsyncMock(side_effect=boom)):
        resp = client.post("/admin/auth/login", json={"email": "admin@test.com", "password": "admin123"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "admin_not_initialised" and "alembic upgrade head" in resp.json()["detail"]["message"]
