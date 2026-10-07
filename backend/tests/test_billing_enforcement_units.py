"""Unit tests (no DB) for step 4: counting thresholds, alerts, entitlement decisions, gate wiring."""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import order_pipeline, send_gate
from app.services.billing import alerts, entitlement, maintenance, usage
from app.services.billing.entitlement import EntitlementState
from app.services.language_templates import TEMPLATES, get_template

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def _patch_settings(monkeypatch, **kw):
    """Make entitlement read these settings instead of the real environment."""
    values = dict(sellertalk24_billing_enforce=True, grace_days=3, billing_legacy_counting=True)
    values.update(kw)
    monkeypatch.setattr(entitlement, "get_settings", lambda: SimpleNamespace(**values))
    return values


# --- usage.crossed_thresholds ----------------------------------------------------------------

@pytest.mark.parametrize(
    "used,limit,expected",
    [
        (1, 10, []), (7, 10, []), (8, 10, [80]), (9, 10, []), (10, 10, [100]), (11, 10, []), (12, 10, [120]), (13, 10, []),
        (1200, 1500, [80]), (1500, 1500, [100]), (1800, 1500, [120]),
        (1, 1, [80, 100]), (2, 1, [120]), (0, 10, []), (5, 0, []),
    ],
)
def test_crossed_thresholds_fires_only_on_the_increment_that_crosses(used, limit, expected):
    assert usage.crossed_thresholds(used, limit) == expected


def test_crossed_thresholds_fire_exactly_once_over_a_full_ramp():
    fired = [p for used in range(1, 40) for p in usage.crossed_thresholds(used, 25)]
    assert fired == [80, 100, 120]


def test_window_is_meta_24_hours():
    assert usage.WINDOW == timedelta(hours=24) and usage.THRESHOLD_PCTS == (80, 100, 120)


# --- alerts.build_alert ---------------------------------------------------------------------

@pytest.mark.parametrize("pct,severity", [(80, "warning"), (100, "warning"), (120, "critical")])
def test_build_alert_usage_levels(pct, severity):
    sev, title, message = alerts.build_alert("usage_%d" % pct, pct=pct, used=12, limit=10, plan="Starter")
    assert sev == severity and title and "Starter" in message


def test_build_alert_100_says_the_assistant_keeps_working():
    assert "keeps working" in alerts.build_alert("usage_100", pct=100, used=10, limit=10, plan="Starter")[2]


def test_build_alert_expiry_reminders_and_pluralisation():
    sev3, title3, msg3 = alerts.build_alert("expiring_3d", days=3, plan="Growth", end="10 Oct 2026")
    sev1, title1, _ = alerts.build_alert("expiring_1d", days=1, plan="Growth", end="08 Oct 2026")
    assert (sev3, title3) == ("warning", "Plan expires in 3 days") and "10 Oct 2026" in msg3
    assert (sev1, title1) == ("critical", "Plan expires in 1 day")


def test_build_alert_grace_and_expired():
    sev, _, msg = alerts.build_alert("grace_started", grace_ends="10 Oct 2026", grace_days=3)
    assert sev == "critical" and "10 Oct 2026" in msg and "keeps working" in msg
    assert "orders already in progress still complete" in alerts.build_alert("expired")[2]


def test_build_alert_rejects_unknown_kinds():
    with pytest.raises(ValueError):
        alerts.build_alert("nope")


# --- entitlement: pure helpers --------------------------------------------------------------

@pytest.mark.parametrize(
    "stage,cart,expected",
    [
        ("greeting", None, False), ("product_inquiry", [], False), ("completed", None, False), (None, None, False),
        ("order_collection", None, True), ("awaiting_final_confirmation", None, True), ("payment", None, True),
        ("awaiting_switch_confirm", None, True), ("awaiting_afc_switch_confirm", None, True),
        ("greeting", [{"sku": "A"}], True), ("some_new_stage", None, True),
    ],
)
def test_stage_in_progress(stage, cart, expected):
    assert entitlement.stage_in_progress(stage, cart) is expected


def test_entitlement_restricted_only_when_expired():
    for state in EntitlementState:
        assert entitlement.Entitlement(state).restricted is (state is EntitlementState.EXPIRED)


def test_enforcement_and_grace_settings_are_read_from_settings(monkeypatch):
    _patch_settings(monkeypatch, sellertalk24_billing_enforce=False, grace_days=5)
    assert entitlement.enforcement_enabled() is False and entitlement.grace_days() == 5
    _patch_settings(monkeypatch)
    assert entitlement.enforcement_enabled() is True and entitlement.grace_days() == 3


# --- entitlement.get_entitlement with a stub DB -----------------------------------------------

def _db(*results):
    """AsyncSession stub whose execute() returns each given `.first()` row in turn."""
    seq = [MagicMock(first=MagicMock(return_value=r)) for r in results]
    db = MagicMock()
    db.execute = AsyncMock(side_effect=seq)
    return db


@pytest.mark.asyncio
async def test_get_entitlement_exempt_does_no_queries():
    db = _db()
    ent = await entitlement.get_entitlement(db, SimpleNamespace(id=1, billing_exempt=True), NOW)
    assert ent.state is EntitlementState.EXEMPT and db.execute.await_count == 0


@pytest.mark.asyncio
async def test_get_entitlement_active_uses_one_query_and_reports_over_limit():
    ent = await entitlement.get_entitlement(
        _db(SimpleNamespace(id=7, over_limit=True)), SimpleNamespace(id=2, billing_exempt=False), NOW
    )
    assert (ent.state, ent.subscription_id, ent.over_limit) == (EntitlementState.ACTIVE, 7, True)


@pytest.mark.asyncio
async def test_get_entitlement_grace_then_expired_around_the_boundary(monkeypatch):
    _patch_settings(monkeypatch, grace_days=3)
    ended = NOW - timedelta(days=3) + timedelta(seconds=1)
    last = SimpleNamespace(id=9, current_period_end=ended)
    client = SimpleNamespace(id=2, billing_exempt=False)

    in_grace = await entitlement.get_entitlement(_db(None, last), client, NOW)
    assert in_grace.state is EntitlementState.GRACE and in_grace.anchor_key == "9"
    assert in_grace.grace_ends_at == ended + timedelta(days=3)

    expired = await entitlement.get_entitlement(_db(None, last), client, NOW + timedelta(seconds=2))
    assert expired.state is EntitlementState.EXPIRED


@pytest.mark.asyncio
async def test_get_entitlement_never_subscribed_graces_from_signup(monkeypatch):
    _patch_settings(monkeypatch, grace_days=3)
    fresh = SimpleNamespace(id=3, billing_exempt=False, created_at=NOW - timedelta(days=1))
    old = SimpleNamespace(id=4, billing_exempt=False, created_at=NOW - timedelta(days=30))

    assert (await entitlement.get_entitlement(_db(None, None), fresh, NOW)).state is EntitlementState.GRACE
    ent = await entitlement.get_entitlement(_db(None, None), old, NOW)
    assert ent.state is EntitlementState.EXPIRED and ent.anchor_key == "never"


# --- decide_inbound ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_decide_inbound_with_enforcement_off_serves_without_touching_the_db(monkeypatch):
    _patch_settings(monkeypatch, sellertalk24_billing_enforce=False)
    db = MagicMock(execute=AsyncMock())
    decision = await entitlement.decide_inbound(db, SimpleNamespace(id=1), SimpleNamespace(), NOW)
    assert decision.allow is True and decision.reason == "enforcement_off" and db.execute.await_count == 0


@pytest.mark.asyncio
async def test_decide_inbound_blocks_only_expired_idle_bot_driven_conversations(monkeypatch):
    _patch_settings(monkeypatch)
    expired = entitlement.Entitlement(EntitlementState.EXPIRED)

    async def fake_ent(db, client, now=None):
        return fake_ent.value

    monkeypatch.setattr(entitlement, "get_entitlement", fake_ent)
    monkeypatch.setattr(entitlement, "order_in_progress", AsyncMock(return_value=False))
    client, conv = SimpleNamespace(id=1), SimpleNamespace(ai_enabled=True, current_stage="greeting")

    fake_ent.value = expired
    blocked = await entitlement.decide_inbound(None, client, conv, NOW)
    assert (blocked.allow, blocked.reason) == (False, "subscription_expired")

    for state in (EntitlementState.ACTIVE, EntitlementState.GRACE, EntitlementState.EXEMPT):
        fake_ent.value = entitlement.Entitlement(state)
        assert (await entitlement.decide_inbound(None, client, conv, NOW)).allow is True

    fake_ent.value = expired
    paused = await entitlement.decide_inbound(None, client, SimpleNamespace(ai_enabled=False), NOW)
    assert (paused.allow, paused.reason) == (True, "human_takeover")

    monkeypatch.setattr(entitlement, "order_in_progress", AsyncMock(return_value=True))
    mid_order = await entitlement.decide_inbound(None, client, conv, NOW)
    assert (mid_order.allow, mid_order.reason) == (True, "order_in_progress")


@pytest.mark.asyncio
async def test_decide_inbound_fails_open_on_any_error(monkeypatch, caplog):
    _patch_settings(monkeypatch)
    monkeypatch.setattr(entitlement, "get_entitlement", AsyncMock(side_effect=RuntimeError("db down")))
    with caplog.at_level(logging.ERROR):
        decision = await entitlement.decide_inbound(None, SimpleNamespace(id=1), SimpleNamespace(), NOW)
    assert decision.allow is True and decision.reason == "error_fail_open" and "failing OPEN" in caplog.text


@pytest.mark.asyncio
async def test_automation_allowed_matrix(monkeypatch):
    _patch_settings(monkeypatch, sellertalk24_billing_enforce=False)
    assert await entitlement.automation_allowed(MagicMock(), 1) is True          # enforcement off: no DB use

    _patch_settings(monkeypatch)
    db = MagicMock(get=AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr(entitlement, "get_entitlement", AsyncMock(return_value=entitlement.Entitlement(EntitlementState.EXPIRED)))
    assert await entitlement.automation_allowed(db, 1) is False
    monkeypatch.setattr(entitlement, "get_entitlement", AsyncMock(return_value=entitlement.Entitlement(EntitlementState.GRACE)))
    assert await entitlement.automation_allowed(db, 1) is True
    assert await entitlement.automation_allowed(MagicMock(get=AsyncMock(return_value=None)), 99) is True   # unknown client
    monkeypatch.setattr(entitlement, "get_entitlement", AsyncMock(side_effect=RuntimeError("x")))
    assert await entitlement.automation_allowed(db, 1) is True                    # fails open


# --- send gate ----------------------------------------------------------------------------------

def test_send_gate_has_the_subscription_deny_reason_and_automated_kinds():
    assert send_gate.DenyReason.SUBSCRIPTION_INACTIVE.value == "subscription_inactive"
    assert send_gate.AUTOMATED_KINDS == {
        send_gate.MessageKind.NUDGE, send_gate.MessageKind.FOLLOWUP, send_gate.MessageKind.BROADCAST_MARKETING,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,denied",
    [
        (send_gate.MessageKind.NUDGE, True), (send_gate.MessageKind.FOLLOWUP, True),
        (send_gate.MessageKind.BROADCAST_MARKETING, True),
        (send_gate.MessageKind.PIPELINE_REPLY, False), (send_gate.MessageKind.UTILITY_TEMPLATE, False),
        (send_gate.MessageKind.MANUAL_AGENT, False), (send_gate.MessageKind.OWNER_ALERT, False),
    ],
)
async def test_check_send_blocks_only_automation_for_an_expired_client(monkeypatch, kind, denied):
    monkeypatch.setattr(entitlement, "automation_allowed", AsyncMock(return_value=False))
    decision = await send_gate.check_send(
        MagicMock(), client_id=5, customer=SimpleNamespace(id=1, client_id=5, is_blocked=False, opted_out=False,
                                                           last_inbound_at=datetime.now(timezone.utc)),
        message_kind=kind,
    )
    assert (decision.denied and decision.reason is send_gate.DenyReason.SUBSCRIPTION_INACTIVE) is denied


@pytest.mark.asyncio
async def test_check_send_unaffected_when_the_client_is_entitled(monkeypatch):
    monkeypatch.setattr(entitlement, "automation_allowed", AsyncMock(return_value=True))
    decision = await send_gate.check_send(
        MagicMock(), client_id=5,
        customer=SimpleNamespace(id=1, client_id=5, is_blocked=False, opted_out=False, last_inbound_at=datetime.now(timezone.utc)),
        message_kind=send_gate.MessageKind.FOLLOWUP, conversation_id=None,
    )
    assert decision.allowed


# --- templates ----------------------------------------------------------------------------------

def test_fallback_template_exists_in_every_language_and_is_distinct():
    texts = {lang: get_template(lang, "billing_assistant_unavailable") for lang in TEMPLATES}
    assert all(texts.values())
    assert len(set(texts.values())) == 5     # hinglish and hindi_roman intentionally share one dict
    assert "seller will contact you" in texts["english"]


# --- pipeline preflight ---------------------------------------------------------------------------

def _ctx(**kw):
    """InboundContext stand-in for the preflight."""
    base = dict(
        db=MagicMock(), client=SimpleNamespace(id=4), conv=SimpleNamespace(id=9, last_customer_language="english"),
        sender_phone="919999", user_text="hi", wamid="w1", is_whatsapp=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_preflight_counts_and_returns_none_when_allowed(monkeypatch):
    monkeypatch.setattr(entitlement, "decide_inbound", AsyncMock(return_value=entitlement.InboundDecision(True, EntitlementState.ACTIVE)))
    track = AsyncMock()
    monkeypatch.setattr(usage, "track_inbound_conversation", track)

    assert await order_pipeline._billing_preflight(_ctx()) is None
    track.assert_awaited_once()
    assert track.await_args.args[1:] == (4, "whatsapp", "919999")


@pytest.mark.asyncio
async def test_preflight_uses_the_instagram_channel_for_non_whatsapp(monkeypatch):
    monkeypatch.setattr(entitlement, "decide_inbound", AsyncMock(return_value=entitlement.InboundDecision(True, EntitlementState.ACTIVE)))
    track = AsyncMock()
    monkeypatch.setattr(usage, "track_inbound_conversation", track)

    await order_pipeline._billing_preflight(_ctx(is_whatsapp=False, sender_phone="ig_user"))

    assert track.await_args.args[2:] == ("instagram", "ig_user")


@pytest.mark.asyncio
async def test_preflight_blocked_returns_the_template_saves_both_messages_and_does_not_count(monkeypatch):
    monkeypatch.setattr(entitlement, "decide_inbound", AsyncMock(return_value=entitlement.InboundDecision(False, EntitlementState.EXPIRED, "subscription_expired")))
    track, save = AsyncMock(), AsyncMock()
    monkeypatch.setattr(usage, "track_inbound_conversation", track)
    monkeypatch.setattr(order_pipeline.conversation_service, "save_message", save)

    result = await order_pipeline._billing_preflight(_ctx(conv=SimpleNamespace(id=9, last_customer_language="hinglish")))

    assert result.text == get_template("hinglish", "billing_assistant_unavailable")
    assert [c.args[2] for c in save.await_args_list] == ["user", "assistant"]
    track.assert_not_awaited()


@pytest.mark.asyncio
async def test_preflight_defaults_to_english_when_language_unknown(monkeypatch):
    monkeypatch.setattr(entitlement, "decide_inbound", AsyncMock(return_value=entitlement.InboundDecision(False, EntitlementState.EXPIRED)))
    monkeypatch.setattr(order_pipeline.conversation_service, "save_message", AsyncMock())
    result = await order_pipeline._billing_preflight(_ctx(conv=SimpleNamespace(id=9, last_customer_language=None)))
    assert result.text == get_template("english", "billing_assistant_unavailable")


@pytest.mark.asyncio
async def test_preflight_never_raises_into_the_webhook_path(monkeypatch, caplog):
    monkeypatch.setattr(entitlement, "decide_inbound", AsyncMock(side_effect=RuntimeError("boom")))
    with caplog.at_level(logging.ERROR):
        assert await order_pipeline._billing_preflight(_ctx()) is None
    assert "preflight failed" in caplog.text

    monkeypatch.setattr(entitlement, "decide_inbound", AsyncMock(return_value=entitlement.InboundDecision(True, EntitlementState.ACTIVE)))
    monkeypatch.setattr(usage, "track_inbound_conversation", AsyncMock(side_effect=RuntimeError("count boom")))
    assert await order_pipeline._billing_preflight(_ctx()) is None


@pytest.mark.asyncio
async def test_preflight_is_a_no_op_without_a_client_or_conversation():
    assert await order_pipeline._billing_preflight(_ctx(client=None)) is None
    assert await order_pipeline._billing_preflight(_ctx(conv=None)) is None


@pytest.mark.asyncio
async def test_legacy_counter_runs_only_while_its_flag_is_on(monkeypatch):
    legacy, rec_msg = AsyncMock(), AsyncMock()
    monkeypatch.setattr(order_pipeline.billing_service, "record_conversation_activity", legacy)
    monkeypatch.setattr(order_pipeline.usage_service, "record_message", rec_msg)
    client, conv = SimpleNamespace(id=1), SimpleNamespace(id=2)

    monkeypatch.setattr(order_pipeline, "get_settings", lambda: SimpleNamespace(billing_legacy_counting=True))
    await order_pipeline._record_usage(MagicMock(), client, conv)
    assert legacy.await_count == 1

    monkeypatch.setattr(order_pipeline, "get_settings", lambda: SimpleNamespace(billing_legacy_counting=False))
    await order_pipeline._record_usage(MagicMock(), client, conv)
    assert legacy.await_count == 1 and rec_msg.await_count == 2     # message quota still recorded


# --- maintenance + scheduler -------------------------------------------------------------------

def test_maintenance_report_defaults_and_lock_key():
    r = maintenance.MaintenanceReport()
    assert (r.expired, r.promoted, r.reminders, r.grace_alerts, r.errors, r.clients) == (0, 0, 0, 0, 0, [])
    assert isinstance(maintenance.ADVISORY_LOCK_KEY, int) and 0 < maintenance.ADVISORY_LOCK_KEY < 2**63


def test_scheduler_registers_the_billing_job_every_15_minutes_single_instance():
    from app import scheduler as sched

    src = open(sched.__file__).read()
    block = src[src.index("_billing_maintenance_job,\n        IntervalTrigger"):]
    block = block[: block.index("scheduler.start()")]
    assert "IntervalTrigger(minutes=15)" in block and 'id="billing_maintenance"' in block
    assert "max_instances=1" in block and "coalesce=True" in block


@pytest.mark.asyncio
async def test_scheduler_job_swallows_failures(monkeypatch, caplog):
    from app import scheduler as sched

    monkeypatch.setattr(maintenance, "run_maintenance_job", AsyncMock(side_effect=RuntimeError("x")))
    with caplog.at_level(logging.ERROR):
        await sched._billing_maintenance_job()
    assert "billing_maintenance job failed" in caplog.text
