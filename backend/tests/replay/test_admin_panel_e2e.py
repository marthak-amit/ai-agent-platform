"""
Admin panel end-to-end: real Postgres, real app, real HTTP.

Covers sign-in (lockout, 2FA, forced password change, logout revocation), RBAC, the tenant directory and
detail views (and that no credentials leak), flag edits, conversation/order views, impersonation, billing-plan
edits, operator management guards, system health and — throughout — that the audit log records what happened.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.admin_user import AdminAuditLog, AdminUser
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.order import Order
from app.models.sellertalk24_billing import BillingPlan
from app.models.user import User
from app.services import admin_auth_service
from tests.replay.conftest import seed_client_and_product

pytestmark = pytest.mark.asyncio

PASSWORD = "Correct-horse-42"
ADMIN_KEY = "e2e-admin-key"


@pytest_asyncio.fixture(autouse=True)
async def _audit_sessions(replay_db_url, monkeypatch):
    """Pin the legacy admin key, point record_standalone's own session at the replay DB, reset the login throttle."""
    from app.config import get_settings

    pinned = get_settings().model_copy(update=dict(admin_secret_key=ADMIN_KEY, environment="development"))
    monkeypatch.setattr("app.routers.admin_deps.get_settings", lambda: pinned)
    engine = create_async_engine(replay_db_url, echo=False)
    monkeypatch.setattr("app.db._session_factory", async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False))
    admin_auth_service.reset_throttle()
    yield
    await engine.dispose()


async def make_admin(db, *, email: str, role: str, password: str = PASSWORD, must_change: bool = False) -> AdminUser:
    """Insert an operator directly (bootstrap path)."""
    admin = await admin_auth_service.create_admin(
        db, email=email, password=password, name=email.split("@")[0], role=role, must_change_password=must_change
    )
    await db.commit()
    return admin


async def login(http, email: str, password: str = PASSWORD, otp: str | None = None):
    """POST /admin/auth/login."""
    return await http.post("/admin/auth/login", json={"email": email, "password": password, "otp": otp})


async def auth(http, email: str, password: str = PASSWORD) -> dict:
    """Sign in and return bearer headers."""
    r = await login(http, email, password)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def audit_actions(db, action: str | None = None) -> list[AdminAuditLog]:
    """Fresh audit rows (optionally for one action), oldest first."""
    q = select(AdminAuditLog).order_by(AdminAuditLog.id)
    if action:
        q = q.where(AdminAuditLog.action == action)
    return list((await db.execute(q.execution_options(populate_existing=True))).scalars())


async def seed_tenant(db, n: int, *, with_secrets: bool = False):
    """A tenant with an owner login (and optionally channel/payment secrets that must never leak)."""
    client, product = await seed_client_and_product(
        db, phone=f"9177{n:07d}", wa_phone_number_id=f"ADM{n}", product_sku=f"ADM{n}", product_name=f"Item {n}"
    )
    client.business_name = f"Acme Store {n}"
    if with_secrets:
        client.whatsapp_access_token = "SECRET-WA-TOKEN"
        client.instagram_access_token = "SECRET-IG-TOKEN"
        client.razorpay_key_id = "rzp_live_x"
        client.razorpay_key_secret = "SECRET-RZP"
        client.bank_account_number = "SECRET-BANK-123"
    db.add(User(client_id=client.id, email=client.email, role="owner", permissions=[], is_active=True, hashed_password="x"))
    await db.commit()
    return client


# ── sign-in ───────────────────────────────────────────────────────────────────

async def test_login_me_logout_and_revocation(replay_http, replay_session):
    """Sign in, read the profile, sign out — and the old token is dead afterwards."""
    await make_admin(replay_session, email="root@ops.test", role="superadmin")
    h = await auth(replay_http, "ROOT@ops.test")  # email is case-insensitive
    me = (await replay_http.get("/admin/auth/me", headers=h)).json()
    assert me["email"] == "root@ops.test" and me["role"] == "superadmin" and "admins.manage" in me["permissions"]
    assert (await replay_http.post("/admin/auth/logout", headers=h)).status_code == 200
    assert (await replay_http.get("/admin/auth/me", headers=h)).status_code == 401
    assert [a.action for a in await audit_actions(replay_session)] == ["auth.login", "auth.logout"]


async def test_lockout_after_repeated_failures_is_audited(replay_http, replay_session):
    """Five bad passwords lock the account (423, even for the right password); failures land in the audit log."""
    await make_admin(replay_session, email="lock@ops.test", role="viewer")
    for _ in range(5):
        r = await login(replay_http, "lock@ops.test", "wrong-password-9")
        assert r.status_code == 401 and r.json()["detail"]["code"] == "invalid_credentials"
    r = await login(replay_http, "lock@ops.test")
    assert r.status_code == 423 and r.json()["detail"]["code"] == "account_locked"
    unknown = await login(replay_http, "ghost@ops.test", "whatever-123456")
    assert unknown.status_code == 401 and unknown.json()["detail"] == r.json()["detail"] | {"code": "invalid_credentials", "message": "Invalid email or password."}
    failures = await audit_actions(replay_session, "auth.login_failed")
    assert len(failures) >= 5 and all(not f.success for f in failures)
    assert {f.actor for f in failures} >= {"lock@ops.test", "ghost@ops.test"}


async def test_totp_enrolment_and_login(replay_http, replay_session):
    """Enrol 2FA, then login needs the code; the secret is never stored in clear."""
    await make_admin(replay_session, email="2fa@ops.test", role="superadmin")
    h = await auth(replay_http, "2fa@ops.test")
    setup = (await replay_http.post("/admin/auth/totp/setup", headers=h)).json()
    secret = setup["secret"]
    assert setup["otpauth_uri"].startswith("otpauth://totp/")
    row = (await replay_session.execute(select(AdminUser).where(AdminUser.email == "2fa@ops.test").execution_options(populate_existing=True))).scalar_one()
    assert secret not in (row.totp_secret_enc or "") and not row.totp_enabled
    step = int(time.time() // 30)
    bad = await replay_http.post("/admin/auth/totp/enable", headers=h, json={"code": "000000"})
    assert bad.status_code == 400
    ok = await replay_http.post("/admin/auth/totp/enable", headers=h, json={"code": admin_auth_service.totp_code(secret, step)})
    assert ok.status_code == 200
    assert (await replay_http.get("/admin/auth/me", headers=h)).status_code == 401  # sessions revoked on enrolment
    need = await login(replay_http, "2fa@ops.test")
    assert need.status_code == 401 and need.json()["detail"]["code"] == "otp_required"
    # the enrol code is now burnt (replay guard); use the next step's code, which the ±1 window accepts
    nxt = admin_auth_service.totp_code(secret, step + 1)
    good = await login(replay_http, "2fa@ops.test", otp=nxt)
    assert good.status_code == 200 and good.json()["admin"]["totp_enabled"] is True


async def test_forced_password_change_flow(replay_http, replay_session):
    """A new operator can only change their password until they do; the old password and token then stop working."""
    await make_admin(replay_session, email="new@ops.test", role="support", password="Temp-pass-12345", must_change=True)
    r = await login(replay_http, "new@ops.test", "Temp-pass-12345")
    assert r.status_code == 200 and r.json()["admin"]["must_change_password"] is True
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await replay_http.get("/admin/panel/overview", headers=h)).status_code == 403
    weak = await replay_http.post("/admin/auth/change-password", headers=h, json={"current_password": "Temp-pass-12345", "new_password": "short"})
    assert weak.status_code == 400 and weak.json()["detail"]["code"] == "weak_password"
    wrong = await replay_http.post("/admin/auth/change-password", headers=h, json={"current_password": "nope", "new_password": "Brand-new-pass-77"})
    assert wrong.status_code == 400 and wrong.json()["detail"]["code"] == "wrong_password"
    done = await replay_http.post("/admin/auth/change-password", headers=h, json={"current_password": "Temp-pass-12345", "new_password": "Brand-new-pass-77"})
    assert done.status_code == 200 and done.json()["admin"]["must_change_password"] is False
    assert (await replay_http.get("/admin/auth/me", headers=h)).status_code == 401  # old token revoked
    fresh = {"Authorization": f"Bearer {done.json()['access_token']}"}
    assert (await replay_http.get("/admin/panel/overview", headers=fresh)).status_code == 200
    assert (await login(replay_http, "new@ops.test", "Temp-pass-12345")).status_code == 401


# ── overview / directory / detail ─────────────────────────────────────────────

async def test_overview_and_directory(replay_http, replay_session):
    """Counts reflect the data; search, status filter and paging behave."""
    await make_admin(replay_session, email="v@ops.test", role="viewer")
    h = await auth(replay_http, "v@ops.test")
    _, b, c = [await seed_tenant(replay_session, i) for i in (1, 2, 3)]
    await replay_session.execute(update(type(b)).where(type(b).id == b.id).values(is_active=False))
    await replay_session.commit()

    from app.models.sellertalk24_billing import PaymentOrder

    plan_id = (await replay_session.execute(select(BillingPlan.id).order_by(BillingPlan.id).limit(1))).scalar_one()
    now = datetime.now(timezone.utc)
    for n, (mode, status_, paise) in enumerate([("live", "paid", 565_482), ("live", "created", 100_000), ("test", "paid", 999_900)]):
        replay_session.add(PaymentOrder(
            client_id=c.id, plan_id=plan_id, razorpay_order_id=f"order_ov{n}", amount_paise=paise, base_paise=paise,
            receipt=f"rcpt_ov{n}", status=status_, mode=mode, paid_at=now if status_ == "paid" else None,
        ))
    await replay_session.commit()

    ov = (await replay_http.get("/admin/panel/overview", headers=h)).json()
    assert ov["revenue"]["collected_30d_inr"] == 5654.82 and ov["revenue"]["paid_orders_30d"] == 1  # live + paid only
    assert ov["clients"]["total"] == 3 and ov["clients"]["active"] == 2 and ov["clients"]["suspended"] == 1
    assert len(ov["signups_14d"]) == 14 and ov["signups_14d"][-1]["count"] == 3

    d = (await replay_http.get("/admin/panel/clients", headers=h, params={"q": "acme store 2"})).json()
    assert d["total"] == 1 and d["items"][0]["id"] == b.id and d["items"][0]["is_active"] is False
    assert (await replay_http.get("/admin/panel/clients", headers=h, params={"q": str(c.id)})).json()["items"][0]["id"] == c.id
    assert (await replay_http.get("/admin/panel/clients", headers=h, params={"q": "%"})).json()["total"] == 0  # wildcards are literals
    sus = (await replay_http.get("/admin/panel/clients", headers=h, params={"status": "suspended"})).json()
    assert [i["id"] for i in sus["items"]] == [b.id]
    page = (await replay_http.get("/admin/panel/clients", headers=h, params={"page": 2, "page_size": 2})).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    assert (await replay_http.get("/admin/panel/clients", headers=h, params={"status": "bogus"})).status_code == 422


async def test_client_detail_never_leaks_credentials(replay_http, replay_session):
    """The tenant detail reports channel/payment setup as booleans only."""
    await make_admin(replay_session, email="s@ops.test", role="support")
    h = await auth(replay_http, "s@ops.test")
    c = await seed_tenant(replay_session, 5, with_secrets=True)
    r = await replay_http.get(f"/admin/panel/clients/{c.id}", headers=h)
    assert r.status_code == 200
    body = r.json()
    for secret in ("SECRET-WA-TOKEN", "SECRET-IG-TOKEN", "SECRET-RZP", "SECRET-BANK-123", "hashed_password"):
        assert secret not in r.text
    assert body["channels"]["whatsapp_token_set"] and body["channels"]["instagram_token_set"] and body["channels"]["razorpay_configured"]
    assert body["profile"]["business_name"] == "Acme Store 5" and body["users"][0]["role"] == "owner"
    assert body["counts"]["products"] == 1
    assert (await replay_http.get("/admin/panel/clients/999999", headers=h)).status_code == 404


# ── writes + audit ────────────────────────────────────────────────────────────

async def test_flags_update_and_audit(replay_http, replay_session):
    """PATCH flags changes only the sent fields (null is meaningful for router_v2) and records before/after."""
    await make_admin(replay_session, email="s2@ops.test", role="support")
    await make_admin(replay_session, email="v2@ops.test", role="viewer")
    h, hv = await auth(replay_http, "s2@ops.test"), await auth(replay_http, "v2@ops.test")
    c = await seed_tenant(replay_session, 6)
    url = f"/admin/panel/clients/{c.id}/flags"
    assert (await replay_http.patch(url, headers=hv, json={"router_v2_enabled": True})).status_code == 403
    r = await replay_http.patch(url, headers=h, json={"router_v2_enabled": True, "daily_message_limit": 250, "reason": "pilot"})
    assert r.json()["changed"] == {"router_v2_enabled": True, "daily_message_limit": 250}
    r = await replay_http.patch(url, headers=h, json={"router_v2_enabled": None})
    assert r.json()["changed"] == {"router_v2_enabled": None}
    assert (await replay_http.patch(url, headers=h, json={"daily_message_limit": -1})).status_code == 422
    assert (await replay_http.patch(url, headers=h, json={})).json()["changed"] == {}
    d = (await replay_http.get(f"/admin/panel/clients/{c.id}", headers=h)).json()
    assert d["flags"]["router_v2_enabled"] is None and d["flags"]["daily_message_limit"] == 250
    rows = await audit_actions(replay_session, "client.flags")
    assert len(rows) == 2 and rows[0].actor == "s2@ops.test" and rows[0].client_id == c.id and rows[0].reason == "pilot"
    assert rows[0].detail["after"] == {"router_v2_enabled": True, "daily_message_limit": 250}
    assert [x["action"] for x in d["recent_audit"]][:2] == ["client.flags", "client.flags"]
    denied = await audit_actions(replay_session, "permission_denied")
    assert denied and denied[0].actor == "v2@ops.test" and not denied[0].success


async def test_suspend_activate_are_audited_with_reason(replay_http, replay_session):
    """The legacy suspend/activate routes now record who did it and why."""
    await make_admin(replay_session, email="s3@ops.test", role="support")
    h = await auth(replay_http, "s3@ops.test")
    c = await seed_tenant(replay_session, 7)
    assert (await replay_http.put(f"/admin/clients/{c.id}/suspend", headers=h, json={"reason": "chargeback"})).json()["is_active"] is False
    assert (await replay_http.put(f"/admin/clients/{c.id}/activate", headers=h)).json()["is_active"] is True  # body optional
    rows = await audit_actions(replay_session)
    acts = [(r.action, r.reason) for r in rows if r.action.startswith("client.")]
    assert acts == [("client.suspend", "chargeback"), ("client.activate", "")]


async def test_conversations_orders_and_transcript_access_is_audited(replay_http, replay_session):
    """Inbox/orders list; transcripts are scoped to the client and reading one is audited."""
    await make_admin(replay_session, email="s4@ops.test", role="support")
    await make_admin(replay_session, email="b4@ops.test", role="billing")
    h, hb = await auth(replay_http, "s4@ops.test"), await auth(replay_http, "b4@ops.test")
    c1, c2 = await seed_tenant(replay_session, 8), await seed_tenant(replay_session, 9)
    conv = Conversation(client_id=c1.id, phone_number="919000000001", channel="whatsapp", customer_name="Ravi")
    other = Conversation(client_id=c2.id, phone_number="919000000002", channel="whatsapp")
    sandbox = Conversation(client_id=c1.id, phone_number="sandbox", channel="whatsapp", is_sandbox=True)
    replay_session.add_all([conv, other, sandbox])
    await replay_session.flush()
    replay_session.add_all([Message(conversation_id=conv.id, role="user", content="hi"), Message(conversation_id=conv.id, role="model", content="hello!")])
    replay_session.add(Order(
        order_number="ADM-1", client_id=c1.id, customer_name="Ravi", customer_phone="919000000001", delivery_address="Surat",
        product_name="Item 8", quantity=2, unit_price=500.0, total_amount=1000.0, status="payment_submitted",
    ))
    await replay_session.commit()

    lst = (await replay_http.get(f"/admin/panel/clients/{c1.id}/conversations", headers=h)).json()
    assert lst["total"] == 1 and lst["items"][0]["message_count"] == 2 and lst["items"][0]["customer_name"] == "Ravi"  # sandbox hidden
    msgs = await replay_http.get(f"/admin/panel/clients/{c1.id}/conversations/{conv.id}/messages", headers=h)
    assert [m["content"] for m in msgs.json()] == ["hi", "hello!"]
    assert (await replay_http.get(f"/admin/panel/clients/{c1.id}/conversations/{other.id}/messages", headers=h)).status_code == 404  # cross-tenant
    views = await audit_actions(replay_session, "conversation.view")
    assert len(views) == 1 and views[0].client_id == c1.id and views[0].target_id == str(conv.id)
    assert (await replay_http.get(f"/admin/panel/clients/{c1.id}/conversations", headers=hb)).status_code == 403  # billing role: no chats

    orders = (await replay_http.get(f"/admin/panel/clients/{c1.id}/orders", headers=hb, params={"status": "payment_submitted"})).json()
    assert orders["total"] == 1 and orders["items"][0]["order_number"] == "ADM-1"
    assert (await replay_http.get(f"/admin/panel/clients/{c1.id}/orders", headers=hb, params={"status": "paid"})).json()["total"] == 0
    ov = (await replay_http.get("/admin/panel/overview", headers=hb)).json()
    assert ov["attention"]["payments_awaiting_review"] == 1


async def test_impersonation_token_works_for_tenant_api_and_is_audited(replay_http, replay_session):
    """A reason is mandatory; the minted token is a short-lived tenant token tagged with the admin; suspended tenants are refused."""
    await make_admin(replay_session, email="s5@ops.test", role="support")
    h = await auth(replay_http, "s5@ops.test")
    c = await seed_tenant(replay_session, 10)
    url = f"/admin/panel/clients/{c.id}/impersonate"
    assert (await replay_http.post(url, headers=h, json={"reason": "x"})).status_code == 422
    r = await replay_http.post(url, headers=h, json={"reason": "helping with catalogue upload"})
    assert r.status_code == 200 and r.json()["expires_in"] == 1800
    me = await replay_http.get("/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.status_code == 200 and me.json()["email"] == c.email
    from jose import jwt

    claims = jwt.get_unverified_claims(r.json()["access_token"])
    assert claims["impersonated_by"] == "s5@ops.test"
    rows = await audit_actions(replay_session, "client.impersonate")
    assert rows[0].reason == "helping with catalogue upload" and rows[0].client_id == c.id
    await replay_session.execute(update(type(c)).where(type(c).id == c.id).values(is_active=False))
    await replay_session.commit()
    assert (await replay_http.post(url, headers=h, json={"reason": "second attempt"})).status_code == 409


async def test_billing_plan_edit_and_billing_role_permissions(replay_http, replay_session):
    """Only plans.write can edit sellable plans; edits are validated and audited with before/after."""
    await make_admin(replay_session, email="b6@ops.test", role="billing")
    await make_admin(replay_session, email="s6@ops.test", role="support")
    hb, hs = await auth(replay_http, "b6@ops.test"), await auth(replay_http, "s6@ops.test")
    plans = (await replay_http.get("/admin/panel/billing-plans", headers=hs)).json()
    assert [p["code"] for p in plans][:3] == ["starter_1500", "growth_5000", "pro_9000"]
    pid, old_price = plans[0]["id"], plans[0]["price_paise"]
    url = f"/admin/panel/billing-plans/{pid}"
    assert (await replay_http.put(url, headers=hs, json={"price_paise": 1, "reason": "no rights"})).status_code == 403
    assert (await replay_http.put(url, headers=hb, json={"price_paise": -5, "reason": "negative price"})).status_code == 422
    assert (await replay_http.put(url, headers=hb, json={"reason": "nothing to change"})).status_code == 400
    r = await replay_http.put(url, headers=hb, json={"price_paise": old_price + 100, "reason": "festive price rise"})
    assert r.status_code == 200 and r.json()["price_paise"] == old_price + 100
    assert (await replay_http.put("/admin/panel/billing-plans/99999", headers=hb, json={"name": "X", "reason": "unknown plan"})).status_code == 404
    row = (await audit_actions(replay_session, "billing_plan.update"))[0]
    assert row.detail["before"] == {"price_paise": old_price} and row.detail["after"] == {"price_paise": old_price + 100}
    assert row.reason == "festive price rise"
    fresh = (await replay_session.execute(select(BillingPlan).where(BillingPlan.id == pid).execution_options(populate_existing=True))).scalar_one()
    assert fresh.price_paise == old_price + 100


async def test_billing_exempt_via_operator_is_audited_under_operator_name(replay_http, replay_session):
    """Retrofitted billing routes attribute the action to the signed-in operator in both audit trails."""
    from app.models.sellertalk24_billing import BillingAdminLog

    await make_admin(replay_session, email="b7@ops.test", role="billing")
    h = await auth(replay_http, "b7@ops.test")
    c = await seed_tenant(replay_session, 11)
    r = await replay_http.put(f"/admin/billing/clients/{c.id}/billing-exempt", headers=h, json={"exempt": True, "reason": "design partner"})
    assert r.status_code == 200 and r.json()["billing_exempt"] is True
    assert (await audit_actions(replay_session, "billing.exempt"))[0].actor == "b7@ops.test"
    log = (await replay_session.execute(select(BillingAdminLog).where(BillingAdminLog.client_id == c.id))).scalars().all()
    assert log and all(x.actor == "b7@ops.test" for x in log)
    assert (await replay_http.get("/admin/billing/subscriptions", headers=h)).status_code == 200


async def test_system_health_reports_without_secrets(replay_http, replay_session):
    """The system page works on a healthy DB, lists scheduler/LLM state and never echoes key material."""
    await make_admin(replay_session, email="v8@ops.test", role="viewer")
    h = await auth(replay_http, "v8@ops.test")
    r = await replay_http.get("/admin/panel/system", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["checks"]["database"] == "ok" and "llm_models" in body["config"] and isinstance(body["scheduler"], list)
    assert body["config"]["secrets_configured"]["groq"] is True
    from app.config import get_settings

    for needle in (ADMIN_KEY, get_settings().secret_key, get_settings().groq_api_key):
        assert len(needle) < 8 or needle not in r.text


# ── operator management ───────────────────────────────────────────────────────

async def test_operator_management_guards_and_audit_log(replay_http, replay_session):
    """Create/update/reset operators; self-lockout guards; audit-log filters and permissions."""
    root = await make_admin(replay_session, email="root2@ops.test", role="superadmin")
    await make_admin(replay_session, email="sup@ops.test", role="support")
    h = await auth(replay_http, "root2@ops.test")
    hs = await auth(replay_http, "sup@ops.test")
    assert (await replay_http.get("/admin/users", headers=hs)).status_code == 403
    assert (await replay_http.get("/admin/audit-log", headers=hs)).status_code == 403

    created = await replay_http.post("/admin/users", headers=h, json={"email": "New.Op@ops.test", "name": "New Op", "role": "billing"})
    assert created.status_code == 201
    temp = created.json()["temporary_password"]
    assert created.json()["admin"]["email"] == "new.op@ops.test" and created.json()["admin"]["must_change_password"] is True
    assert "hashed_password" not in created.text
    assert (await login(replay_http, "new.op@ops.test", temp)).status_code == 200
    dup = await replay_http.post("/admin/users", headers=h, json={"email": "new.op@ops.test", "role": "viewer"})
    assert dup.status_code == 400
    assert (await replay_http.post("/admin/users", headers=h, json={"email": "z@ops.test", "role": "root"})).status_code == 400
    assert (await replay_http.post("/admin/users", headers=h, json={"email": "z@ops.test", "role": "viewer", "password": "weak"})).status_code == 400
    new_id = created.json()["admin"]["id"]

    # self-protection: nobody can change their own role / active flag
    assert (await replay_http.patch(f"/admin/users/{root.id}", headers=h, json={"role": "viewer"})).status_code == 403
    assert (await replay_http.patch(f"/admin/users/{root.id}", headers=h, json={"is_active": False})).status_code == 403

    # with a second superadmin present, demoting one is fine — and revokes the demoted account's tokens
    second = await make_admin(replay_session, email="root4@ops.test", role="superadmin")
    h4 = await auth(replay_http, "root4@ops.test")
    demote = await replay_http.patch(f"/admin/users/{root.id}", headers=h4, json={"role": "support"})
    assert demote.status_code == 200 and demote.json()["role"] == "support"
    assert (await replay_http.get("/admin/users", headers=h)).status_code == 401  # root's old token is dead
    assert second.id != root.id

    # reset password revokes sessions and forces a change
    rp = await replay_http.post(f"/admin/users/{new_id}/reset-password", headers=h4)
    assert rp.status_code == 200 and rp.json()["temporary_password"] != temp
    assert (await login(replay_http, "new.op@ops.test", temp)).status_code == 401
    assert (await login(replay_http, "new.op@ops.test", rp.json()["temporary_password"])).json()["admin"]["must_change_password"] is True
    assert (await replay_http.post(f"/admin/users/{new_id}/unlock", headers=h4)).status_code == 200
    assert (await replay_http.post("/admin/users/99999/unlock", headers=h4)).status_code == 404

    log = (await replay_http.get("/admin/audit-log", headers=h4, params={"action": "admin_user."})).json()
    assert log["total"] >= 4 and all(i["action"].startswith("admin_user.") for i in log["items"])
    assert all("password" not in str(i["detail"]).lower() or "[redacted]" in str(i["detail"]) for i in log["items"])
    only_fail = (await replay_http.get("/admin/audit-log", headers=h4, params={"success": "false"})).json()
    assert only_fail["total"] >= 1 and all(not i["success"] for i in only_fail["items"])
    assert (await replay_http.get("/admin/audit-log", headers=h4, params={"page_size": 500})).status_code == 422


async def test_last_active_superadmin_cannot_be_removed(replay_http, replay_session):
    """With a single active superadmin, any attempt to demote/deactivate them (by anyone) is refused."""
    only = await make_admin(replay_session, email="only@ops.test", role="superadmin")
    h = await auth(replay_http, "only@ops.test")
    key = {"X-Admin-Key": ADMIN_KEY, "X-Admin-User": "breakglass"}  # legacy key = no "self", exercises the guard
    r = await replay_http.patch(f"/admin/users/{only.id}", headers=key, json={"role": "viewer"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "last_superadmin"
    r = await replay_http.patch(f"/admin/users/{only.id}", headers=key, json={"is_active": False})
    assert r.status_code == 409
    assert (await replay_http.get("/admin/auth/me", headers=h)).status_code == 200


async def test_legacy_key_actions_are_attributed(replay_http, replay_session):
    """Scripts using X-Admin-Key + X-Admin-User are audited under that name; without it as 'admin-key'."""
    c = await seed_tenant(replay_session, 12)
    key = {"X-Admin-Key": ADMIN_KEY}
    await replay_http.put(f"/admin/clients/{c.id}/suspend", headers={**key, "X-Admin-User": "cron-bot"})
    await replay_http.put(f"/admin/clients/{c.id}/activate", headers=key)
    rows = await audit_actions(replay_session)
    assert [(r.actor, r.action) for r in rows if r.action.startswith("client.")] == [("cron-bot", "client.suspend"), ("admin-key", "client.activate")]
