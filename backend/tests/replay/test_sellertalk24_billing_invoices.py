# ruff: noqa: F811  (the `env` fixture is imported from the API test module and used as a parameter)
"""
SellerTalk24 invoices, billing emails and admin endpoints — real Postgres, the real FastAPI app.

Invoice numbering is tested at the database level (concurrency, rollback gaps, financial-year rollover) and
through real purchases (activation hook, GST split, idempotency, failure isolation, PDF endpoint). Emails are
captured by a recording provider. Admin endpoints are exercised with the real X-Admin-Key dependency.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.client import Client
from app.models.sellertalk24_billing import (
    BillingAdminLog,
    ClientSubscription,
    Invoice,
    InvoiceCounter,
    PaymentEvent,
    PaymentOrder,
)
from app.services import email_service
from app.services.billing import emails as billing_emails
from app.services.billing import invoices, maintenance
from app.services.billing.activation import activate_from_payment
from app.services.billing.pricing import round_half_up_div
from tests.replay.test_sellertalk24_billing_api import Env, ScriptedGateway, env  # noqa: F401  (env is a fixture)

pytestmark = pytest.mark.asyncio

UTC = timezone.utc
CURRENT_FY = invoices.financial_year(datetime.now(UTC))
GUJARAT_GSTIN = "24ABCDE1234F1Z5"
MAHARASHTRA_GSTIN = "27AAPFU0939F1ZV"


# --- fixtures ------------------------------------------------------------------------------

class Recorder:
    """Email provider that remembers what it was asked to send."""

    def __init__(self) -> None:
        self.sent: list[email_service.EmailMessage] = []

    async def send(self, message, sender) -> None:
        self.sent.append(message)

    def to(self, address: str) -> list[email_service.EmailMessage]:
        return [m for m in self.sent if m.to == address]


@pytest.fixture(autouse=True)
def mailbox(monkeypatch) -> Recorder:
    """Capture every outbound email (and pin the seller identity printed on invoices)."""
    rec = Recorder()
    monkeypatch.setattr(email_service, "get_email_provider", lambda settings=None: rec)
    pinned = get_settings().model_copy(update=dict(
        seller_gstin="24AAAAA0000A1Z5", seller_address="Ahmedabad, Gujarat", seller_state_code="24",
        invoice_prefix="ST24", gst_rate_bps=1800,
    ))
    monkeypatch.setattr(invoices, "get_settings", lambda: pinned)
    return rec


@pytest_asyncio.fixture
async def pool(replay_db_url):
    """A session factory on its own engine, for tests that need genuinely concurrent transactions."""
    engine = create_async_engine(replay_db_url, echo=False, pool_size=30, max_overflow=0)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


ADMIN_KEY = "unit-test-admin-key"


@pytest.fixture(autouse=True)
def pinned_admin_key(monkeypatch):
    """require_admin reads app.routers.admin.get_settings(); give it a known key independent of .env / harness."""
    admin_settings = get_settings().model_copy(update=dict(admin_secret_key=ADMIN_KEY))
    monkeypatch.setattr("app.routers.admin.get_settings", lambda: admin_settings)


def admin_headers() -> dict:
    return {"X-Admin-Key": ADMIN_KEY}


async def all_invoices(env) -> list[Invoice]:
    return sorted(await env.rows(Invoice), key=lambda i: i.id)


# --- numbering: sequence, concurrency, rollback, financial-year rollover ---------------------

async def alloc(pool, when: datetime, *, commit: bool = True) -> str | None:
    async with pool() as s:
        number, _, _ = await invoices.allocate_invoice_number(s, when, mode="live", prefix="ST24")
        if commit:
            await s.commit()
            return number
        await s.rollback()
        return None


async def test_numbers_are_sequential_and_formatted(pool):
    when = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

    got = [await alloc(pool, when) for _ in range(3)]

    assert got == ["ST24/2026-27/0001", "ST24/2026-27/0002", "ST24/2026-27/0003"]


async def test_25_concurrent_allocations_get_unique_gapless_numbers(pool):
    when = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

    got = await asyncio.gather(*[alloc(pool, when) for _ in range(25)])

    assert len(set(got)) == 25
    assert sorted(int(n.rsplit("/", 1)[1]) for n in got) == list(range(1, 25 + 1))
    async with pool() as s:
        assert await s.scalar(select(InvoiceCounter.last_seq).where(InvoiceCounter.financial_year == "2026-27")) == 25


async def test_rolled_back_allocations_leave_no_gap(pool):
    when = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

    results = await asyncio.gather(*[alloc(pool, when, commit=(i % 2 == 0)) for i in range(20)])

    committed = sorted(int(n.rsplit("/", 1)[1]) for n in results if n)
    assert committed == list(range(1, 11))          # 10 commits -> exactly 1..10, the rollbacks burned nothing
    async with pool() as s:
        assert await s.scalar(select(InvoiceCounter.last_seq)) == 10


async def test_financial_year_rollover_restarts_at_one_in_the_new_year(pool):
    last_ist_moment = datetime(2027, 3, 31, 18, 29, tzinfo=UTC)   # 23:59 IST on 31 Mar
    first_ist_moment = datetime(2027, 3, 31, 18, 30, tzinfo=UTC)  # 00:00 IST on 1 Apr

    a = await alloc(pool, datetime(2027, 3, 1, tzinfo=UTC))
    b = await alloc(pool, last_ist_moment)
    c = await alloc(pool, first_ist_moment)
    d = await alloc(pool, datetime(2027, 4, 2, tzinfo=UTC))
    e = await alloc(pool, datetime(2027, 3, 5, tzinfo=UTC))      # a late old-year invoice continues the OLD series

    assert (a, b) == ("ST24/2026-27/0001", "ST24/2026-27/0002")
    assert (c, d) == ("ST24/2027-28/0001", "ST24/2027-28/0002")
    assert e == "ST24/2026-27/0003"


async def test_allocations_across_two_financial_years_run_independently_under_concurrency(pool):
    old, new = datetime(2027, 3, 1, tzinfo=UTC), datetime(2027, 5, 1, tzinfo=UTC)

    got = await asyncio.gather(*[alloc(pool, old if i % 2 else new) for i in range(20)])

    by_year: dict[str, list[int]] = {}
    for n in got:
        _, fy, seq = n.split("/")
        by_year.setdefault(fy, []).append(int(seq))
    assert {fy: sorted(v) for fy, v in by_year.items()} == {"2026-27": list(range(1, 11)), "2027-28": list(range(1, 11))}


# --- invoices from real purchases ------------------------------------------------------------

async def test_a_purchase_creates_one_invoice_with_an_intra_state_split(env):
    client, h = await env.owner(business_name="Riya Sarees", gst_number=GUJARAT_GSTIN, business_address="12 MG Road, Surat")

    co = await env.buy(h, "starter_1500")

    [inv] = await all_invoices(env)
    order = await env.order(co["razorpay_order_id"])
    assert inv.invoice_number == f"TEST-ST24/{CURRENT_FY}/0001" and inv.financial_year == CURRENT_FY and inv.seq == 1
    assert (inv.client_id, inv.payment_order_id, inv.sac_code) == (client.id, order.id, "998314")
    assert inv.intra_state is True and inv.igst_paise == 0
    assert (inv.base_paise, inv.credit_paise, inv.taxable_paise) == (459900, 0, 459900)
    assert (inv.cgst_paise, inv.sgst_paise) == (41391, 41391)
    assert inv.total_paise == order.amount_paise == 542682
    assert inv.buyer_gstin == GUJARAT_GSTIN and inv.buyer_state_code == "24" and inv.buyer_address == "12 MG Road, Surat"
    assert (inv.seller_gstin, inv.seller_state_code, inv.razorpay_payment_id) == ("24AAAAA0000A1Z5", "24", order.razorpay_payment_id)
    assert inv.subscription_id is not None


async def test_a_client_with_no_gstin_is_treated_as_intra_state(env):
    _, h = await env.owner()

    await env.buy(h, "starter_1500")

    [inv] = await all_invoices(env)
    assert inv.intra_state is True and inv.buyer_gstin is None and inv.buyer_state_code is None
    assert inv.cgst_paise == inv.sgst_paise > 0 and inv.igst_paise == 0


async def test_another_state_gets_igst_only(env):
    _, h = await env.owner(gst_number=MAHARASHTRA_GSTIN)

    co = await env.buy(h, "growth_5000")

    [inv] = await all_invoices(env)
    assert inv.intra_state is False and inv.buyer_state_code == "27"
    assert (inv.cgst_paise, inv.sgst_paise, inv.igst_paise) == (0, 0, 215982)    # 18% of 11,999.00
    assert inv.total_paise == (await env.order(co["razorpay_order_id"])).amount_paise == 1415882


@pytest.mark.parametrize("plan_code", ["starter_1500", "growth_5000", "pro_9000"])
@pytest.mark.parametrize("gstin", [None, GUJARAT_GSTIN, MAHARASHTRA_GSTIN])
async def test_invoice_amounts_always_add_up_to_what_razorpay_charged(env, plan_code, gstin):
    _, h = await env.owner(gst_number=gstin)

    co = await env.buy(h, plan_code)

    [inv] = await all_invoices(env)
    gst = inv.cgst_paise + inv.sgst_paise + inv.igst_paise
    assert inv.taxable_paise + gst == inv.total_paise == co["amount"]
    assert gst == round_half_up_div(inv.taxable_paise * inv.gst_rate_bps, 10_000)
    assert (inv.igst_paise > 0) == (gstin == MAHARASHTRA_GSTIN) and (inv.cgst_paise > 0) == (gstin != MAHARASHTRA_GSTIN)
    assert abs(inv.cgst_paise - inv.sgst_paise) <= 1


async def test_an_upgrade_invoice_shows_the_credit_and_taxes_the_net_amount(env):
    _, h = await env.owner(gst_number=GUJARAT_GSTIN)
    await env.buy(h, "starter_1500")

    co = await env.buy(h, "growth_5000")

    first, second = await all_invoices(env)
    assert (first.seq, second.seq) == (1, 2)
    assert second.credit_paise > 0 and second.taxable_paise == second.base_paise - second.credit_paise
    assert second.total_paise == co["amount"]
    assert "upgrade" in second.description


async def test_invoice_is_a_snapshot_later_profile_edits_do_not_change_it(env):
    client, h = await env.owner(business_name="Old Name", gst_number=GUJARAT_GSTIN)
    await env.buy(h, "starter_1500")

    await env.session.execute(update(Client).where(Client.id == client.id).values(business_name="New Name", gst_number=MAHARASHTRA_GSTIN))
    await env.session.commit()

    [inv] = await all_invoices(env)
    assert (inv.buyer_name, inv.buyer_gstin, inv.intra_state) == ("Old Name", GUJARAT_GSTIN, True)


async def test_verify_plus_webhook_for_the_same_payment_issue_exactly_one_invoice_and_one_email(env, mailbox):
    client, h = await env.owner()
    co = await env.checkout(h, "starter_1500")
    order_id = co.json()["razorpay_order_id"]
    creds = env.paid_credentials(order_id)

    assert (await env.pay(h, order_id)).status_code == 200
    resp = await env.post_webhook(
        "payment.captured", order_id=order_id, payment_id=creds["razorpay_payment_id"], amount=co.json()["amount"],
    )

    assert resp.status_code == 200
    assert len(await all_invoices(env)) == 1
    assert await env.session.scalar(select(InvoiceCounter.last_seq)) == 1
    assert len(mailbox.to(client.email)) == 1


async def test_simultaneous_purchases_by_different_clients_get_distinct_numbers(env):
    _, h1 = await env.owner()
    _, h2 = await env.owner()
    o1 = (await env.checkout(h1, "starter_1500")).json()["razorpay_order_id"]
    o2 = (await env.checkout(h2, "pro_9000")).json()["razorpay_order_id"]

    r1, r2 = await asyncio.gather(env.pay(h1, o1), env.pay(h2, o2))

    assert r1.status_code == r2.status_code == 200
    numbers = sorted(i.invoice_number for i in await all_invoices(env))
    assert numbers == [f"TEST-ST24/{CURRENT_FY}/0001", f"TEST-ST24/{CURRENT_FY}/0002"]


async def test_activation_across_the_financial_year_boundary_numbers_each_year_separately(env):
    _, h1 = await env.owner()
    _, h2 = await env.owner()
    o1 = (await env.checkout(h1, "starter_1500")).json()["razorpay_order_id"]
    o2 = (await env.checkout(h2, "starter_1500")).json()["razorpay_order_id"]
    old_year = datetime(2027, 3, 31, 18, 29, tzinfo=UTC)
    new_year = datetime(2027, 3, 31, 18, 30, tzinfo=UTC)

    for order_id, when in ((o1, old_year), (o2, new_year)):
        creds = env.paid_credentials(order_id)
        await activate_from_payment(
            env.session, env.gateway, razorpay_order_id=order_id,
            razorpay_payment_id=creds["razorpay_payment_id"], source="verify", now=when,
        )

    a, b = await all_invoices(env)
    assert (a.invoice_number, b.invoice_number) == ("TEST-ST24/2026-27/0001", "TEST-ST24/2027-28/0001")
    assert (a.issued_at, b.issued_at) == (old_year, new_year)


async def test_an_invoice_failure_never_blocks_activation_and_can_be_issued_later(env, monkeypatch, mailbox):
    client, h = await env.owner()
    real = invoices.create_invoice

    async def boom(*a, **kw):
        raise RuntimeError("bad seller config")

    monkeypatch.setattr(invoices, "create_invoice", boom)
    co = await env.buy(h, "starter_1500")             # asserts checkout + payment both 200

    order = await env.order(co["razorpay_order_id"])
    assert order.status == "paid" and (await env.subs(client.id))[0].status == "active"
    assert await all_invoices(env) == []
    assert await env.session.scalar(select(func.count()).select_from(InvoiceCounter)) == 0   # no number burned
    [mail] = mailbox.to(client.email)
    assert mail.attachments == []                      # the customer still got the confirmation, minus the PDF

    monkeypatch.setattr(invoices, "create_invoice", real)
    issued = await invoices.ensure_invoice(env.session, order.id)
    assert issued.invoice_number == f"TEST-ST24/{CURRENT_FY}/0001"
    assert (await invoices.ensure_invoice(env.session, order.id)).id == issued.id     # idempotent


async def test_ensure_invoice_ignores_unpaid_and_unknown_orders(env):
    _, h = await env.owner()
    co = (await env.checkout(h, "starter_1500")).json()
    order = await env.order(co["razorpay_order_id"])

    assert await invoices.ensure_invoice(env.session, order.id) is None
    assert await invoices.ensure_invoice(env.session, 987654) is None


# --- PDF endpoint + payments list ------------------------------------------------------------

async def test_invoice_pdf_endpoint_and_payments_list_link(env):
    client, h = await env.owner(business_name="Riya Sarees")
    await env.buy(h, "starter_1500")

    item = (await env.http.get("/billing/payments", headers=h)).json()["items"][0]
    resp = await env.http.get(f"/billing/invoices/{item['invoice_id']}/pdf", headers=h)

    assert item["invoice_number"] == f"TEST-ST24/{CURRENT_FY}/0001"
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF-")
    assert f'filename="TEST-ST24-{CURRENT_FY}-0001.pdf"' in resp.headers["content-disposition"]


async def test_invoice_pdf_is_tenant_scoped_owner_only_and_needs_login(env):
    owner, h = await env.owner()
    _, other = await env.owner()
    staff = await env.staff(owner)
    await env.buy(h, "starter_1500")
    [inv] = await all_invoices(env)

    assert (await env.http.get(f"/billing/invoices/{inv.id}/pdf", headers=other)).status_code == 404
    assert (await env.http.get(f"/billing/invoices/{inv.id}/pdf", headers=staff)).status_code == 403
    assert (await env.http.get(f"/billing/invoices/{inv.id}/pdf")).status_code == 401
    assert (await env.http.get("/billing/invoices/99999/pdf", headers=h)).status_code == 404


async def test_payments_list_has_null_invoice_for_orders_without_one(env):
    client, h = await env.owner()
    await env.checkout(h, "starter_1500")             # created, never paid

    item = (await env.http.get("/billing/payments", headers=h)).json()["items"][0]

    assert item["invoice_id"] is None and item["invoice_number"] is None


# --- GSTIN validation on the profile ---------------------------------------------------------

async def test_profile_rejects_a_malformed_new_gstin_and_normalises_a_good_one(env):
    _, h = await env.owner()

    bad = await env.http.patch("/auth/me", json={"gst_number": "NOT-A-GSTIN"}, headers=h)
    good = await env.http.patch("/auth/me", json={"gst_number": " 27aapfu0939f1zv "}, headers=h)
    cleared = await env.http.patch("/auth/me", json={"gst_number": ""}, headers=h)

    assert bad.status_code == 422 and "GSTIN" in bad.json()["detail"]
    assert good.status_code == 200 and good.json()["gst_number"] == MAHARASHTRA_GSTIN
    assert cleared.status_code == 200 and cleared.json()["gst_number"] is None


async def test_resending_an_old_free_text_gstin_unchanged_still_saves(env):
    _, h = await env.owner(gst_number="legacy-free-text")

    resp = await env.http.patch("/auth/me", json={"gst_number": "legacy-free-text", "business_name": "X"}, headers=h)

    assert resp.status_code == 200


# --- emails ---------------------------------------------------------------------------------

async def test_payment_success_email_carries_the_invoice_pdf(env, mailbox):
    client, h = await env.owner(business_name="Riya Sarees")

    await env.buy(h, "growth_5000")

    [mail] = mailbox.to(client.email)
    [att] = mail.attachments
    assert "Growth" in mail.subject
    assert att.filename == f"TEST-ST24-{CURRENT_FY}-0001.pdf" and att.content.startswith(b"%PDF-")
    assert f"TEST-ST24/{CURRENT_FY}/0001" in mail.text and "Riya Sarees" in mail.text


async def test_payment_failed_email_goes_out_once_even_if_the_customer_keeps_failing(env, mailbox):
    client, h = await env.owner()
    co = (await env.checkout(h, "starter_1500")).json()
    failure = {"status": "failed", "error_description": "Card declined"}

    for n in (1, 2, 3):
        resp = await env.post_webhook(
            "payment.failed", order_id=co["razorpay_order_id"], payment_id=f"pay_fail_{n}", amount=co["amount"],
            event_id=f"evt_fail_{n}", extra=failure,
        )
        assert resp.status_code == 200

    [mail] = mailbox.to(client.email)
    assert "didn't go through" in mail.subject and "Card declined" in mail.text


async def test_no_failed_email_for_an_order_that_is_already_paid(env, mailbox):
    client, h = await env.owner()
    co = await env.buy(h, "starter_1500")
    mailbox.sent.clear()

    await env.post_webhook(
        "payment.failed", order_id=co["razorpay_order_id"], payment_id="pay_late_fail", amount=co["amount"], extra={"status": "failed"},
    )

    assert mailbox.to(client.email) == []


async def test_expiry_reminder_email_is_sent_once_per_period(env, mailbox):
    client, _ = await env.owner()
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    await env.seed_period(client.id, "starter_1500", start=now - timedelta(days=28))     # ends in 2 days

    await maintenance.run_maintenance(env.session, now)
    await maintenance.run_maintenance(env.session, now + timedelta(minutes=15))

    [mail] = mailbox.to(client.email)
    assert "expires in 3 days" in mail.subject and "Starter" in mail.text


async def test_grace_started_then_expired_emails_are_sent_once_each(env, mailbox):
    client, _ = await env.owner()
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    await env.session.execute(update(Client).where(Client.id == client.id).values(created_at=now - timedelta(days=1)))
    await env.session.commit()

    await maintenance.run_maintenance(env.session, now)                              # day 1: in grace
    await maintenance.run_maintenance(env.session, now + timedelta(minutes=15))      # same state: no repeat
    await maintenance.run_maintenance(env.session, now + timedelta(days=5))          # grace over

    subjects = [m.subject for m in mailbox.to(client.email)]
    assert subjects == ["Your SellerTalk24 plan has ended — grace period started", "Your SellerTalk24 plan has expired"]


async def test_a_failing_email_provider_never_breaks_activation(env, monkeypatch):
    class Boom:
        async def send(self, message, sender):
            raise ConnectionError("smtp down")

    monkeypatch.setattr(email_service, "get_email_provider", lambda settings=None: Boom())
    client, h = await env.owner()

    await env.buy(h, "starter_1500")

    assert (await env.subs(client.id))[0].status == "active" and len(await all_invoices(env)) == 1


async def test_billing_email_hooks_ignore_usage_alerts(env, mailbox):
    client, _ = await env.owner()

    assert await billing_emails.notify_alert(env.session, client.id, "usage_80", {"pct": 80}) is False
    assert mailbox.sent == []


# --- admin endpoints ------------------------------------------------------------------------

@pytest.mark.parametrize("method, path, body", [
    ("get", "/admin/billing/subscriptions", None),
    ("post", "/admin/billing/subscriptions/grant", {"client_id": 1, "plan_code": "starter_1500", "note": "cash"}),
    ("post", "/admin/billing/subscriptions/1/extend", {"days": 5, "note": "goodwill"}),
    ("put", "/admin/billing/clients/1/billing-exempt", {"exempt": True, "note": "house"}),
    ("get", "/admin/billing/payment-events", None),
    ("get", "/admin/billing/payment-events/1", None),
    ("post", "/admin/billing/payment-events/1/reprocess", None),
    ("post", "/admin/billing/invoices/backfill", None),
])
async def test_admin_billing_endpoints_require_the_admin_key(env, method, path, body):
    _, owner_headers = await env.owner()          # a client JWT must not open admin routes either

    for headers in ({}, {"X-Admin-Key": "wrong"}, owner_headers):
        resp = await getattr(env.http, method)(path, headers=headers, **({"json": body} if body else {}))
        assert resp.status_code == 401, (path, headers)


async def test_grant_starts_a_period_now_and_stacks_the_next_one(env):
    client, _ = await env.owner()

    first = await env.http.post("/admin/billing/subscriptions/grant", headers=admin_headers(),
                                json={"client_id": client.id, "plan_code": "growth_5000", "note": "cash received 7 Oct"})
    second = await env.http.post("/admin/billing/subscriptions/grant", headers=admin_headers(),
                                 json={"client_id": client.id, "plan_code": "growth_5000", "days": 15, "note": "bonus fortnight"})

    assert first.status_code == second.status_code == 201
    a, b = first.json(), second.json()
    assert (a["status"], a["plan_code"], a["conversation_limit"]) == ("active", "growth_5000", 5000)
    assert a["source_payment_id"] is not None                  # an offline grant is a paid ₹0 order now
    assert (datetime.fromisoformat(a["current_period_end"]) - datetime.fromisoformat(a["current_period_start"])).days == 30
    assert b["status"] == "pending" and b["current_period_start"] == a["current_period_end"]
    assert (datetime.fromisoformat(b["current_period_end"]) - datetime.fromisoformat(b["current_period_start"])).days == 15
    log = await env.rows(BillingAdminLog)
    assert sorted(r.action for r in log) == ["grant_subscription", "grant_subscription"]
    assert {r.detail["note"] for r in log} == {"cash received 7 Oct", "bonus fortnight"}


async def test_a_manual_grant_makes_the_client_active_for_entitlement(env):
    client, h = await env.owner()
    await env.http.post("/admin/billing/subscriptions/grant", headers=admin_headers(),
                        json={"client_id": client.id, "plan_code": "pro_9000", "note": "offline payment"})

    state = (await env.http.get("/billing/subscription", headers=h)).json()

    assert state["status"] == "active" and state["subscription"]["plan"]["code"] == "pro_9000"
    assert state["entitlement"]["state"] == "active"
    assert state["seller_state_code"] == "24"          # lets the dashboard say CGST+SGST vs IGST


async def test_grant_validates_client_plan_and_note(env):
    client, _ = await env.owner()
    url = "/admin/billing/subscriptions/grant"

    unknown_client = await env.http.post(url, headers=admin_headers(), json={"client_id": 99999, "plan_code": "starter_1500", "note": "cash"})
    unknown_plan = await env.http.post(url, headers=admin_headers(), json={"client_id": client.id, "plan_code": "nope", "note": "cash"})
    no_note = await env.http.post(url, headers=admin_headers(), json={"client_id": client.id, "plan_code": "starter_1500", "note": "x"})

    assert unknown_client.status_code == 404 and unknown_client.json()["detail"]["code"] == "client_not_found"
    assert unknown_plan.status_code == 404 and unknown_plan.json()["detail"]["code"] == "unknown_plan"
    assert no_note.status_code == 422
    assert await env.rows(ClientSubscription) == []


async def test_extend_moves_the_period_and_everything_queued_after_it(env):
    client, _ = await env.owner()
    first = (await env.http.post("/admin/billing/subscriptions/grant", headers=admin_headers(),
                                 json={"client_id": client.id, "plan_code": "starter_1500", "note": "first"})).json()
    queued = (await env.http.post("/admin/billing/subscriptions/grant", headers=admin_headers(),
                                  json={"client_id": client.id, "plan_code": "starter_1500", "note": "queued"})).json()

    resp = await env.http.post(f"/admin/billing/subscriptions/{first['id']}/extend", headers=admin_headers(),
                               json={"days": 10, "note": "service outage compensation"})

    assert resp.status_code == 200
    extended = resp.json()
    assert datetime.fromisoformat(extended["current_period_end"]) - datetime.fromisoformat(first["current_period_end"]) == timedelta(days=10)
    moved = next(s for s in await env.rows(ClientSubscription) if s.id == queued["id"])
    assert moved.current_period_start == datetime.fromisoformat(extended["current_period_end"])   # still back-to-back
    assert moved.current_period_end - moved.current_period_start == timedelta(days=30)
    assert any(r.action == "extend_subscription" and r.detail["shifted_periods"] == [queued["id"]] for r in await env.rows(BillingAdminLog))


async def test_extend_refuses_expired_and_unknown_subscriptions(env):
    client, _ = await env.owner()
    sub = await env.seed_period(client.id, "starter_1500", start=datetime.now(UTC) - timedelta(days=60), status="expired")

    expired = await env.http.post(f"/admin/billing/subscriptions/{sub.id}/extend", headers=admin_headers(), json={"days": 5, "note": "try"})
    unknown = await env.http.post("/admin/billing/subscriptions/99999/extend", headers=admin_headers(), json={"days": 5, "note": "try"})

    assert expired.status_code == 409 and expired.json()["detail"]["code"] == "not_extendable"
    assert unknown.status_code == 404


async def test_set_billing_exempt_flips_the_flag_and_is_audited(env):
    client, h = await env.owner()

    on = await env.http.put(f"/admin/billing/clients/{client.id}/billing-exempt", headers=admin_headers(), json={"exempt": True, "note": "partner account"})
    state_on = (await env.http.get("/billing/subscription", headers=h)).json()["entitlement"]["state"]
    off = await env.http.put(f"/admin/billing/clients/{client.id}/billing-exempt", headers=admin_headers(), json={"exempt": False, "note": "partnership ended"})
    missing = await env.http.put("/admin/billing/clients/99999/billing-exempt", headers=admin_headers(), json={"exempt": True, "note": "nope"})

    assert on.json() == {"client_id": client.id, "billing_exempt": True} and state_on == "exempt"
    assert off.json()["billing_exempt"] is False and missing.status_code == 404
    flips = [(r.detail["exempt"], r.detail["previous"]) for r in sorted(await env.rows(BillingAdminLog), key=lambda r: r.id)]
    assert flips == [(True, False), (False, True)]


async def test_subscription_list_filters_and_paginates(env):
    c1, _ = await env.owner()
    c2, _ = await env.owner()
    now = datetime.now(UTC)
    await env.seed_period(c1.id, "starter_1500", start=now - timedelta(days=5))
    await env.seed_period(c2.id, "pro_9000", start=now - timedelta(days=60), status="expired")
    await env.http.post("/admin/billing/subscriptions/grant", headers=admin_headers(), json={"client_id": c2.id, "plan_code": "growth_5000", "note": "cash"})

    everything = (await env.http.get("/admin/billing/subscriptions", headers=admin_headers())).json()
    only_c2 = (await env.http.get(f"/admin/billing/subscriptions?client_id={c2.id}", headers=admin_headers())).json()
    expired = (await env.http.get("/admin/billing/subscriptions?status=expired", headers=admin_headers())).json()
    page = (await env.http.get("/admin/billing/subscriptions?page=2&page_size=2", headers=admin_headers())).json()

    assert everything["total"] == 3 and {i["plan_code"] for i in everything["items"]} == {"starter_1500", "pro_9000", "growth_5000"}
    assert only_c2["total"] == 2 and {i["client_email"] for i in only_c2["items"]} == {c2.email}
    assert [i["plan_code"] for i in expired["items"]] == ["pro_9000"]
    assert page["total"] == 3 and len(page["items"]) == 1 and page["page"] == 2
    manual = next(i for i in everything["items"] if i["plan_code"] == "growth_5000")
    offline = (await env.rows(PaymentOrder, PaymentOrder.id == manual["source_payment_id"]))[0]
    assert offline.razorpay_order_id.startswith("offline_") and offline.amount_paise == 0 and manual["business_name"] is not None


async def test_payment_events_list_shows_errors_and_reprocess_recovers_a_failed_activation(env):
    gateway = ScriptedGateway()
    env.gateway = gateway
    client, h = await env.owner()
    co = (await env.checkout(h, "starter_1500")).json()
    creds = env.paid_credentials(co["razorpay_order_id"])
    gateway.fail = True                                      # Razorpay API outage while the webhook is handled

    hook = await env.post_webhook(
        "payment.captured", order_id=co["razorpay_order_id"], payment_id=creds["razorpay_payment_id"], amount=co["amount"], event_id="evt_outage",
    )

    assert hook.status_code == 200                           # webhooks always answer 200 once persisted
    errors = (await env.http.get("/admin/billing/payment-events?has_error=true", headers=admin_headers())).json()
    [bad] = errors["items"]
    assert bad["razorpay_event_id"] == "evt_outage" and bad["processed"] is False and "gateway_error" in bad["error"]
    assert bad["payload"] is None                            # list view omits the raw payload
    detail = (await env.http.get(f"/admin/billing/payment-events/{bad['id']}", headers=admin_headers())).json()
    assert detail["payload"]["event"] == "payment.captured"
    assert (await env.order(co["razorpay_order_id"])).status != "paid"

    gateway.fail = False                                     # outage over
    redo = await env.http.post(f"/admin/billing/payment-events/{bad['id']}/reprocess", headers=admin_headers())

    assert redo.status_code == 200 and redo.json() == {"outcome": "ok", "processed": True, "error": None}
    assert (await env.order(co["razorpay_order_id"])).status == "paid"
    assert (await env.subs(client.id))[0].status == "active" and len(await all_invoices(env)) == 1
    assert (await env.http.get("/admin/billing/payment-events?has_error=true", headers=admin_headers())).json()["total"] == 0
    again = await env.http.post(f"/admin/billing/payment-events/{bad['id']}/reprocess", headers=admin_headers())
    assert again.status_code == 409 and again.json()["detail"]["code"] == "already_processed"
    assert any(r.action == "reprocess_payment_event" for r in await env.rows(BillingAdminLog))


async def test_payment_event_filters_and_unknown_ids(env):
    await env.post_webhook("some.other.event", event_id="evt_a")
    await env.post_webhook("payment.captured", order_id="order_unknown_xyz", payment_id="pay_x", amount=100, event_id="evt_b")

    all_events = (await env.http.get("/admin/billing/payment-events", headers=admin_headers())).json()
    captured = (await env.http.get("/admin/billing/payment-events?event_type=payment.captured", headers=admin_headers())).json()
    unprocessed = (await env.http.get("/admin/billing/payment-events?processed=false", headers=admin_headers())).json()

    assert all_events["total"] == 2 and captured["total"] == 1 and unprocessed["total"] == 0
    assert (await env.http.get("/admin/billing/payment-events/99999", headers=admin_headers())).status_code == 404
    assert (await env.http.post("/admin/billing/payment-events/99999/reprocess", headers=admin_headers())).status_code == 404


async def test_backfill_issues_invoices_for_old_paid_orders_in_payment_order(env):
    c1, _ = await env.owner()
    c2, _ = await env.owner()
    now = datetime.now(UTC)
    await env.seed_period(c2.id, "starter_1500", start=now - timedelta(days=10))      # paid later
    await env.seed_period(c1.id, "growth_5000", start=now - timedelta(days=20))       # paid earlier

    resp = await env.http.post("/admin/billing/invoices/backfill", headers=admin_headers())
    again = await env.http.post("/admin/billing/invoices/backfill", headers=admin_headers())

    assert resp.json()["created"] == 2 and again.json() == {"created": 0, "invoice_numbers": []}
    by_client = {i.client_id: i.invoice_number for i in await all_invoices(env)}
    assert by_client[c1.id].endswith("/0001") and by_client[c2.id].endswith("/0002")     # earliest payment first
    assert await env.session.scalar(select(func.count()).select_from(PaymentEvent)) == 0
