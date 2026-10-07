"""
SellerTalk24 plan enforcement — real Postgres, the real FastAPI app and the real message pipeline.

Covers: conversation-window counting (dedupe, new window, concurrency), soft-cap alerts, the expiry /
renewal / reminder / grace maintenance job (incl. the advisory lock), the entitlement gate
(order-in-progress, exempt client, human takeover, enforcement flag), the send gate, and the full
WhatsApp webhook path.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from itertools import count
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.client import Client
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.order import Order
from app.models.sellertalk24_billing import (
    BillingAlert,
    BillingPlan,
    ClientSubscription,
    ConversationUsageLog,
    PaymentOrder,
)
from app.models.user import User
from app.services import conversation_service, send_gate, whatsapp_service
from app.services.auth_service import create_access_token
from app.services.billing import alerts as alert_service
from app.services.billing import entitlement, maintenance, usage
from app.services.billing.entitlement import EntitlementState
from app.services.language_templates import get_template
from tests.replay.conftest import seed_client_and_product
from tests.replay.helpers import send_message

pytestmark = pytest.mark.asyncio

_n = count(1)
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
FALLBACK = get_template("english", "billing_assistant_unavailable")


@pytest.fixture(autouse=True)
def _billing_settings(monkeypatch):
    """Enforcement ON, 3-day grace, legacy counter ON (the production-intent configuration)."""
    from app.config import get_settings

    pinned = get_settings().model_copy(update=dict(
        sellertalk24_billing_enforce=True, grace_days=3, billing_legacy_counting=True,
    ))
    monkeypatch.setattr(entitlement, "get_settings", lambda: pinned)
    monkeypatch.setattr(maintenance, "get_settings", lambda: pinned)
    return pinned


@pytest_asyncio.fixture
async def restore_plans(replay_session):
    """billing_plans survives the per-test TRUNCATE; nothing here edits it, but keep it pristine."""
    yield
    await replay_session.rollback()


async def new_client(session, *, age_days: int = 0, exempt: bool = False) -> SimpleNamespace:
    """Seed a client (created *age_days* ago) with its own WhatsApp number; return a plain snapshot."""
    n = next(_n)
    client, _ = await seed_client_and_product(session, phone=f"91770{n:05d}", wa_phone_number_id=f"PNX{n}")
    client.billing_exempt = exempt
    if age_days:
        client.created_at = datetime.now(timezone.utc) - timedelta(days=age_days)
    session.add(User(client_id=client.id, email=client.email, role="owner", permissions=[], is_active=True))
    await session.commit()
    return SimpleNamespace(id=client.id, email=client.email, pnid=f"PNX{n}", n=n)


async def plan(session, code="starter_1500") -> BillingPlan:
    """A seeded billing plan."""
    return (await session.execute(select(BillingPlan).where(BillingPlan.code == code))).scalar_one()


async def seed_sub(session, client_id, code="starter_1500", *, start, status="active", limit=None, used=0):
    """Insert an already-paid 30-day period with its source order; return the subscription row."""
    p = await plan(session, code)
    n = next(_n)
    order = PaymentOrder(
        client_id=client_id, plan_id=p.id, razorpay_order_id=f"order_e_{client_id}_{n}", receipt=f"e_{client_id}_{n}",
        amount_paise=p.price_paise, base_paise=p.price_paise, taxable_paise=p.price_paise, status="paid",
        purpose="new", paid_at=start, razorpay_payment_id=f"pay_e_{client_id}_{n}",
    )
    session.add(order)
    await session.flush()
    sub = ClientSubscription(
        client_id=client_id, plan_id=p.id, status=status, current_period_start=start,
        current_period_end=start + timedelta(days=30), conversation_limit=limit or p.conversation_limit,
        conversations_used=used, source_payment_id=order.id,
    )
    session.add(sub)
    await session.commit()
    return sub


async def fresh(session, model, *where):
    """Rows of *model*, refreshed from the DB."""
    stmt = select(model).execution_options(populate_existing=True)
    for w in where:
        stmt = stmt.where(w)
    return list((await session.execute(stmt)).scalars().all())


async def used_of(session, sub_id) -> int:
    """conversations_used of a subscription, straight from the DB."""
    return (await session.execute(select(ClientSubscription.conversations_used).where(ClientSubscription.id == sub_id))).scalar_one()


async def n_logs(session, client_id) -> int:
    """Number of usage-log rows for a client."""
    return (await session.execute(select(func.count()).select_from(ConversationUsageLog).where(ConversationUsageLog.client_id == client_id))).scalar_one()


# ===== counting ===============================================================================

async def test_first_message_opens_a_window_and_counts_once(replay_session):
    c = await new_client(replay_session)
    sub = await seed_sub(replay_session, c.id, start=NOW - timedelta(days=1))

    rec = await usage.track_inbound_conversation(replay_session, c.id, "whatsapp", "9190001", NOW)

    assert rec.inserted and (rec.subscription_id, rec.used, rec.limit) == (sub.id, 1, 1500)
    assert await used_of(replay_session, sub.id) == 1
    (log,) = await fresh(replay_session, ConversationUsageLog)
    assert (log.client_id, log.channel, log.customer_key, log.subscription_id, log.window_started_at) == (c.id, "whatsapp", "9190001", sub.id, NOW)


async def test_messages_inside_the_same_window_do_not_count_again(replay_session):
    c = await new_client(replay_session)
    sub = await seed_sub(replay_session, c.id, start=NOW - timedelta(days=1))
    await usage.track_inbound_conversation(replay_session, c.id, "whatsapp", "9190002", NOW)

    results = [
        await usage.track_inbound_conversation(replay_session, c.id, "whatsapp", "9190002", NOW + d)
        for d in (timedelta(seconds=5), timedelta(hours=1), timedelta(hours=23, minutes=59))
    ]

    assert results == [None, None, None]
    assert await used_of(replay_session, sub.id) == 1 and await n_logs(replay_session, c.id) == 1


async def test_a_message_after_24_hours_opens_a_new_window_and_increments(replay_session):
    c = await new_client(replay_session)
    sub = await seed_sub(replay_session, c.id, start=NOW - timedelta(days=1))
    await usage.track_inbound_conversation(replay_session, c.id, "whatsapp", "9190003", NOW)

    rec = await usage.track_inbound_conversation(replay_session, c.id, "whatsapp", "9190003", NOW + timedelta(hours=24))

    assert rec.inserted and rec.used == 2
    assert await used_of(replay_session, sub.id) == 2 and await n_logs(replay_session, c.id) == 2
    # and the new window is now the open one
    assert await usage.track_inbound_conversation(replay_session, c.id, "whatsapp", "9190003", NOW + timedelta(hours=30)) is None


async def test_windows_are_per_channel_and_per_customer(replay_session):
    c = await new_client(replay_session)
    sub = await seed_sub(replay_session, c.id, start=NOW - timedelta(days=1))

    for channel, key in [("whatsapp", "A"), ("instagram", "A"), ("whatsapp", "B")]:
        assert (await usage.track_inbound_conversation(replay_session, c.id, channel, key, NOW)).inserted

    assert await used_of(replay_session, sub.id) == 3


async def test_the_other_clients_windows_are_independent(replay_session):
    a, b = await new_client(replay_session), await new_client(replay_session)
    sub_a = await seed_sub(replay_session, a.id, start=NOW - timedelta(days=1))
    sub_b = await seed_sub(replay_session, b.id, start=NOW - timedelta(days=1))

    await usage.track_inbound_conversation(replay_session, a.id, "whatsapp", "SAME", NOW)
    await usage.track_inbound_conversation(replay_session, b.id, "whatsapp", "SAME", NOW)

    assert (await used_of(replay_session, sub_a.id), await used_of(replay_session, sub_b.id)) == (1, 1)


async def test_record_conversation_is_idempotent_for_the_same_window_start(replay_session):
    c = await new_client(replay_session)
    sub = await seed_sub(replay_session, c.id, start=NOW - timedelta(days=1))

    first = await usage.record_conversation(replay_session, c.id, "whatsapp", "X", NOW, NOW)
    second = await usage.record_conversation(replay_session, c.id, "whatsapp", "X", NOW, NOW)
    await replay_session.commit()

    assert (first.inserted, second.inserted) == (True, False)
    assert await used_of(replay_session, sub.id) == 1


async def test_concurrent_first_messages_from_one_customer_count_exactly_once(replay_db_url, replay_session):
    c = await new_client(replay_session)
    sub = await seed_sub(replay_session, c.id, start=NOW - timedelta(days=1))
    engine = create_async_engine(replay_db_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def one(i):
        async with factory() as s:
            # distinct timestamps: the unique key alone could NOT dedupe these
            return await usage.track_inbound_conversation(s, c.id, "whatsapp", "RACE", NOW + timedelta(milliseconds=i))

    results = await asyncio.gather(*[one(i) for i in range(12)])
    await engine.dispose()

    assert sum(1 for r in results if r is not None) == 1
    assert await used_of(replay_session, sub.id) == 1 and await n_logs(replay_session, c.id) == 1


async def test_concurrent_different_customers_all_count_atomically(replay_db_url, replay_session):
    c = await new_client(replay_session)
    sub = await seed_sub(replay_session, c.id, start=NOW - timedelta(days=1))
    engine = create_async_engine(replay_db_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def one(i):
        async with factory() as s:
            return await usage.track_inbound_conversation(s, c.id, "whatsapp", f"cust{i}", NOW)

    await asyncio.gather(*[one(i) for i in range(20)])
    await engine.dispose()

    assert await used_of(replay_session, sub.id) == 20            # no lost updates (used = used + 1 in SQL)


async def test_no_running_subscription_logs_the_window_but_increments_nothing(replay_session):
    c = await new_client(replay_session)
    expired = await seed_sub(replay_session, c.id, start=NOW - timedelta(days=45))                   # lapsed
    queued = await seed_sub(replay_session, c.id, start=NOW + timedelta(days=5), status="pending")   # not started

    rec = await usage.track_inbound_conversation(replay_session, c.id, "whatsapp", "9190009", NOW)

    assert rec.inserted and rec.subscription_id is None
    assert await used_of(replay_session, expired.id) == 0 and await used_of(replay_session, queued.id) == 0
    (log,) = await fresh(replay_session, ConversationUsageLog)
    assert log.subscription_id is None


async def test_counting_failure_is_swallowed_and_the_session_stays_usable(replay_session, monkeypatch, caplog):
    c = await new_client(replay_session)
    sub = await seed_sub(replay_session, c.id, start=NOW - timedelta(days=1))
    monkeypatch.setattr(usage, "record_conversation", AsyncMock(side_effect=RuntimeError("boom")))

    with caplog.at_level(logging.ERROR):
        result = await usage.track_inbound_conversation(replay_session, c.id, "whatsapp", "9190010", NOW)

    assert result is None and "counting failed" in caplog.text
    assert await used_of(replay_session, sub.id) == 0 and await n_logs(replay_session, c.id) == 0
    monkeypatch.undo()
    assert (await usage.track_inbound_conversation(replay_session, c.id, "whatsapp", "9190010", NOW)).inserted   # recovers


# ===== soft cap + alerts ======================================================================

async def test_threshold_alerts_fire_once_each_and_over_limit_flags_but_never_blocks(replay_session):
    c = await new_client(replay_session)
    sub = await seed_sub(replay_session, c.id, start=NOW - timedelta(days=1), limit=10)

    flags = []
    for i in range(1, 16):
        rec = await usage.track_inbound_conversation(replay_session, c.id, "whatsapp", f"cust{i}", NOW)
        assert rec.inserted and rec.used == i                                # every conversation still counted
        row = (await fresh(replay_session, ClientSubscription, ClientSubscription.id == sub.id))[0]
        flags.append(row.over_limit)
        kinds = sorted(a.kind for a in await fresh(replay_session, BillingAlert, BillingAlert.client_id == c.id))
        if i == 7:
            assert kinds == []
        if i == 8:
            assert kinds == ["usage_80"]
        if i == 10:
            assert kinds == ["usage_100", "usage_80"]
        if i == 12:
            assert kinds == ["usage_100", "usage_120", "usage_80"]

    assert flags == [False] * 10 + [True] * 5                                # over_limit = used > limit: from the 11th on (the 100% alert fired at the 10th)
    alerts = await fresh(replay_session, BillingAlert, BillingAlert.client_id == c.id)
    assert len(alerts) == 3                                                  # 13..15 added nothing
    by_kind = {a.kind: a for a in alerts}
    assert by_kind["usage_80"].dedupe_key == f"usage_80:{sub.id}" and by_kind["usage_80"].subscription_id == sub.id
    assert by_kind["usage_80"].params["used"] == 8 and by_kind["usage_100"].params["limit"] == 10
    assert (by_kind["usage_80"].severity, by_kind["usage_120"].severity) == ("warning", "critical")
    assert by_kind["usage_80"].read_at is None


async def test_alerts_fire_again_for_a_new_period(replay_session):
    c = await new_client(replay_session)
    old = await seed_sub(replay_session, c.id, start=NOW - timedelta(days=40), limit=10)
    for i in range(8):
        await usage.track_inbound_conversation(replay_session, c.id, "whatsapp", f"a{i}", NOW - timedelta(days=35))  # inside old period
    assert len(await fresh(replay_session, BillingAlert, BillingAlert.client_id == c.id)) in (0, 1)

    await replay_session.execute(update(ClientSubscription).where(ClientSubscription.id == old.id).values(status="expired"))
    await replay_session.commit()
    new = await seed_sub(replay_session, c.id, start=NOW - timedelta(days=1), limit=10)
    for i in range(8):
        await usage.track_inbound_conversation(replay_session, c.id, "whatsapp", f"b{i}", NOW)

    keys = {a.dedupe_key for a in await fresh(replay_session, BillingAlert, BillingAlert.client_id == c.id)}
    assert f"usage_80:{new.id}" in keys


async def test_raise_alert_is_idempotent_and_reports_whether_it_created(replay_session):
    c = await new_client(replay_session)

    first = await alert_service.raise_alert(replay_session, c.id, "expired", "expired:never")
    second = await alert_service.raise_alert(replay_session, c.id, "expired", "expired:never")
    await replay_session.commit()

    assert (first, second) == (True, False)
    assert len(await fresh(replay_session, BillingAlert, BillingAlert.client_id == c.id)) == 1


async def test_list_alerts_and_mark_read_are_tenant_scoped(replay_session):
    a, b = await new_client(replay_session), await new_client(replay_session)
    await alert_service.raise_alert(replay_session, a.id, "expired", "k1")
    await alert_service.raise_alert(replay_session, b.id, "expired", "k1")
    await replay_session.commit()
    (mine,) = await alert_service.list_alerts(replay_session, a.id)

    assert await alert_service.mark_read(replay_session, b.id, mine.id) is False     # someone else's alert
    assert await alert_service.mark_read(replay_session, a.id, mine.id) is True
    assert await alert_service.mark_read(replay_session, a.id, mine.id) is True      # idempotent
    assert await alert_service.mark_read(replay_session, a.id, 99999) is False
    await replay_session.commit()
    assert await alert_service.list_alerts(replay_session, a.id, unread_only=True) == []
    assert len(await alert_service.list_alerts(replay_session, b.id, unread_only=True)) == 1


# ===== expiry / renewal / reminders / grace ===================================================

async def test_lapsed_period_expires_and_the_queued_renewal_is_activated(replay_session):
    c = await new_client(replay_session)
    ended = NOW - timedelta(hours=1)
    old = await seed_sub(replay_session, c.id, "starter_1500", start=ended - timedelta(days=30))
    renewal = await seed_sub(replay_session, c.id, "growth_5000", start=ended, status="pending")

    report = await maintenance.run_maintenance(replay_session, NOW)

    assert (report.expired, report.promoted, report.errors) == (1, 1, 0) and report.clients == [c.id]
    states = {s.id: s.status for s in await fresh(replay_session, ClientSubscription, ClientSubscription.client_id == c.id)}
    assert states == {old.id: "expired", renewal.id: "active"}
    legacy = (await fresh(replay_session, Client, Client.id == c.id))[0]
    assert (legacy.plan_slug, legacy.plan_conv_limit_snapshot) == ("growth", 5000)      # legacy columns follow the promotion
    again = await maintenance.run_maintenance(replay_session, NOW)
    assert (again.expired, again.promoted) == (0, 0)                                    # idempotent


async def test_the_renewal_gets_a_fresh_usage_counter_when_it_starts(replay_session):
    c = await new_client(replay_session)
    ended = NOW - timedelta(minutes=5)
    await seed_sub(replay_session, c.id, start=ended - timedelta(days=30), used=1499)
    renewal = await seed_sub(replay_session, c.id, start=ended, status="pending")
    await maintenance.run_maintenance(replay_session, NOW)

    rec = await usage.track_inbound_conversation(replay_session, c.id, "whatsapp", "first-in-new-period", NOW)

    assert (rec.subscription_id, rec.used) == (renewal.id, 1)


async def test_lapsed_period_with_no_renewal_expires_then_alerts_grace_then_expired_once_each(replay_session):
    c = await new_client(replay_session)
    old = await seed_sub(replay_session, c.id, start=NOW - timedelta(days=30, hours=1))

    r1 = await maintenance.run_maintenance(replay_session, NOW)
    assert (r1.expired, r1.promoted, r1.grace_alerts) == (1, 0, 1)
    ent = await entitlement.get_entitlement(replay_session, SimpleNamespace(id=c.id, billing_exempt=False), NOW)
    assert ent.state is EntitlementState.GRACE and ent.grace_ends_at == old.current_period_end + timedelta(days=3)

    r2 = await maintenance.run_maintenance(replay_session, NOW + timedelta(hours=2))
    assert r2.grace_alerts == 0                                                          # one-time

    later = old.current_period_end + timedelta(days=4)
    r3 = await maintenance.run_maintenance(replay_session, later)
    r4 = await maintenance.run_maintenance(replay_session, later + timedelta(hours=1))
    assert (r3.grace_alerts, r4.grace_alerts) == (1, 0)
    kinds = sorted(a.kind for a in await fresh(replay_session, BillingAlert, BillingAlert.client_id == c.id))
    assert kinds == ["expired", "grace_started"]


async def test_expiry_reminders_3d_then_1d_fire_once_each(replay_session):
    c = await new_client(replay_session)
    sub = await seed_sub(replay_session, c.id, start=NOW - timedelta(days=30) + timedelta(days=2, hours=12))   # ends in 2.5 days

    r1 = await maintenance.run_maintenance(replay_session, NOW)
    r2 = await maintenance.run_maintenance(replay_session, NOW + timedelta(hours=1))
    assert (r1.reminders, r2.reminders) == (1, 0)

    r3 = await maintenance.run_maintenance(replay_session, sub.current_period_end - timedelta(hours=20))
    r4 = await maintenance.run_maintenance(replay_session, sub.current_period_end - timedelta(hours=19))
    assert (r3.reminders, r4.reminders) == (1, 0)
    alerts = {a.kind: a for a in await fresh(replay_session, BillingAlert, BillingAlert.client_id == c.id)}
    assert set(alerts) == {"expiring_3d", "expiring_1d"}
    assert alerts["expiring_3d"].dedupe_key == f"expiring_3d:{sub.id}" and alerts["expiring_1d"].params["days"] == 1


async def test_no_expiry_reminder_when_a_renewal_is_already_queued(replay_session):
    c = await new_client(replay_session)
    active = await seed_sub(replay_session, c.id, start=NOW - timedelta(days=29))                          # ends in 1 day
    await seed_sub(replay_session, c.id, start=active.current_period_end, status="pending")

    report = await maintenance.run_maintenance(replay_session, NOW)

    assert report.reminders == 0


async def test_no_reminder_for_a_period_that_ends_later_than_3_days(replay_session):
    c = await new_client(replay_session)
    await seed_sub(replay_session, c.id, start=NOW - timedelta(days=20))
    assert (await maintenance.run_maintenance(replay_session, NOW)).reminders == 0


async def test_exempt_and_inactive_clients_get_no_grace_alerts(replay_session):
    exempt = await new_client(replay_session, age_days=60, exempt=True)
    inactive = await new_client(replay_session, age_days=60)
    await replay_session.execute(update(Client).where(Client.id == inactive.id).values(is_active=False))
    await replay_session.commit()

    await maintenance.run_maintenance(replay_session, datetime.now(timezone.utc))

    assert await fresh(replay_session, BillingAlert, BillingAlert.client_id.in_([exempt.id, inactive.id])) == []


async def test_a_client_that_never_subscribed_gets_the_expired_alert_after_grace(replay_session):
    c = await new_client(replay_session, age_days=30)

    report = await maintenance.run_maintenance(replay_session, datetime.now(timezone.utc))

    assert report.grace_alerts >= 1
    (alert,) = await fresh(replay_session, BillingAlert, BillingAlert.client_id == c.id)
    assert (alert.kind, alert.dedupe_key) == ("expired", "expired:never")


async def test_one_failing_client_does_not_stop_the_others(replay_session, monkeypatch):
    bad, good = await new_client(replay_session), await new_client(replay_session)
    for cl in (bad, good):
        await seed_sub(replay_session, cl.id, start=NOW - timedelta(days=31))
    real = maintenance.roll_forward

    async def flaky(db, client_id, now=None):
        if client_id == bad.id:
            raise RuntimeError("boom")
        return await real(db, client_id, now)

    monkeypatch.setattr(maintenance, "roll_forward", flaky)
    report = await maintenance.run_maintenance(replay_session, NOW)

    assert report.errors == 1 and report.expired == 1 and good.id in report.clients
    status = {s.client_id: s.status for s in await fresh(replay_session, ClientSubscription)}
    assert (status[good.id], status[bad.id]) == ("expired", "active")


async def test_count_comparison_is_logged_hourly_while_the_legacy_counter_is_on(replay_session, caplog, _billing_settings, monkeypatch):
    c = await new_client(replay_session)
    await seed_sub(replay_session, c.id, start=NOW - timedelta(days=1), used=7)
    at = datetime(2026, 10, 7, 12, 5, tzinfo=timezone.utc)               # minute < 15

    with caplog.at_level(logging.INFO):
        await maintenance.run_maintenance(replay_session, at)
    assert f"BILLING_COUNT_COMPARE client={c.id}" in caplog.text and "new_billing_period=7" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO):
        await maintenance.run_maintenance(replay_session, at.replace(minute=30))
    assert "BILLING_COUNT_COMPARE" not in caplog.text

    off = _billing_settings.model_copy(update={"billing_legacy_counting": False})
    monkeypatch.setattr(maintenance, "get_settings", lambda: off)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await maintenance.run_maintenance(replay_session, at)
    assert "BILLING_COUNT_COMPARE" not in caplog.text


async def test_advisory_lock_makes_a_second_instance_skip_its_tick(replay_db_url):
    engine = create_async_engine(replay_db_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.connect() as other_instance:
        assert await other_instance.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": maintenance.ADVISORY_LOCK_KEY})
        skipped = await maintenance.run_maintenance_job(factory, NOW)
        await other_instance.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": maintenance.ADVISORY_LOCK_KEY})

    ran = await maintenance.run_maintenance_job(factory, NOW)
    async with engine.connect() as probe:                                 # the job released the lock when it finished
        assert await probe.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": maintenance.ADVISORY_LOCK_KEY})
        await probe.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": maintenance.ADVISORY_LOCK_KEY})
    await engine.dispose()

    assert skipped is None and isinstance(ran, maintenance.MaintenanceReport)


async def test_the_advisory_lock_is_released_even_when_the_run_fails(replay_db_url, monkeypatch):
    engine = create_async_engine(replay_db_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(maintenance, "run_maintenance", AsyncMock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        await maintenance.run_maintenance_job(factory, NOW)

    async with engine.connect() as probe:
        assert await probe.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": maintenance.ADVISORY_LOCK_KEY})
    await engine.dispose()


# ===== entitlement gate (real rows) ===========================================================

async def conv_for(session, client, phone="919000111222", *, stage="greeting", ai_enabled=True) -> Conversation:
    """A conversation for the client in a given stage."""
    conv = await conversation_service.get_or_create_conversation(session, phone, channel="whatsapp", client_id=client.id)
    conv.current_stage, conv.ai_enabled = stage, ai_enabled
    await session.commit()
    return conv


def as_client(c, **kw):
    """Client-shaped object for entitlement calls."""
    base = dict(id=c.id, billing_exempt=False, created_at=datetime.now(timezone.utc) - timedelta(days=30))
    base.update(kw)
    return SimpleNamespace(**base)


async def test_gate_active_and_grace_clients_are_served(replay_session):
    active, grace = await new_client(replay_session), await new_client(replay_session)
    await seed_sub(replay_session, active.id, start=datetime.now(timezone.utc) - timedelta(days=1))
    await seed_sub(replay_session, grace.id, start=datetime.now(timezone.utc) - timedelta(days=31))   # ended ~1 day ago

    d1 = await entitlement.decide_inbound(replay_session, as_client(active), await conv_for(replay_session, active))
    d2 = await entitlement.decide_inbound(replay_session, as_client(grace), await conv_for(replay_session, grace))

    assert (d1.allow, d1.state) == (True, EntitlementState.ACTIVE) and (d2.allow, d2.state) == (True, EntitlementState.GRACE)


async def test_gate_blocks_an_expired_client_new_conversation(replay_session):
    c = await new_client(replay_session)
    await seed_sub(replay_session, c.id, start=datetime.now(timezone.utc) - timedelta(days=40))        # ended 10 days ago

    d = await entitlement.decide_inbound(replay_session, as_client(c), await conv_for(replay_session, c))

    assert (d.allow, d.state, d.reason) == (False, EntitlementState.EXPIRED, "subscription_expired")


@pytest.mark.parametrize("stage", ["order_collection", "awaiting_final_confirmation", "payment", "awaiting_switch_confirm"])
async def test_gate_lets_an_in_progress_order_complete_after_expiry(replay_session, stage):
    c = await new_client(replay_session)
    d = await entitlement.decide_inbound(replay_session, as_client(c), await conv_for(replay_session, c, stage=stage))
    assert (d.allow, d.reason) == (True, "order_in_progress")


async def test_gate_lets_a_customer_with_items_in_their_cart_continue(replay_session):
    c = await new_client(replay_session)
    conv = await conv_for(replay_session, c)
    await replay_session.execute(update(Conversation).where(Conversation.id == conv.id).values(cart_items=[{"sku": "SKU001", "qty": 1}]))
    await replay_session.commit()
    conv = (await fresh(replay_session, Conversation, Conversation.id == conv.id))[0]

    assert (await entitlement.decide_inbound(replay_session, as_client(c), conv)).reason == "order_in_progress"


@pytest.mark.parametrize("status", ["pending_payment", "payment_submitted"])
async def test_gate_lets_a_customer_awaiting_payment_verification_through_even_if_the_stage_looks_idle(replay_session, status):
    c = await new_client(replay_session)
    conv = await conv_for(replay_session, c, stage="greeting")
    replay_session.add(Order(
        order_number=f"ORD-E-{next(_n)}", client_id=c.id, conversation_id=conv.id, customer_name="N",
        customer_phone="919000111222",
        delivery_address="A", product_name="P", quantity=1, unit_price=500.0, total_amount=500.0, status=status,
    ))
    await replay_session.commit()

    d = await entitlement.decide_inbound(replay_session, as_client(c), conv)

    assert (d.allow, d.reason) == (True, "order_in_progress")


async def test_gate_a_finished_order_does_not_count_as_in_progress(replay_session):
    c = await new_client(replay_session)
    conv = await conv_for(replay_session, c, stage="completed")
    replay_session.add(Order(
        order_number=f"ORD-E-{next(_n)}", client_id=c.id, conversation_id=conv.id, customer_name="N",
        customer_phone="919000111222",
        delivery_address="A", product_name="P", quantity=1, unit_price=500.0, total_amount=500.0, status="delivered",
    ))
    await replay_session.commit()

    assert (await entitlement.decide_inbound(replay_session, as_client(c), conv)).allow is False


async def test_gate_never_interferes_with_a_human_takeover_conversation(replay_session):
    c = await new_client(replay_session)
    d = await entitlement.decide_inbound(replay_session, as_client(c), await conv_for(replay_session, c, ai_enabled=False))
    assert (d.allow, d.reason) == (True, "human_takeover")


async def test_gate_exempt_client_is_never_restricted(replay_session):
    c = await new_client(replay_session, exempt=True)
    d = await entitlement.decide_inbound(replay_session, as_client(c, billing_exempt=True), await conv_for(replay_session, c))
    assert (d.allow, d.state) == (True, EntitlementState.EXEMPT)


async def test_gate_does_nothing_when_enforcement_is_off(replay_session, _billing_settings, monkeypatch):
    c = await new_client(replay_session)
    monkeypatch.setattr(entitlement, "get_settings", lambda: _billing_settings.model_copy(update={"sellertalk24_billing_enforce": False}))
    d = await entitlement.decide_inbound(replay_session, as_client(c), await conv_for(replay_session, c))
    assert (d.allow, d.reason) == (True, "enforcement_off")


async def test_a_queued_renewal_that_has_started_counts_as_running_before_the_scheduler_promotes_it(replay_session):
    c = await new_client(replay_session)
    now = datetime.now(timezone.utc)
    await seed_sub(replay_session, c.id, start=now - timedelta(days=30, minutes=2))                  # ended 2 min ago
    await seed_sub(replay_session, c.id, start=now - timedelta(minutes=2), status="pending")        # promoted by nobody yet

    ent = await entitlement.get_entitlement(replay_session, as_client(c), now)

    assert ent.state is EntitlementState.ACTIVE


# ===== send gate ======================================================================================

async def test_send_gate_stops_automation_but_not_replies_for_an_expired_client(replay_session):
    c = await new_client(replay_session, age_days=30)
    customer = SimpleNamespace(id=1, client_id=c.id, is_blocked=False, opted_out=False, last_inbound_at=datetime.now(timezone.utc))

    async def decide(kind):
        return await send_gate.check_send(replay_session, client_id=c.id, customer=customer, message_kind=kind)

    followup = await decide(send_gate.MessageKind.FOLLOWUP)
    assert followup.denied and followup.reason is send_gate.DenyReason.SUBSCRIPTION_INACTIVE
    assert (await decide(send_gate.MessageKind.BROADCAST_MARKETING)).reason is send_gate.DenyReason.SUBSCRIPTION_INACTIVE
    assert (await decide(send_gate.MessageKind.NUDGE)).reason is send_gate.DenyReason.SUBSCRIPTION_INACTIVE
    assert (await decide(send_gate.MessageKind.PIPELINE_REPLY)).allowed
    assert (await decide(send_gate.MessageKind.UTILITY_TEMPLATE)).allowed
    assert (await decide(send_gate.MessageKind.MANUAL_AGENT)).allowed


async def test_send_gate_automation_is_allowed_for_active_grace_and_exempt_clients(replay_session):
    active, exempt = await new_client(replay_session), await new_client(replay_session, age_days=60, exempt=True)
    grace = await new_client(replay_session)                                   # created just now -> in grace
    await seed_sub(replay_session, active.id, start=datetime.now(timezone.utc) - timedelta(days=1))

    for c in (active, exempt, grace):
        customer = SimpleNamespace(id=1, client_id=c.id, is_blocked=False, opted_out=False, last_inbound_at=datetime.now(timezone.utc))
        d = await send_gate.check_send(replay_session, client_id=c.id, customer=customer, message_kind=send_gate.MessageKind.FOLLOWUP)
        assert d.allowed, c


# ===== end to end through the WhatsApp webhook ================================================

def sent_texts() -> list[str]:
    """Every string handed to the (mocked) WhatsApp transport — text, button and image sends, args and kwargs."""
    out = []
    for name in ("_raw_send_text_message", "_raw_send_button_message", "_raw_send_image_message"):
        for call in getattr(whatsapp_service, name).call_args_list:
            out.extend(str(v) for v in (*call.args, *call.kwargs.values()) if isinstance(v, str))
    return out


async def messages_of(session, client_id) -> list[tuple[str, str]]:
    """(role, content) of every stored message of a client's conversations."""
    rows = (await session.execute(
        select(Message.role, Message.content).join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.client_id == client_id).order_by(Message.id)
    )).all()
    return [(r, c) for r, c in rows]


async def test_e2e_expired_client_new_customer_gets_the_fixed_fallback_and_no_llm(replay_http, replay_session):
    from app.services import gemini_service

    c = await new_client(replay_session, age_days=30)

    resp = await send_message(replay_http, "919111000001", "Hi, do you have kurtas?", phone_number_id=c.pnid)

    assert resp.status_code == 200
    assert any(FALLBACK in t for t in sent_texts())
    gemini_service.generate_reply.assert_not_called()
    msgs = await messages_of(replay_session, c.id)
    assert msgs == [("user", "Hi, do you have kurtas?"), ("assistant", FALLBACK)]
    assert await n_logs(replay_session, c.id) == 0                           # an unserved message is not a counted conversation


async def test_e2e_expired_client_second_message_still_gets_the_fallback_not_a_crash(replay_http, replay_session):
    c = await new_client(replay_session, age_days=30)
    await send_message(replay_http, "919111000002", "hello", phone_number_id=c.pnid, wamid="wamid.e2e.1")
    await send_message(replay_http, "919111000002", "anyone there?", phone_number_id=c.pnid, wamid="wamid.e2e.2")

    msgs = await messages_of(replay_session, c.id)
    assert [m for m in msgs if m[0] == "assistant"] == [("assistant", FALLBACK)] * 2


async def test_e2e_order_in_progress_completes_after_expiry(replay_http, replay_session):
    c = await new_client(replay_session, age_days=30)
    phone = "919111000003"
    await send_message(replay_http, phone, "hello", phone_number_id=c.pnid, wamid="wamid.ip.1")          # fallback (idle)
    conv = (await fresh(replay_session, Conversation, Conversation.client_id == c.id))[0]
    await replay_session.execute(update(Conversation).where(Conversation.id == conv.id).values(current_stage="order_collection"))
    await replay_session.commit()
    for name in ("_raw_send_text_message", "_raw_send_button_message", "_raw_send_image_message"):
        getattr(whatsapp_service, name).reset_mock()

    resp = await send_message(replay_http, phone, "my name is Asha", phone_number_id=c.pnid, wamid="wamid.ip.2")

    assert resp.status_code == 200
    assert not any(FALLBACK in t for t in sent_texts())                    # served by the normal pipeline
    assert sent_texts()


async def test_e2e_exempt_client_is_unaffected_even_with_no_subscription(replay_http, replay_session):
    c = await new_client(replay_session, age_days=365, exempt=True)

    resp = await send_message(replay_http, "919111000004", "hello", phone_number_id=c.pnid)

    assert resp.status_code == 200
    assert not any(FALLBACK in t for t in sent_texts()) and sent_texts()


async def test_e2e_grace_client_is_served_normally(replay_http, replay_session):
    c = await new_client(replay_session)                                     # created now -> grace
    resp = await send_message(replay_http, "919111000005", "hello", phone_number_id=c.pnid)
    assert resp.status_code == 200 and sent_texts() and not any(FALLBACK in t for t in sent_texts())


async def test_e2e_enforcement_off_means_an_expired_client_is_still_served(replay_http, replay_session, _billing_settings, monkeypatch):
    c = await new_client(replay_session, age_days=30)
    monkeypatch.setattr(entitlement, "get_settings", lambda: _billing_settings.model_copy(update={"sellertalk24_billing_enforce": False}))

    await send_message(replay_http, "919111000006", "hello", phone_number_id=c.pnid)

    assert sent_texts() and not any(FALLBACK in t for t in sent_texts())


async def test_e2e_active_client_is_served_and_the_conversation_is_counted_once_per_window(replay_http, replay_session):
    c = await new_client(replay_session)
    sub = await seed_sub(replay_session, c.id, start=datetime.now(timezone.utc) - timedelta(days=1))
    phone = "919111000007"

    await send_message(replay_http, phone, "hello", phone_number_id=c.pnid, wamid="wamid.cnt.1")
    await send_message(replay_http, phone, "show me kurtas", phone_number_id=c.pnid, wamid="wamid.cnt.2")
    await send_message(replay_http, "919111000008", "hi", phone_number_id=c.pnid, wamid="wamid.cnt.3")   # a second customer

    assert not any(FALLBACK in t for t in sent_texts())
    assert await used_of(replay_session, sub.id) == 2                         # two customers, two windows
    keys = sorted(r.customer_key for r in await fresh(replay_session, ConversationUsageLog, ConversationUsageLog.client_id == c.id))
    assert keys == ["919111000007", "919111000008"]
    assert {r.channel for r in await fresh(replay_session, ConversationUsageLog)} == {"whatsapp"}


async def test_e2e_over_the_limit_the_bot_keeps_answering_and_keeps_counting(replay_http, replay_session):
    c = await new_client(replay_session)
    sub = await seed_sub(replay_session, c.id, start=datetime.now(timezone.utc) - timedelta(days=1), limit=1, used=1)
    await replay_session.execute(update(ClientSubscription).where(ClientSubscription.id == sub.id).values(over_limit=True))
    await replay_session.commit()

    resp = await send_message(replay_http, "919111000009", "hello", phone_number_id=c.pnid)

    assert resp.status_code == 200 and sent_texts() and not any(FALLBACK in t for t in sent_texts())   # never blocked
    assert await used_of(replay_session, sub.id) == 2
    kinds = {a.kind for a in await fresh(replay_session, BillingAlert, BillingAlert.client_id == c.id)}
    assert "usage_120" in kinds or "usage_100" in kinds


async def test_e2e_a_counting_bug_cannot_break_the_webhook(replay_http, replay_session, monkeypatch):
    c = await new_client(replay_session)
    await seed_sub(replay_session, c.id, start=datetime.now(timezone.utc) - timedelta(days=1))
    monkeypatch.setattr(usage, "record_conversation", AsyncMock(side_effect=RuntimeError("counting is broken")))

    resp = await send_message(replay_http, "919111000010", "hello", phone_number_id=c.pnid)

    assert resp.status_code == 200 and sent_texts() and not any(FALLBACK in t for t in sent_texts())


async def test_e2e_an_entitlement_bug_fails_open(replay_http, replay_session, monkeypatch):
    c = await new_client(replay_session, age_days=30)                         # would be blocked...
    monkeypatch.setattr(entitlement, "get_entitlement", AsyncMock(side_effect=RuntimeError("boom")))   # ...but the check dies

    resp = await send_message(replay_http, "919111000011", "hello", phone_number_id=c.pnid)

    assert resp.status_code == 200 and not any(FALLBACK in t for t in sent_texts()) and sent_texts()


async def test_e2e_the_fallback_follows_the_customers_language(replay_http, replay_session):
    c = await new_client(replay_session, age_days=30)
    phone = "919111000012"
    await send_message(replay_http, phone, "hello", phone_number_id=c.pnid, wamid="wamid.lang.1")
    await replay_session.execute(update(Conversation).where(Conversation.client_id == c.id).values(last_customer_language="gujarati_script"))
    await replay_session.commit()
    whatsapp_service._raw_send_text_message.reset_mock()

    await send_message(replay_http, phone, "hello", phone_number_id=c.pnid, wamid="wamid.lang.2")

    assert any(get_template("gujarati_script", "billing_assistant_unavailable") in t for t in sent_texts())


# ===== API: entitlement + alerts ==============================================================

def auth(c) -> dict:
    """Bearer header for the client's owner user."""
    return {"Authorization": f"Bearer {create_access_token({'sub': c.email})}"}


@pytest_asyncio.fixture
async def api(replay_http):
    """The HTTP client with billing settings pinned and the mock gateway (no Razorpay)."""
    from app.config import get_settings
    from app.main import app
    from app.routers.billing import get_gateway
    from app.services.billing.razorpay_client import MockRazorpayClient

    pinned = get_settings().model_copy(update=dict(
        prices_include_gst=False, gst_rate_bps=1800, seller_state_code="24",
        sellertalk24_billing_enforce=True, grace_days=3,
    ))
    gw = MockRazorpayClient()
    app.dependency_overrides[get_settings] = lambda: pinned
    app.dependency_overrides[get_gateway] = lambda: gw
    yield replay_http
    app.dependency_overrides.pop(get_settings, None)
    app.dependency_overrides.pop(get_gateway, None)


async def test_api_subscription_reports_entitlement_states(api, replay_session, monkeypatch, _billing_settings):
    monkeypatch.setattr("app.services.billing.views.enforcement_enabled", lambda: True)
    grace, expired, exempt = await new_client(replay_session), await new_client(replay_session, age_days=30), await new_client(replay_session, age_days=30, exempt=True)

    g = (await api.get("/billing/subscription", headers=auth(grace))).json()
    e = (await api.get("/billing/subscription", headers=auth(expired))).json()
    x = (await api.get("/billing/subscription", headers=auth(exempt))).json()

    assert (g["status"], g["entitlement"]["state"], g["entitlement"]["restricted"]) == ("none", "grace", False)
    assert g["entitlement"]["grace_ends_at"] is not None and g["entitlement"]["enforced"] is True
    assert (e["entitlement"]["state"], e["entitlement"]["restricted"]) == ("expired", True)
    assert (x["entitlement"]["state"], x["entitlement"]["restricted"]) == ("exempt", False)


async def test_api_subscription_reports_over_limit_for_the_upgrade_banner(api, replay_session):
    c = await new_client(replay_session)
    sub = await seed_sub(replay_session, c.id, start=datetime.now(timezone.utc) - timedelta(days=1), limit=5, used=4)
    ok = (await api.get("/billing/subscription", headers=auth(c))).json()
    await usage.track_inbound_conversation(replay_session, c.id, "whatsapp", "the-fifth")
    at_limit = (await api.get("/billing/subscription", headers=auth(c))).json()
    await usage.track_inbound_conversation(replay_session, c.id, "whatsapp", "the-sixth")

    over = (await api.get("/billing/subscription", headers=auth(c))).json()

    assert (ok["over_limit"], ok["entitlement"]["over_limit"]) == (False, False)
    assert (at_limit["over_limit"], at_limit["subscription"]["percent_used"]) == (False, 100.0)   # AT the limit is not over it
    assert (over["over_limit"], over["entitlement"]["over_limit"]) == (True, True)
    assert over["subscription"]["percent_used"] == 120.0 and over["subscription"]["id"] == sub.id


async def test_api_alerts_list_unread_filter_and_mark_read(api, replay_session):
    c, other = await new_client(replay_session), await new_client(replay_session)
    await alert_service.raise_alert(replay_session, c.id, "expiring_3d", "k1", days=3, plan="Starter", end="10 Oct 2026")
    await alert_service.raise_alert(replay_session, c.id, "expired", "k2")
    await alert_service.raise_alert(replay_session, other.id, "expired", "k3")
    await replay_session.commit()

    listing = (await api.get("/billing/alerts", headers=auth(c))).json()
    assert listing["unread"] == 2 and {a["kind"] for a in listing["alerts"]} == {"expiring_3d", "expired"}
    first = listing["alerts"][0]
    assert first["params"] is not None and first["read_at"] is None and first["title"]

    assert (await api.post(f"/billing/alerts/{first['id']}/read", headers=auth(c))).status_code == 204
    again = (await api.get("/billing/alerts?unread_only=true", headers=auth(c))).json()
    assert again["unread"] == 1 and first["id"] not in {a["id"] for a in again["alerts"]}


async def test_api_cannot_read_or_mark_another_tenants_alert(api, replay_session):
    a, b = await new_client(replay_session), await new_client(replay_session)
    await alert_service.raise_alert(replay_session, a.id, "expired", "k")
    await replay_session.commit()
    (alert,) = await alert_service.list_alerts(replay_session, a.id)

    assert (await api.post(f"/billing/alerts/{alert.id}/read", headers=auth(b))).status_code == 404
    assert (await api.get("/billing/alerts", headers=auth(b))).json() == {"alerts": [], "unread": 0}


async def test_api_alerts_require_authentication(api):
    assert (await api.get("/billing/alerts")).status_code == 401
    assert (await api.post("/billing/alerts/1/read")).status_code == 401
