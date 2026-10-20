"""
Dashboard-first onboarding, end to end: real app, real HTTP, real Postgres.

Signup (email + Google) -> quick start -> sample products -> checklist derived from real state -> go live ->
trial rules (starts at go-live, 7 days / 100 conversations, grace, one trial per WhatsApp number) -> the bot being
silent to real customers until then.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select

import app.main  # noqa: F401 — bind every module's get_settings BEFORE the replay fixture patches app.config
from app.config import Settings
from app.models.client import Client
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.product_variant import ProductVariant
from app.models.sellertalk24_billing import ConversationUsageLog
from app.models.trial_claim import TrialClaim
from app.models.user import User
from app.services import google_auth_service
from app.services.billing import entitlement
from app.services.billing.entitlement import EntitlementState
from tests.replay.conftest import _PG_AVAILABLE
from tests.replay.helpers import capture_all, send_message
from tests.replay.test_sellertalk24_billing_enforcement import seed_sub

pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")

PASSWORD = "Sup3r-secret-pass"


@pytest.fixture(autouse=True)
def _no_reply_delay(monkeypatch):
    """Zero the human-like reply delay."""
    monkeypatch.setattr("app.services.order_pipeline.random.uniform", lambda *_a, **_k: 0)


async def signup(http, email: str = "owner@shop.test", **extra) -> dict:
    """Register through the real endpoint and sign in; returns bearer headers."""
    r = await http.post("/auth/register", json={"email": email, "password": PASSWORD, **extra})
    assert r.status_code == 201, r.text
    r = await http.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def quick_start(http, headers, name: str = "Riya Sarees", language: str = "en"):
    """POST /onboarding/quick-start."""
    return await http.post("/onboarding/quick-start", headers=headers, json={"business_name": name, "language": language})


async def checklist(http, headers) -> dict:
    """GET /onboarding/checklist as a dict keyed by item."""
    r = await http.get("/onboarding/checklist", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def done(cl: dict) -> dict[str, bool]:
    """{key: done} from a checklist payload."""
    return {i["key"]: i["done"] for i in cl["items"]}


async def client_row(session, email: str) -> Client:
    """Fresh Client row by email."""
    return (await session.execute(
        select(Client).where(Client.email == email).execution_options(populate_existing=True))).scalar_one()


async def finish_setup(http, headers, *, pnid: str = "PNID-A", remove_demo: bool = True) -> None:
    """Take an account through every checklist prerequisite except pressing 'Go live'."""
    r = await http.post("/catalogue/products", headers=headers, json={"name": "Real Saree", "price": 1200, "stock": 5})
    assert r.status_code == 201, r.text
    r = await http.post("/onboarding/setup-agent", headers=headers, json={
        "business_name": "Riya Sarees", "business_type": "textile", "business_description": "Sarees and kurtis",
    })
    assert r.status_code == 200, r.text
    r = await http.patch("/auth/me", headers=headers, json={"whatsapp_phone_number_id": pnid, "whatsapp_access_token": "tok"})
    assert r.status_code == 200, r.text
    if remove_demo:
        assert (await http.delete("/onboarding/demo-products", headers=headers)).status_code == 200


async def mark_tested(session, email: str) -> None:
    """Stand in for three simulator turns (covered separately in the simulator tests)."""
    c = await client_row(session, email)
    c.agent_tested_at = datetime.now(timezone.utc)
    await session.commit()


# ── signup + quick start ──────────────────────────────────────────────────────

async def test_signup_without_a_business_name_starts_in_setup(replay_http, replay_session):
    """No forced stepper: register needs only email + password; the account starts in 'setup' with the bot off."""
    h = await signup(replay_http)
    me = (await replay_http.get("/auth/me", headers=h)).json()
    assert me["account_status"] == "setup" and me["bot_active"] is False
    assert me["quick_start_completed"] is False and me["business_name"] == "" and me["trial_ends_at"] is None
    assert me["catalogue_slug"] is None


async def test_legacy_register_with_a_business_name_still_works(replay_http, replay_session):
    """Old clients that still send business_name get a slug immediately."""
    h = await signup(replay_http, business_name="Mehta Textiles")
    me = (await replay_http.get("/auth/me", headers=h)).json()
    assert me["business_name"] == "Mehta Textiles" and me["catalogue_slug"] == "mehta-textiles"


async def test_quick_start_saves_name_language_and_seeds_three_samples_once(replay_http, replay_session):
    """One screen: name + EN/HI/GU. Three flagged sample products appear; submitting again never re-seeds."""
    h = await signup(replay_http)
    r = await quick_start(replay_http, h, "Riya Sarees", "gu")
    assert r.status_code == 200 and r.json() == {"business_name": "Riya Sarees", "language": "gu", "demo_products_created": 3}

    me = (await replay_http.get("/auth/me", headers=h)).json()
    assert me["business_name"] == "Riya Sarees" and me["dashboard_language"] == "gu"
    assert me["quick_start_completed"] is True and me["catalogue_slug"] == "riya-sarees"

    products = (await replay_http.get("/catalogue/products", headers=h)).json()
    assert len(products) == 3 and all(p["is_demo"] for p in products)
    assert {p["name"] for p in products} == {"Cotton Kurti", "Banarasi Silk Saree", "Handmade Tote Bag"}
    kurti = next(p for p in products if p["name"] == "Cotton Kurti")
    assert kurti["has_variants"] and set(kurti["available_colors"]) == {"Pink", "Blue"} and kurti["stock"] == 40

    again = await quick_start(replay_http, h, "Riya Silks", "hi")
    assert again.json()["demo_products_created"] == 0
    assert len((await replay_http.get("/catalogue/products", headers=h)).json()) == 3
    assert (await replay_http.get("/auth/me", headers=h)).json()["business_name"] == "Riya Silks"


async def test_quick_start_rejects_blank_name_and_unknown_language(replay_http, replay_session):
    """Validation: whitespace-only names and languages other than en/hi/gu are 422."""
    h = await signup(replay_http)
    assert (await quick_start(replay_http, h, "   ")).status_code == 422
    assert (await quick_start(replay_http, h, "Shop", "fr")).status_code == 422


async def test_gujarati_only_business_name_still_gets_a_unique_slug(replay_http, replay_session):
    """A name with no ASCII letters must not produce an empty (colliding) slug."""
    h1 = await signup(replay_http, "a@shop.test")
    h2 = await signup(replay_http, "b@shop.test")
    await quick_start(replay_http, h1, "રિયા સાડી")
    await quick_start(replay_http, h2, "રિયા સાડી")
    slugs = {(await replay_http.get("/auth/me", headers=h)).json()["catalogue_slug"] for h in (h1, h2)}
    assert slugs == {"shop", "shop-1"}


# ── checklist: derived from real state ────────────────────────────────────────

async def test_checklist_is_derived_from_real_state_not_manual_ticks(replay_http, replay_session):
    """Each item flips only when the underlying fact becomes true; sample products never count as 'Add products'."""
    h = await signup(replay_http)
    await quick_start(replay_http, h)

    cl = await checklist(replay_http, h)
    assert [i["key"] for i in cl["items"]] == [
        "add_products", "setup_agent", "connect_whatsapp", "connect_instagram", "test_agent", "go_live"]
    assert [i["required"] for i in cl["items"]] == [True, True, True, False, True, True]
    assert done(cl) == {k: False for k in done(cl)} and cl["done_count"] == 0 and cl["total"] == 6
    assert cl["next_key"] == "add_products" and cl["demo_products"] == 3 and cl["hidden"] is False
    assert cl["account_status"] == "setup" and cl["go_live_mode"] == "trial" and cl["trial"] is None

    await replay_http.post("/catalogue/products", headers=h, json={"name": "Mine", "price": 100, "stock": 1})
    assert done(await checklist(replay_http, h))["add_products"] is True

    await replay_http.post("/onboarding/setup-agent", headers=h, json={
        "business_name": "Riya", "business_type": "textile", "business_description": "Sarees"})
    cl = await checklist(replay_http, h)
    assert done(cl)["setup_agent"] is True and cl["next_key"] == "connect_whatsapp"

    await replay_http.patch("/auth/me", headers=h, json={"whatsapp_phone_number_id": "PN1"})
    assert done(await checklist(replay_http, h))["connect_whatsapp"] is False         # token still missing
    await replay_http.patch("/auth/me", headers=h, json={"whatsapp_access_token": "tok"})
    cl = await checklist(replay_http, h)
    assert done(cl)["connect_whatsapp"] is True and cl["next_key"] == "test_agent" and cl["can_go_live"] is False

    await mark_tested(replay_session, "owner@shop.test")
    cl = await checklist(replay_http, h)
    assert done(cl)["test_agent"] is True and cl["can_go_live"] is True and cl["next_key"] == "go_live"
    assert done(cl)["connect_instagram"] is False and cl["hidden"] is False


async def test_deactivated_real_products_do_not_count(replay_http, replay_session):
    """'Add products' needs an ACTIVE non-sample product."""
    h = await signup(replay_http)
    await quick_start(replay_http, h)
    p = (await replay_http.post("/catalogue/products", headers=h, json={"name": "Mine", "price": 100, "stock": 1})).json()
    await replay_http.put(f"/catalogue/products/{p['id']}", headers=h, json={"is_active": False})
    assert done(await checklist(replay_http, h))["add_products"] is False


async def test_checklist_is_owner_only(replay_http, replay_session):
    """A staff login cannot read or act on the owner's setup."""
    h = await signup(replay_http)
    r = await replay_http.post("/team/invite", headers=h, json={"email": "staff@shop.test", "role": "staff"})
    assert r.status_code in (200, 201), r.text
    staff = await replay_session.scalar(select(User).where(User.email == "staff@shop.test"))
    staff.hashed_password = "x"
    await replay_session.commit()
    from app.services import auth_service
    token = auth_service.create_access_token({"sub": "staff@shop.test"})
    assert (await replay_http.get("/onboarding/checklist", headers={"Authorization": f"Bearer {token}"})).status_code == 403


async def test_remove_samples_is_one_click_and_leaves_real_products(replay_http, replay_session):
    """DELETE /onboarding/demo-products removes all three samples (variants too) and nothing else."""
    h = await signup(replay_http)
    await quick_start(replay_http, h)
    await replay_http.post("/catalogue/products", headers=h, json={"name": "Mine", "price": 100, "stock": 1})

    r = await replay_http.delete("/onboarding/demo-products", headers=h)
    assert r.status_code == 200 and r.json() == {"removed": 3}

    names = [p["name"] for p in (await replay_http.get("/catalogue/products", headers=h)).json()]
    assert names == ["Mine"]
    assert await replay_session.scalar(select(func.count()).select_from(ProductVariant)) == 0
    assert (await checklist(replay_http, h))["demo_products"] == 0
    assert (await replay_http.delete("/onboarding/demo-products", headers=h)).json() == {"removed": 0}


# ── go live + trial ───────────────────────────────────────────────────────────

async def test_go_live_is_refused_until_the_checklist_is_done(replay_http, replay_session):
    """409 SETUP_INCOMPLETE lists exactly what is missing; nothing changes on the account."""
    h = await signup(replay_http)
    await quick_start(replay_http, h)
    r = await replay_http.post("/onboarding/go-live", headers=h, json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SETUP_INCOMPLETE"
    assert r.json()["detail"]["missing"] == ["add_products", "setup_agent", "connect_whatsapp", "test_agent"]
    c = await client_row(replay_session, "owner@shop.test")
    assert c.bot_active is False and c.account_status == "setup" and c.trial_started_at is None


async def test_go_live_refuses_while_sample_products_remain_unless_told_to_remove_them(replay_http, replay_session):
    """Customers must never be sold samples: 409 DEMO_PRODUCTS_PRESENT, or remove_demo_products=true to do it in one step."""
    h = await signup(replay_http)
    await quick_start(replay_http, h)
    await finish_setup(replay_http, h, remove_demo=False)
    await mark_tested(replay_session, "owner@shop.test")

    r = await replay_http.post("/onboarding/go-live", headers=h, json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "DEMO_PRODUCTS_PRESENT"

    r = await replay_http.post("/onboarding/go-live", headers=h, json={"remove_demo_products": True})
    assert r.status_code == 200 and r.json()["demo_products_removed"] == 3
    assert [p["name"] for p in (await replay_http.get("/catalogue/products", headers=h)).json()] == ["Real Saree"]


async def test_the_trial_starts_at_go_live_not_at_signup(replay_http, replay_session):
    """Setup has no trial/expiry; go-live sets trial_started_at (+7 days), flips the bot on and claims the number."""
    h = await signup(replay_http)
    await quick_start(replay_http, h)
    c = await client_row(replay_session, "owner@shop.test")
    assert c.trial_started_at is None and c.trial_ends_at is None
    ent = await entitlement.get_entitlement(replay_session, c)
    assert ent.state is EntitlementState.SETUP and not ent.restricted

    await finish_setup(replay_http, h)
    await mark_tested(replay_session, "owner@shop.test")
    before = datetime.now(timezone.utc)
    r = await replay_http.post("/onboarding/go-live", headers=h, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account_status"] == "trial" and body["bot_active"] is True
    assert body["trial"]["active"] is True and body["trial"]["conversation_limit"] == 100 and body["trial"]["conversations_used"] == 0

    c = await client_row(replay_session, "owner@shop.test")
    assert before <= c.trial_started_at <= datetime.now(timezone.utc)
    assert c.trial_ends_at - c.trial_started_at == timedelta(days=7)
    assert c.go_live_at is not None and c.bot_active is True
    claim = await replay_session.scalar(select(TrialClaim).where(TrialClaim.phone_number_id == "PNID-A"))
    assert claim is not None and claim.client_id == c.id
    ent = await entitlement.get_entitlement(replay_session, c)
    assert ent.state is EntitlementState.TRIAL and ent.trial_conversation_limit == 100

    cl = await checklist(replay_http, h)
    assert cl["hidden"] is True and cl["go_live_mode"] == "live" and done(cl)["go_live"] is True

    again = await replay_http.post("/onboarding/go-live", headers=h, json={})            # idempotent
    assert again.status_code == 200
    assert (await client_row(replay_session, "owner@shop.test")).trial_started_at == c.trial_started_at


async def test_one_trial_per_whatsapp_phone_number_id(replay_http, replay_session):
    """A second account connecting the same WhatsApp number cannot start another trial; it must subscribe."""
    h1 = await signup(replay_http, "first@shop.test")
    await quick_start(replay_http, h1)
    await finish_setup(replay_http, h1, pnid="SHARED-PN")
    await mark_tested(replay_session, "first@shop.test")
    assert (await replay_http.post("/onboarding/go-live", headers=h1, json={})).status_code == 200

    h2 = await signup(replay_http, "second@shop.test")
    await quick_start(replay_http, h2)
    await finish_setup(replay_http, h2, pnid="SHARED-PN")
    await mark_tested(replay_session, "second@shop.test")
    assert (await checklist(replay_http, h2))["go_live_mode"] == "trial_used"
    r = await replay_http.post("/onboarding/go-live", headers=h2, json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "TRIAL_ALREADY_USED"
    c2 = await client_row(replay_session, "second@shop.test")
    assert c2.bot_active is False and c2.trial_started_at is None and c2.account_status == "setup"


async def test_reconnecting_the_same_number_does_not_restart_the_trial(replay_http, replay_session):
    """The claim is permanent: after the trial lapsed, the same account is 'live' and keeps its original dates."""
    h = await signup(replay_http)
    await quick_start(replay_http, h)
    await finish_setup(replay_http, h)
    await mark_tested(replay_session, "owner@shop.test")
    await replay_http.post("/onboarding/go-live", headers=h, json={})
    c = await client_row(replay_session, "owner@shop.test")
    started = c.trial_started_at
    await replay_http.patch("/auth/me", headers=h, json={"whatsapp_phone_number_id": "PNID-A"})
    await replay_http.post("/onboarding/go-live", headers=h, json={})
    assert (await client_row(replay_session, "owner@shop.test")).trial_started_at == started
    assert await replay_session.scalar(select(func.count()).select_from(TrialClaim)) == 1


async def test_a_running_plan_goes_live_without_a_trial(replay_http, replay_session):
    """An account that already pays goes live on its plan: status 'active', no trial dates, no claim."""
    h = await signup(replay_http)
    await quick_start(replay_http, h)
    await finish_setup(replay_http, h)
    await mark_tested(replay_session, "owner@shop.test")
    c = await client_row(replay_session, "owner@shop.test")
    await seed_sub(replay_session, c.id, start=datetime.now(timezone.utc) - timedelta(days=1))
    assert (await checklist(replay_http, h))["go_live_mode"] == "plan"

    r = await replay_http.post("/onboarding/go-live", headers=h, json={})
    assert r.status_code == 200 and r.json()["account_status"] == "active" and r.json()["trial"] is None
    c = await client_row(replay_session, "owner@shop.test")
    assert c.trial_started_at is None and c.bot_active is True
    assert await replay_session.scalar(select(func.count()).select_from(TrialClaim)) == 0


# ── trial lifecycle: 7 days / 100 conversations, then grace, then expiry ──────

async def _live_trial_client(http, session) -> Client:
    """A client that has just gone live on its trial."""
    h = await signup(http)
    await quick_start(http, h)
    await finish_setup(http, h)
    await mark_tested(session, "owner@shop.test")
    assert (await http.post("/onboarding/go-live", headers=h, json={})).status_code == 200
    return await client_row(session, "owner@shop.test")


async def test_trial_ends_by_time_then_grace_then_expiry(replay_http, replay_session):
    """After 7 days the trial lapses into the normal 3-day grace, then EXPIRED; the grace counts from the trial's end."""
    c = await _live_trial_client(replay_http, replay_session)
    day = timedelta(days=1)
    ent = await entitlement.get_entitlement(replay_session, c, c.trial_started_at + 6 * day)
    assert ent.state is EntitlementState.TRIAL

    ent = await entitlement.get_entitlement(replay_session, c, c.trial_ends_at + day)
    assert ent.state is EntitlementState.GRACE and ent.grace_ends_at == c.trial_ends_at + 3 * day
    assert ent.anchor_key == f"trial:{c.id}" and not ent.restricted

    ent = await entitlement.get_entitlement(replay_session, c, c.trial_ends_at + 4 * day)
    assert ent.state is EntitlementState.EXPIRED and ent.restricted


async def test_trial_ends_at_100_conversations_whichever_comes_first(replay_http, replay_session):
    """The 100th counted conversation ends the trial (days to spare) and anchors the grace on that moment."""
    c = await _live_trial_client(replay_http, replay_session)
    base = c.trial_started_at + timedelta(hours=1)
    for i in range(100):
        replay_session.add(ConversationUsageLog(
            client_id=c.id, channel="whatsapp", customer_key=f"c{i}", window_started_at=base + timedelta(seconds=i),
            created_at=base + timedelta(seconds=i)))
    await replay_session.commit()

    soon = c.trial_started_at + timedelta(days=2)
    ent = await entitlement.get_entitlement(replay_session, c, soon)
    assert ent.state is EntitlementState.GRACE
    assert ent.grace_ends_at == base + timedelta(seconds=99) + timedelta(days=3)


async def test_99_conversations_is_still_trial_and_simulator_style_rows_are_not_counted(replay_http, replay_session):
    """One short of the cap is still TRIAL; usage rows that belong to a paid period never count against the trial."""
    c = await _live_trial_client(replay_http, replay_session)
    base = c.trial_started_at + timedelta(hours=1)
    for i in range(99):
        replay_session.add(ConversationUsageLog(
            client_id=c.id, channel="whatsapp", customer_key=f"c{i}", window_started_at=base + timedelta(seconds=i),
            created_at=base + timedelta(seconds=i)))
    await replay_session.commit()
    ent = await entitlement.get_entitlement(replay_session, c, base + timedelta(days=1))
    assert ent.state is EntitlementState.TRIAL and ent.trial_conversations_used == 99


# ── the bot is silent to real customers until go-live ─────────────────────────

async def test_real_customers_get_no_reply_before_go_live_but_the_message_is_saved(replay_http, replay_session, monkeypatch):
    """Setup status: inbound WhatsApp is stored for the owner to see; nothing is sent. After go-live the bot answers."""
    captured = capture_all(monkeypatch)
    h = await signup(replay_http)
    await quick_start(replay_http, h)
    await finish_setup(replay_http, h, pnid="PN-LIVE")
    customer = "919876500001"

    await send_message(replay_http, customer, "hello", phone_number_id="PN-LIVE")

    assert captured == []
    c = await client_row(replay_session, "owner@shop.test")
    conv = await replay_session.scalar(select(Conversation).where(Conversation.client_id == c.id, Conversation.phone_number == customer))
    assert conv is not None
    stored = (await replay_session.execute(select(Message).where(Message.conversation_id == conv.id))).scalars().all()
    assert [m.content for m in stored if m.role == "user"] == ["hello"]
    assert await replay_session.scalar(select(func.count()).select_from(ConversationUsageLog)) == 0     # nothing counted

    await mark_tested(replay_session, "owner@shop.test")
    assert (await replay_http.post("/onboarding/go-live", headers=h, json={})).status_code == 200
    await send_message(replay_http, customer, "hi there", phone_number_id="PN-LIVE")
    assert captured, "the bot must answer once the account is live"
    assert await replay_session.scalar(select(func.count()).select_from(ConversationUsageLog)) == 1     # now it counts toward the trial


async def test_automation_and_widget_are_off_before_go_live(replay_http, replay_session):
    """Follow-ups/campaigns are denied by the billing gate and the public widget answers 503 until the account is live."""
    h = await signup(replay_http)
    await quick_start(replay_http, h)
    c = await client_row(replay_session, "owner@shop.test")
    assert await entitlement.automation_allowed(replay_session, c.id) is False
    c.bot_active = True
    await replay_session.commit()
    assert await entitlement.automation_allowed(replay_session, c.id) is True


async def test_existing_accounts_are_untouched_by_the_migration_defaults(replay_http, replay_session):
    """A client row created the old way (no lifecycle fields) is live: active status, bot on, quick start not required."""
    c = Client(business_name="Old Shop", email="old@shop.test", hashed_password="x", whatsapp_phone_number_id="OLDPN")
    replay_session.add(c)
    await replay_session.commit()
    await replay_session.refresh(c)
    assert (c.account_status, c.bot_active) == ("active", True)
    ent = await entitlement.get_entitlement(replay_session, c)
    assert ent.state in (EntitlementState.GRACE, EntitlementState.EXPIRED, EntitlementState.ACTIVE)   # legacy rules, not SETUP/TRIAL


# ── Google sign-in ────────────────────────────────────────────────────────────

@pytest.fixture
def google(monkeypatch):
    """Google configured; verify_id_token returns whatever identity the test sets on `g.identity`."""
    class G:
        identity = google_auth_service.GoogleIdentity(sub="g-1", email="gina@shop.test", name="Gina", picture=None)
    g = G()

    async def fake_verify(credential: str):
        if credential == "bad":
            raise google_auth_service.GoogleAuthError("Invalid or expired Google credential.")
        return g.identity

    monkeypatch.setattr("app.services.google_auth_service.verify_id_token", fake_verify)
    from app.config import get_settings as real

    settings = real().model_copy(update={"google_client_id": "cid.apps.googleusercontent.com"})
    monkeypatch.setattr("app.routers.auth.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.google_auth_service.get_settings", lambda: settings)
    return g


async def test_google_config_endpoint(replay_http, google):
    """The login page asks the backend whether to show the button and which client id to use."""
    assert (await replay_http.get("/auth/google/config")).json() == {"enabled": True, "client_id": "cid.apps.googleusercontent.com"}


async def test_google_config_disabled_when_unset(replay_http, monkeypatch):
    """No GOOGLE_CLIENT_ID: button hidden, sign-in answers 503."""
    settings = Settings(google_client_id="")
    monkeypatch.setattr("app.routers.auth.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.google_auth_service.get_settings", lambda: settings)
    assert (await replay_http.get("/auth/google/config")).json() == {"enabled": False, "client_id": ""}
    assert (await replay_http.post("/auth/google", json={"credential": "x"})).status_code == 503


async def test_google_signup_creates_a_setup_account_and_a_session(replay_http, replay_session, google):
    """First Google sign-in: new Client+Owner in 'setup', verified email, and a working JWT (no password exists)."""
    r = await replay_http.post("/auth/google", json={"credential": "ok"})
    assert r.status_code == 200 and r.json()["is_new_account"] is True and r.json()["linked_existing"] is False
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = (await replay_http.get("/auth/me", headers=h)).json()
    assert me["email"] == "gina@shop.test" and me["account_status"] == "setup" and me["current_user"]["is_owner"] is True
    assert me["quick_start_completed"] is False

    user = await replay_session.scalar(select(User).where(User.email == "gina@shop.test"))
    assert user.google_sub == "g-1" and user.email_verified_at is not None and user.hashed_password is None
    pw = await replay_http.post("/auth/login", json={"email": "gina@shop.test", "password": "anything-at-all-123"})
    assert pw.status_code == 401                                                # a Google-only account has no password login

    again = await replay_http.post("/auth/google", json={"credential": "ok"})                 # second time: plain sign-in
    assert again.json()["is_new_account"] is False and again.json()["linked_existing"] is False
    assert await replay_session.scalar(select(func.count()).select_from(Client)) == 1


async def test_google_links_to_an_existing_verified_login_and_keeps_its_password(replay_http, replay_session, google):
    """Same email, email already proven (accepted invite / earlier Google): linked, password login still works."""
    await signup(replay_http, "gina@shop.test")
    user = await replay_session.scalar(select(User).where(User.email == "gina@shop.test"))
    user.email_verified_at = datetime.now(timezone.utc)
    await replay_session.commit()

    r = await replay_http.post("/auth/google", json={"credential": "ok"})
    assert r.status_code == 200 and r.json()["linked_existing"] is True and r.json()["password_login_disabled"] is False
    replay_session.expire_all()
    assert (await replay_session.scalar(select(User).where(User.email == "gina@shop.test"))).google_sub == "g-1"
    assert (await replay_http.post("/auth/login", json={"email": "gina@shop.test", "password": PASSWORD})).status_code == 200
    assert await replay_session.scalar(select(func.count()).select_from(Client)) == 1       # same business, not a duplicate


async def test_google_link_revokes_the_password_of_an_unverified_signup(replay_http, replay_session, google):
    """Anti pre-hijack: a password someone set on an UNVERIFIED email stops working once the real owner signs in with Google."""
    await signup(replay_http, "gina@shop.test")                                  # squatter registered Gina's address first
    r = await replay_http.post("/auth/google", json={"credential": "ok"})
    assert r.status_code == 200 and r.json()["linked_existing"] is True and r.json()["password_login_disabled"] is True
    assert (await replay_http.post("/auth/login", json={"email": "gina@shop.test", "password": PASSWORD})).status_code == 401
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await replay_http.get("/auth/me", headers=h)).status_code == 200      # but Google works, on the same business


async def test_google_email_match_is_case_insensitive(replay_http, replay_session, google):
    """A login registered as Gina@Shop.test is the same person as Google's lower-case address."""
    await signup(replay_http, "Gina@Shop.test")
    r = await replay_http.post("/auth/google", json={"credential": "ok"})
    assert r.json()["linked_existing"] is True and r.json()["is_new_account"] is False
    assert await replay_session.scalar(select(func.count()).select_from(Client)) == 1


async def test_google_refuses_a_different_google_account_for_a_linked_email(replay_http, replay_session, google):
    """An email already linked to Google account A cannot be taken over by Google account B."""
    await replay_http.post("/auth/google", json={"credential": "ok"})
    google.identity = google_auth_service.GoogleIdentity(sub="g-OTHER", email="gina@shop.test", name=None, picture=None)
    assert (await replay_http.post("/auth/google", json={"credential": "ok"})).status_code == 409


async def test_google_bad_credential_and_deactivated_user(replay_http, replay_session, google):
    """Invalid token -> 401; a deactivated login cannot sign in through Google either."""
    assert (await replay_http.post("/auth/google", json={"credential": "bad"})).status_code == 401
    await replay_http.post("/auth/google", json={"credential": "ok"})
    user = await replay_session.scalar(select(User).where(User.email == "gina@shop.test"))
    user.is_active = False
    await replay_session.commit()
    assert (await replay_http.post("/auth/google", json={"credential": "ok"})).status_code == 403


async def test_registering_with_an_email_that_exists_via_google_conflicts(replay_http, replay_session, google):
    """Email/password signup cannot squat an address that already belongs to a Google-created account."""
    await replay_http.post("/auth/google", json={"credential": "ok"})
    r = await replay_http.post("/auth/register", json={"email": "GINA@shop.test", "password": PASSWORD})
    assert r.status_code == 409


async def test_accepting_an_invite_marks_the_email_verified(replay_http, replay_session):
    """The emailed invite link proves the address, so a later Google link keeps the invitee's password."""
    h = await signup(replay_http)
    invite = (await replay_http.post("/team/invite", headers=h, json={"email": "staff@shop.test", "role": "staff"})).json()
    token = invite["invite_link"].split("token=")[-1]
    r = await replay_http.post("/team/accept-invite", json={"token": token, "password": PASSWORD})
    assert r.status_code == 200, r.text
    user = await replay_session.scalar(select(User).where(User.email == "staff@shop.test"))
    assert user.email_verified_at is not None


@pytest_asyncio.fixture(autouse=True)
async def _flush_identity(replay_session):
    """Make sure each test's ORM identity map starts clean (the helpers re-read rows with populate_existing)."""
    yield
    replay_session.expire_all()


async def test_claiming_a_trial_is_atomic_and_idempotent_per_number(replay_http, replay_session):
    """claim_phone_number: the first account wins, the same account may re-claim, any other account loses."""
    from app.services import trial_service

    a = await signup(replay_http, "a@shop.test")
    b = await signup(replay_http, "b@shop.test")
    ca, cb = await client_row(replay_session, "a@shop.test"), await client_row(replay_session, "b@shop.test")

    assert await trial_service.claim_phone_number(replay_session, ca.id, "PN-X") is True
    assert await trial_service.claim_phone_number(replay_session, ca.id, "PN-X") is True       # idempotent for the owner
    assert await trial_service.claim_phone_number(replay_session, cb.id, "PN-X") is False      # taken
    assert await trial_service.claim_phone_number(replay_session, cb.id, "PN-Y") is True       # another number is free
    await replay_session.commit()
    assert a and b


async def test_nothing_at_all_is_sent_to_a_real_customer_before_go_live(replay_http, replay_session, monkeypatch):
    """Not even the typing indicator: a text and a voice note to a not-live number trigger zero channel calls."""
    import unittest.mock as mock

    typing = mock.AsyncMock()
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_typing_indicator", typing)
    captured = capture_all(monkeypatch)
    h = await signup(replay_http)
    await quick_start(replay_http, h)
    await finish_setup(replay_http, h, pnid="PN-QUIET")

    await send_message(replay_http, "919876500002", "hello", phone_number_id="PN-QUIET")
    from tests.replay.helpers import wa_audio_payload

    body, headers = wa_audio_payload("919876500002", phone_number_id="PN-QUIET")
    r = await replay_http.post("/webhook", content=body, headers=headers)

    assert r.status_code == 200
    assert captured == [] and not typing.await_count
