"""
SellerTalk24 billing schema (migration 0062) against real Postgres.

Proves the guarantees the billing engine will lean on: the idempotent plan seed,
one-active-subscription-per-client, webhook-event and order uniqueness, the
conversation-window dedupe (ON CONFLICT DO NOTHING), and the CHECK constraints.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.models.client import Client
from app.models.sellertalk24_billing import (
    BillingPlan,
    ClientSubscription,
    ConversationUsageLog,
    PaymentEvent,
    PaymentOrder,
)
from tests.replay.conftest import BACKEND_DIR, seed_client_and_product

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)


async def _plans(session) -> dict[str, BillingPlan]:
    """Seeded plans keyed by code."""
    rows = (await session.execute(select(BillingPlan))).scalars().all()
    return {p.code: p for p in rows}


async def _client_id(session, phone: str = "919100000001") -> int:
    """Create a minimal client and return its id."""
    client, _ = await seed_client_and_product(
        session, phone=phone, wa_phone_number_id=f"PN{phone}"
    )
    return client.id


def _sub(client_id: int, plan: BillingPlan, status: str, start_days: int = 0) -> ClientSubscription:
    """A 30-day subscription starting *start_days* from NOW."""
    start = NOW + timedelta(days=start_days)
    return ClientSubscription(
        client_id=client_id, plan_id=plan.id, status=status,
        current_period_start=start, current_period_end=start + timedelta(days=30),
        conversation_limit=plan.conversation_limit,
    )


def _order(client_id: int, plan_id: int, n: int, **kw) -> PaymentOrder:
    """A payment order for *plan_id* with unique order id / receipt number *n*."""
    base = dict(
        client_id=client_id, plan_id=plan_id, razorpay_order_id=f"order_{n}",
        receipt=f"st24_{client_id}_{n}", amount_paise=542682, base_paise=459900, gst_paise=82782,
    )
    base.update(kw)
    return PaymentOrder(**base)


# --- seed ---------------------------------------------------------------------

async def test_seeded_plans_have_spec_prices_limits_and_features(replay_session):
    plans = await _plans(replay_session)

    assert set(plans) == {"starter_1500", "growth_5000", "pro_9000"}
    got = {c: (p.name, p.conversation_limit, p.price_paise, p.sort_order) for c, p in plans.items()}
    assert got == {
        "starter_1500": ("Starter", 1500, 459900, 1),
        "growth_5000": ("Growth", 5000, 1199900, 2),
        "pro_9000": ("Pro", 9000, 1799900, 3),
    }
    assert plans["starter_1500"].features == {"whatsapp": True, "instagram": False}
    assert plans["growth_5000"].features == {"whatsapp": True, "instagram": True}
    assert plans["pro_9000"].features == {"whatsapp": True, "instagram": True}
    assert all(p.currency == "INR" and p.billing_period_days == 30 and p.is_active for p in plans.values())


async def test_seed_upsert_is_idempotent_and_corrects_drifted_values(replay_session):
    path = Path(BACKEND_DIR) / "alembic/versions/0062_sellertalk24_billing_tables.py"
    spec = importlib.util.spec_from_file_location("mig0062", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    await replay_session.execute(text("UPDATE billing_plans SET price_paise = 1 WHERE code = 'pro_9000'"))
    await replay_session.commit()
    conn = await replay_session.connection()
    await conn.run_sync(lambda sync_conn: mig._seed_plans(sync_conn))
    await conn.run_sync(lambda sync_conn: mig._seed_plans(sync_conn))
    await replay_session.commit()

    count = (await replay_session.execute(select(func.count()).select_from(BillingPlan))).scalar_one()
    price = (await replay_session.execute(
        select(BillingPlan.price_paise).where(BillingPlan.code == "pro_9000")
    )).scalar_one()
    assert count == 3
    assert price == 1799900


async def test_plan_code_is_unique_and_checks_reject_bad_values(replay_session):
    for kw in (
        dict(code="starter_1500"),                      # duplicate code
        dict(code="x1", price_paise=-1),                # negative price
        dict(code="x2", conversation_limit=0),          # non-positive limit
        dict(code="x3", billing_period_days=0),         # non-positive period
    ):
        fields = dict(code="n", name="N", conversation_limit=10, price_paise=100, features={})
        fields.update(kw)
        replay_session.add(BillingPlan(**fields))
        with pytest.raises(IntegrityError):
            await replay_session.commit()
        await replay_session.rollback()


# --- client_subscriptions -------------------------------------------------------

async def test_only_one_active_subscription_per_client(replay_session):
    plans = await _plans(replay_session)
    cid = await _client_id(replay_session)
    replay_session.add(_sub(cid, plans["starter_1500"], "active"))
    await replay_session.commit()

    replay_session.add(_sub(cid, plans["growth_5000"], "active"))
    with pytest.raises(IntegrityError):
        await replay_session.commit()
    await replay_session.rollback()


async def test_non_active_rows_and_other_clients_can_coexist_with_an_active_row(replay_session):
    plans = await _plans(replay_session)
    a = await _client_id(replay_session, "919100000001")
    b = await _client_id(replay_session, "919100000002")

    replay_session.add_all([
        _sub(a, plans["starter_1500"], "active"),
        _sub(a, plans["starter_1500"], "pending", start_days=30),
        _sub(a, plans["starter_1500"], "expired", start_days=-60),
        _sub(a, plans["starter_1500"], "superseded", start_days=-30),
        _sub(a, plans["starter_1500"], "cancelled", start_days=-90),
        _sub(b, plans["growth_5000"], "active"),
    ])
    await replay_session.commit()

    n = (await replay_session.execute(select(func.count()).select_from(ClientSubscription))).scalar_one()
    assert n == 6


async def test_subscription_rejects_bad_status_inverted_period_and_negative_counters(replay_session):
    plans = await _plans(replay_session)
    cid = await _client_id(replay_session)
    bad_status = _sub(cid, plans["starter_1500"], "bogus")
    inverted = _sub(cid, plans["starter_1500"], "pending")
    inverted.current_period_end = inverted.current_period_start
    negative = _sub(cid, plans["starter_1500"], "pending")
    negative.conversations_used = -1

    for bad in (bad_status, inverted, negative):
        replay_session.add(bad)
        with pytest.raises(IntegrityError):
            await replay_session.commit()
        await replay_session.rollback()


async def test_subscription_defaults_and_source_payment_link(replay_session):
    plans = await _plans(replay_session)
    cid = await _client_id(replay_session)
    order = _order(cid, plans["growth_5000"].id, 1, purpose="upgrade")
    replay_session.add(order)
    await replay_session.flush()
    sub = _sub(cid, plans["growth_5000"], "active")
    sub.source_payment_id = order.id
    replay_session.add(sub)
    await replay_session.commit()
    await replay_session.refresh(sub)

    assert sub.conversations_used == 0 and sub.credited_paise == 0
    assert sub.source_payment_id == order.id
    assert sub.created_at is not None and sub.updated_at is not None


# --- payment_orders -----------------------------------------------------------

async def test_payment_order_defaults_and_multiple_unpaid_orders_with_null_payment_id(replay_session):
    plans = await _plans(replay_session)
    cid = await _client_id(replay_session)
    replay_session.add_all([_order(cid, plans["starter_1500"].id, 1), _order(cid, plans["starter_1500"].id, 2)])
    await replay_session.commit()

    orders = (await replay_session.execute(select(PaymentOrder).order_by(PaymentOrder.id))).scalars().all()
    assert [o.razorpay_payment_id for o in orders] == [None, None]
    assert all(o.status == "created" and o.purpose == "new" and o.currency == "INR" for o in orders)
    assert all(o.credit_paise == o.taxable_paise == o.cgst_paise == o.sgst_paise == o.igst_paise == 0 for o in orders)


async def test_payment_order_uniqueness_on_order_id_payment_id_and_receipt(replay_session):
    plans = await _plans(replay_session)
    cid = await _client_id(replay_session)
    pid = plans["starter_1500"].id
    replay_session.add(_order(cid, pid, 1, razorpay_payment_id="pay_1"))
    await replay_session.commit()

    for kw in (
        dict(razorpay_order_id="order_1", receipt="r_other"),                   # dup order id
        dict(razorpay_order_id="order_9", receipt="st24_%d_1" % cid),            # dup receipt
        dict(razorpay_order_id="order_9", receipt="r_9", razorpay_payment_id="pay_1"),  # dup payment id
    ):
        replay_session.add(_order(cid, pid, 9, **kw))
        with pytest.raises(IntegrityError):
            await replay_session.commit()
        await replay_session.rollback()


async def test_payment_order_rejects_negative_amounts_bad_status_and_bad_purpose(replay_session):
    plans = await _plans(replay_session)
    cid = await _client_id(replay_session)
    pid = plans["starter_1500"].id
    for n, kw in enumerate((dict(amount_paise=-1), dict(cgst_paise=-1), dict(status="bogus"), dict(purpose="bogus")), 1):
        replay_session.add(_order(cid, pid, n, **kw))
        with pytest.raises(IntegrityError):
            await replay_session.commit()
        await replay_session.rollback()


async def test_payment_order_stores_money_as_exact_integers_and_raw_webhook_json(replay_session):
    plans = await _plans(replay_session)
    cid = await _client_id(replay_session)
    replay_session.add(_order(
        cid, plans["growth_5000"].id, 1, base_paise=1199900, credit_paise=100001, taxable_paise=1099899,
        cgst_paise=98991, sgst_paise=98991, igst_paise=0, gst_paise=197982, amount_paise=1297881,
        raw_webhook={"event": "payment.captured"},
    ))
    await replay_session.commit()

    o = (await replay_session.execute(select(PaymentOrder))).scalar_one()
    assert isinstance(o.amount_paise, int) and o.amount_paise == 1297881
    assert o.cgst_paise + o.sgst_paise + o.igst_paise == o.gst_paise
    assert o.taxable_paise + o.gst_paise == o.amount_paise
    assert o.raw_webhook == {"event": "payment.captured"}


# --- payment_events -----------------------------------------------------------

async def test_payment_event_id_is_unique_so_replayed_webhooks_are_rejected(replay_session):
    replay_session.add(PaymentEvent(razorpay_event_id="evt_1", event_type="payment.captured", payload={}))
    await replay_session.commit()

    replay_session.add(PaymentEvent(razorpay_event_id="evt_1", event_type="payment.captured", payload={}))
    with pytest.raises(IntegrityError):
        await replay_session.commit()
    await replay_session.rollback()


async def test_payment_event_insert_on_conflict_do_nothing_claims_event_exactly_once(replay_session):
    stmt = (
        pg_insert(PaymentEvent)
        .values(razorpay_event_id="evt_2", event_type="order.paid", payload={"a": 1})
        .on_conflict_do_nothing(index_elements=["razorpay_event_id"])
    )
    first = await replay_session.execute(stmt)
    second = await replay_session.execute(stmt)
    await replay_session.commit()

    assert (first.rowcount, second.rowcount) == (1, 0)
    ev = (await replay_session.execute(select(PaymentEvent))).scalar_one()
    assert ev.processed is False and ev.processed_at is None and ev.error is None


# --- conversation_usage_log ---------------------------------------------------

async def test_usage_window_dedupe_counts_a_customer_window_once(replay_session):
    cid = await _client_id(replay_session)
    stmt = (
        pg_insert(ConversationUsageLog)
        .values(client_id=cid, customer_key="919999", channel="whatsapp", window_started_at=NOW)
        .on_conflict_do_nothing(constraint="uq_sellertalk24_conversation_usage_window")
    )
    first = await replay_session.execute(stmt)
    second = await replay_session.execute(stmt)
    await replay_session.commit()

    assert (first.rowcount, second.rowcount) == (1, 0)


async def test_usage_window_is_distinct_per_window_channel_and_customer(replay_session):
    cid = await _client_id(replay_session)
    base = dict(client_id=cid, customer_key="919999", channel="whatsapp", window_started_at=NOW)
    replay_session.add_all([
        ConversationUsageLog(**base),
        ConversationUsageLog(**{**base, "window_started_at": NOW + timedelta(hours=25)}),  # next window
        ConversationUsageLog(**{**base, "channel": "instagram"}),                            # other channel
        ConversationUsageLog(**{**base, "customer_key": "918888"}),                           # other customer
    ])
    await replay_session.commit()

    n = (await replay_session.execute(select(func.count()).select_from(ConversationUsageLog))).scalar_one()
    assert n == 4


async def test_usage_log_rejects_bad_channel_and_allows_no_subscription(replay_session):
    cid = await _client_id(replay_session)
    replay_session.add(ConversationUsageLog(
        client_id=cid, customer_key="x", channel="sms", window_started_at=NOW))
    with pytest.raises(IntegrityError):
        await replay_session.commit()
    await replay_session.rollback()

    replay_session.add(ConversationUsageLog(
        client_id=cid, customer_key="x", channel="website", window_started_at=NOW))  # trial/grace: no sub
    await replay_session.commit()
    row = (await replay_session.execute(select(ConversationUsageLog))).scalar_one()
    assert row.subscription_id is None


async def test_deleting_a_subscription_keeps_its_usage_rows(replay_session):
    plans = await _plans(replay_session)
    cid = await _client_id(replay_session)
    sub = _sub(cid, plans["starter_1500"], "pending")
    replay_session.add(sub)
    await replay_session.flush()
    replay_session.add(ConversationUsageLog(
        client_id=cid, subscription_id=sub.id, customer_key="x", channel="whatsapp", window_started_at=NOW))
    await replay_session.commit()

    await replay_session.delete(sub)
    await replay_session.commit()

    row = (await replay_session.execute(select(ConversationUsageLog))).scalar_one()
    assert row.subscription_id is None


# --- migration 0063: over_limit, billing_exempt, billing_alerts -------------------------------

async def test_new_subscriptions_default_to_not_over_limit_and_new_clients_to_not_exempt(replay_session):
    plans = await _plans(replay_session)
    cid = await _client_id(replay_session)
    replay_session.add(_sub(cid, plans["starter_1500"], "active"))
    await replay_session.commit()

    sub = (await replay_session.execute(select(ClientSubscription))).scalar_one()
    client = (await replay_session.execute(select(Client).where(Client.id == cid))).scalar_one()

    assert sub.over_limit is False
    assert client.billing_exempt is False


async def test_billing_alert_is_unique_per_client_and_dedupe_key(replay_session):
    from app.models.sellertalk24_billing import BillingAlert

    a = await _client_id(replay_session, "919100000011")
    b = await _client_id(replay_session, "919100000012")
    mk = lambda cid, key: BillingAlert(client_id=cid, kind="expired", dedupe_key=key, title="t", message="m")  # noqa: E731
    replay_session.add_all([mk(a, "k"), mk(b, "k"), mk(a, "k2")])          # same key for another client / another key: fine
    await replay_session.commit()

    replay_session.add(mk(a, "k"))
    with pytest.raises(IntegrityError):
        await replay_session.commit()
    await replay_session.rollback()

    alert = (await replay_session.execute(select(BillingAlert).where(BillingAlert.client_id == b))).scalar_one()
    assert alert.severity == "info" and alert.params == {} and alert.read_at is None and alert.created_at is not None


async def test_deleting_a_subscription_keeps_its_alerts(replay_session):
    from app.models.sellertalk24_billing import BillingAlert

    plans = await _plans(replay_session)
    cid = await _client_id(replay_session)
    sub = _sub(cid, plans["starter_1500"], "pending")
    replay_session.add(sub)
    await replay_session.flush()
    replay_session.add(BillingAlert(client_id=cid, subscription_id=sub.id, kind="usage_80", dedupe_key="k", title="t", message="m"))
    await replay_session.commit()

    await replay_session.delete(sub)
    await replay_session.commit()

    assert (await replay_session.execute(select(BillingAlert))).scalar_one().subscription_id is None
