"""Tests for LLM usage tracking: pricing, llm_call recording, persistence, order report, API, CI guard."""

from __future__ import annotations

import re
from datetime import date, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.models.llm_usage import LLMUsage
from app.services import cost_log, llm_client, llm_health, llm_usage_service

# Captured at import time, before conftest's autouse fixture replaces it with a no-op.
REAL_PERSIST_ROW = llm_usage_service._persist_row

PRICE_SETTINGS = dict(
    database_url="postgresql://u:p@h/d", meta_app_secret="x", meta_verify_token="x",
    whatsapp_access_token="x", whatsapp_phone_number_id="1", _env_file=None,
)


@pytest.fixture
def prices(monkeypatch):
    """Deterministic pricing: model 'm' = $1 in / $2 out per 1M, USD_INR=80, STT $0.36/hour."""
    s = Settings(
        usd_inr=80.0, llm_price_usd_per_1m={"m": (1.0, 2.0)}, llm_price_default_usd_per_1m=(10.0, 10.0),
        llm_stt_usd_per_hour=0.36, **PRICE_SETTINGS,
    )
    monkeypatch.setattr(llm_usage_service, "get_settings", lambda: s)
    monkeypatch.setattr(llm_client, "get_settings", lambda: s)
    llm_usage_service._warned_models.clear()
    return s


@pytest.fixture
def rows(monkeypatch):
    """Capture rows that would be persisted and reset the ambient context."""
    captured: list[dict] = []

    async def _capture(row):
        """Record the row instead of touching the DB."""
        captured.append(dict(row))

    monkeypatch.setattr(llm_usage_service, "_persist_row", _capture)
    llm_usage_service.set_context(None, None)
    yield captured
    llm_usage_service.set_context(None, None)


def _resp(prompt=100, completion=20, content="ok"):
    """Fake chat-completion response carrying real-looking usage."""
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
    )


def _client_returning(resp=None, exc=None):
    """Fake AsyncOpenAI whose chat.completions.create returns resp or raises exc."""
    c = MagicMock()
    c.chat.completions.create = AsyncMock(return_value=resp, side_effect=exc)
    return c


# ── cost calculation ─────────────────────────────────────────────────────────

def test_compute_cost_inr_uses_per_1m_prices_and_usd_inr(prices):
    """cost = (in×in_price + out×out_price)/1M × USD_INR."""
    assert llm_usage_service.compute_cost_inr("m", 1_000_000, 1_000_000) == pytest.approx(240.0)
    assert llm_usage_service.compute_cost_inr("m", 500, 0) == pytest.approx(500 * 1.0 / 1e6 * 80)
    assert llm_usage_service.compute_cost_inr("m", 0, 0) == 0.0


def test_compute_cost_inr_unknown_model_uses_default_and_warns_once(prices, caplog):
    """An unpriced model is billed at the default rate (never ₹0) with one warning."""
    with caplog.at_level("WARNING"):
        a = llm_usage_service.compute_cost_inr("new-model", 1_000_000, 0)
        llm_usage_service.compute_cost_inr("new-model", 1_000_000, 0)
    assert a == pytest.approx(10.0 * 80)
    assert caplog.text.count("no price configured") == 1


def test_default_price_table_covers_configured_models():
    """Every default model id (reply/classifier/vision) has a price entry."""
    s = Settings(**PRICE_SETTINGS)
    for model in (s.llm_model_reply, s.llm_model_classifier, s.llm_model_vision):
        assert model in s.llm_price_usd_per_1m, f"no default price for {model}"


def test_stt_cost_inr_applies_ten_second_minimum(prices):
    """Tiny audio bills the 10 s minimum; longer audio bills by estimated seconds."""
    per_second = 0.36 / 3600 * 80
    assert llm_usage_service.stt_cost_inr(100) == pytest.approx(10 * per_second)
    assert llm_usage_service.stt_cost_inr(3000 * 60) == pytest.approx(60 * per_second)


def test_error_code_for_prefers_http_status():
    """HTTP status wins; otherwise the exception class name."""
    assert llm_usage_service.error_code_for(SimpleNamespace(status_code=429)) == "429"
    assert llm_usage_service.error_code_for(TimeoutError()) == "TimeoutError"


# ── llm_call recording ───────────────────────────────────────────────────────

async def test_llm_call_records_real_usage_cost_and_latency(prices, rows):
    """A successful call persists purpose, model, response.usage tokens, ₹ cost, latency, ids."""
    resp = await llm_client.llm_call(
        "classify_intent", "m", [{"role": "user", "content": "hi"}], 7, 55, 9,
        max_tokens=10, client=_client_returning(_resp(1_000, 200)),
    )
    assert resp.usage.prompt_tokens == 1_000
    [row] = rows
    assert row["purpose"] == "classify_intent" and row["model"] == "m"
    assert (row["prompt_tokens"], row["completion_tokens"]) == (1_000, 200)
    assert row["cost_inr"] == pytest.approx((1_000 * 1 + 200 * 2) / 1e6 * 80)
    assert row["success"] is True and row["error_code"] is None and row["latency_ms"] >= 0
    assert (row["client_id"], row["conversation_id"], row["order_id"]) == (7, 55, 9)


async def test_llm_call_falls_back_to_ambient_context_and_explicit_ids_win(prices, rows):
    """Ids default to the pipeline's ambient context; explicit args override it."""
    llm_usage_service.set_context(3, 30)
    c = _client_returning(_resp())
    await llm_client.llm_call("reply", "m", [], max_tokens=5, client=c)
    await llm_client.llm_call("reply", "m", [], 4, 40, max_tokens=5, client=c)
    assert [(r["client_id"], r["conversation_id"]) for r in rows] == [(3, 30), (4, 40)]


async def test_llm_call_records_failures_then_reraises(prices, rows):
    """A provider error is stored (success=False, error_code=status) and re-raised."""
    boom = type("RateLimit", (Exception,), {"status_code": 429})("slow down")
    with pytest.raises(Exception, match="slow down"):
        await llm_client.llm_call("reply", "m", [], max_tokens=5, client=_client_returning(exc=boom))
    [row] = rows
    assert row["success"] is False and row["error_code"] == "429" and row["prompt_tokens"] == 0


async def test_llm_call_does_not_record_breaker_short_circuit(prices, rows, monkeypatch):
    """An open circuit breaker makes no provider call, so nothing is recorded."""
    monkeypatch.setattr(llm_client, "chat", AsyncMock(side_effect=llm_health.LLMUnavailableError("down")))
    with pytest.raises(llm_health.LLMUnavailableError):
        await llm_client.llm_call("reply", "m", [], max_tokens=5)
    assert rows == []


async def test_llm_call_without_breaker_skips_chat(prices, rows, monkeypatch):
    """use_breaker=False (vision) bypasses chat()/the breaker but is still recorded."""
    monkeypatch.setattr(llm_client, "chat", AsyncMock(side_effect=AssertionError("breaker path used")))
    await llm_client.llm_call("vision", "m", [], max_tokens=5, client=_client_returning(_resp()), use_breaker=False)
    assert rows[0]["purpose"] == "vision"


async def test_llm_call_tolerates_missing_usage(prices, rows):
    """A response without usable usage records 0 tokens rather than failing."""
    await llm_client.llm_call("reply", "m", [], max_tokens=5, client=_client_returning(SimpleNamespace(usage=None, choices=[])))
    assert rows[0]["prompt_tokens"] == 0 and rows[0]["success"] is True


async def test_chat_json_records_one_row_per_attempt(prices, rows, monkeypatch):
    """A repair retry is a second llm_usage row (both cost money)."""
    seq = iter([_resp(content="junk"), _resp(content='{"a": 1}')])
    c = MagicMock()
    c.chat.completions.create = AsyncMock(side_effect=lambda **k: next(seq))
    monkeypatch.setattr(llm_client, "get_client", lambda: c)
    out = await llm_client.chat_json("m", [{"role": "user", "content": "x"}], max_tokens=10, purpose="tool_router", conversation_id=5)
    assert out == {"a": 1}
    assert [(r["purpose"], r["conversation_id"]) for r in rows] == [("tool_router", 5)] * 2


async def test_llm_transcribe_records_estimated_stt_cost(prices, rows):
    """Whisper has no tokens: row has 0 tokens and a size-based ₹ estimate."""
    stt = MagicMock()
    stt.audio.transcriptions.create.return_value = "namaste"
    text = await llm_client.llm_transcribe("stt", "whisper-x", b"x" * 3000 * 30, "a.ogg", "audio/ogg", 1, 2, client=stt, language="hi")
    assert text == "namaste"
    assert rows[0]["prompt_tokens"] == 0 and rows[0]["cost_inr"] == pytest.approx(30 * 0.36 / 3600 * 80)
    assert stt.audio.transcriptions.create.call_args.kwargs["language"] == "hi"


async def test_llm_transcribe_records_failure(prices, rows):
    """A failed transcription is recorded (₹0) and re-raised."""
    stt = MagicMock()
    stt.audio.transcriptions.create.side_effect = RuntimeError("bad audio")
    with pytest.raises(RuntimeError):
        await llm_client.llm_transcribe("stt", "whisper-x", b"x", "a.ogg", "audio/ogg", client=stt)
    assert rows[0]["success"] is False and rows[0]["error_code"] == "RuntimeError" and rows[0]["cost_inr"] == 0.0


async def test_record_usage_never_raises_when_persist_fails(prices, monkeypatch):
    """A DB failure is swallowed and counted so it cannot break a customer reply."""
    monkeypatch.setattr(llm_usage_service, "_persist_row", AsyncMock(side_effect=RuntimeError("db down")))
    before = llm_usage_service.persist_failures
    cost = await llm_usage_service.record_usage(purpose="reply", model="m", prompt_tokens=1_000_000)
    assert cost == pytest.approx(80.0) and llm_usage_service.persist_failures == before + 1


async def test_record_usage_logs_actual_exception_message(prices, monkeypatch, caplog):
    """A persist failure logs the exception message (e.g. 'relation does not exist'), not just its class."""
    monkeypatch.setattr(
        llm_usage_service, "_persist_row", AsyncMock(side_effect=RuntimeError('relation "llm_usage" does not exist'))
    )
    with caplog.at_level("WARNING"):
        await llm_usage_service.record_usage(purpose="reply", model="m")
    assert 'relation "llm_usage" does not exist' in caplog.text


# ── order cost report ────────────────────────────────────────────────────────

def _row(purpose="reply", cost=0.5, ok=True, p=100, c=10):
    """Lightweight stand-in for an LLMUsage row."""
    return SimpleNamespace(purpose=purpose, model="m", prompt_tokens=p, completion_tokens=c,
                           cost_inr=cost, latency_ms=120, success=ok, error_code=None if ok else "429")


def test_format_cost_report_lists_calls_and_totals():
    """The report shows each call, per-purpose subtotals, failures, total and per-message ₹."""
    text = llm_usage_service.format_cost_report(
        "ORD-1", [_row("reply", 0.5), _row("classify_intent", 0.1), _row("reply", 0.4, ok=False)],
        {"messages": 4, "template": 2, "llm": 2},
    )
    assert "COST REPORT  ORD-1" in text and "ERR 429" in text
    assert "LLM calls: 3 (1 failed)" in text and "reply: 2 (₹0.9000)" in text
    assert "total: ₹1.00" in text and "per-msg: ₹0.2500" in text and "messages: 4" in text


async def test_cost_report_is_sourced_from_llm_usage(monkeypatch):
    """print_report attributes the conversation's rows to the order, then reports llm_usage totals."""
    attribute = AsyncMock(return_value=2)
    monkeypatch.setattr(llm_usage_service, "attribute_to_order", attribute)
    monkeypatch.setattr(llm_usage_service, "fetch_order_rows", AsyncMock(return_value=[_row(cost=1.25)]))
    cost_log._logs[77] = [{"path": "LLM"}, {"path": "TEMPLATE"}]
    report = await cost_log.print_report(MagicMock(), 77, "ORD-9", 9)
    attribute.assert_awaited_once()
    assert attribute.await_args.args[1:] == (77, 9)
    assert "total: ₹1.25" in report and "messages: 2" in report and 77 not in cost_log._logs


async def test_cost_report_never_raises(monkeypatch):
    """A DB failure while reporting returns None instead of breaking order creation."""
    monkeypatch.setattr(llm_usage_service, "attribute_to_order", AsyncMock(side_effect=RuntimeError("db")))
    assert await cost_log.print_report(MagicMock(), 78, "ORD-10", 10) is None


# ── persistence + aggregation (real SQL, rolled back) ────────────────────────

@pytest.fixture
async def pg(monkeypatch):
    """
    A session on the configured Postgres inside a transaction that is always rolled back.

    The llm_usage table is created inside that transaction if it doesn't exist yet. Skips when
    Postgres isn't reachable. Service commits only release a SAVEPOINT, so nothing persists.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.config import get_settings

    engine = create_async_engine(get_settings().database_url)
    try:
        conn = await engine.connect()
    except Exception as exc:  # pragma: no cover - environment dependent
        await engine.dispose()
        pytest.skip(f"Postgres not reachable: {type(exc).__name__}")
    trans = await conn.begin()
    try:
        await conn.run_sync(lambda c: LLMUsage.__table__.create(c, checkfirst=True))
        factory = async_sessionmaker(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False, class_=AsyncSession)
        monkeypatch.setattr("app.db._get_session_factory", lambda: factory)
        monkeypatch.setattr(llm_usage_service, "_persist_row", REAL_PERSIST_ROW)
        async with factory() as session:
            yield session
    finally:
        await trans.rollback()
        await conn.close()
        await engine.dispose()


C1, C2 = 987_001, 987_002  # client ids far outside real data


async def _seed(pg, **kw):
    """Persist one row through the real service path."""
    base = dict(purpose="reply", model="m", prompt_tokens=100, completion_tokens=10, latency_ms=100, client_id=C1, cost_inr=1.0)
    base.update(kw)
    await llm_usage_service.record_usage(**base)


async def test_record_usage_persists_row(pg):
    """record_usage writes a llm_usage row with every spec column populated."""
    from sqlalchemy import select

    await _seed(pg, conversation_id=11, order_id=None, error_code=None, success=True)
    row = (await pg.execute(select(LLMUsage).where(LLMUsage.client_id == C1))).scalar_one()
    assert (row.purpose, row.model, row.prompt_tokens, row.completion_tokens) == ("reply", "m", 100, 10)
    assert (row.cost_inr, row.latency_ms, row.success, row.conversation_id) == (1.0, 100, True, 11)
    assert row.created_at is not None


async def test_attribute_to_order_only_touches_unattributed_rows(pg):
    """Order creation stamps the conversation's NULL-order rows and leaves earlier orders alone."""
    await _seed(pg, conversation_id=21)
    await _seed(pg, conversation_id=21, order_id=5)
    await _seed(pg, conversation_id=22)
    assert await llm_usage_service.attribute_to_order(pg, 21, 6) == 1
    assert len(await llm_usage_service.fetch_order_rows(pg, 6)) == 1
    assert len(await llm_usage_service.fetch_order_rows(pg, 5)) == 1


async def test_usage_report_rollups_scope_and_range(pg):
    """Totals and per-day/purpose/model/conversation/order rollups, scoped to the client and date range."""
    await _seed(pg, purpose="reply", model="m", conversation_id=31, order_id=41, cost_inr=2.0, prompt_tokens=300)
    await _seed(pg, purpose="classify_intent", model="small", conversation_id=31, order_id=41, cost_inr=0.5)
    await _seed(pg, purpose="reply", model="m", conversation_id=32, cost_inr=1.0, success=False, error_code="429")
    await _seed(pg, client_id=C2, cost_inr=99.0)  # another tenant: must be excluded from C1
    rep = await llm_usage_service.usage_report(pg, client_id=C1)
    assert rep["totals"]["calls"] == 3 and rep["totals"]["cost_inr"] == pytest.approx(3.5)
    assert rep["totals"]["failures"] == 1 and rep["totals"]["prompt_tokens"] == 500
    assert {p["purpose"]: p["calls"] for p in rep["per_purpose"]} == {"reply": 2, "classify_intent": 1}
    assert rep["per_model"][0]["model"] == "m"  # most expensive first
    assert [(c["conversation_id"], c["cost_inr"]) for c in rep["per_conversation"]] == [(31, 2.5), (32, 1.0)]
    assert [(o["order_id"], o["cost_inr"]) for o in rep["per_order"]] == [(41, 2.5)]
    assert len(rep["per_day"]) == 1 and rep["per_day"][0]["calls"] == 3
    assert rep["from_date"] <= rep["to_date"]
    # admin scope (client_id=None) sees both tenants
    assert (await llm_usage_service.usage_report(pg, client_id=None))["totals"]["cost_inr"] >= 102.5
    # a range that ends before today excludes today's rows
    old = await llm_usage_service.usage_report(pg, client_id=C1, start=date(2020, 1, 1), end=date(2020, 1, 2))
    assert old["totals"]["calls"] == 0


async def test_persist_row_resolves_client_from_conversation(pg):
    """With no client_id, the row's client is looked up from the conversation."""
    from app.models.conversation import Conversation
    from sqlalchemy import select

    conv = (await pg.execute(select(Conversation).limit(1))).scalar_one_or_none()
    if conv is None:
        pytest.skip("no conversation row available in this database")
    await llm_usage_service.record_usage(purpose="reply", model="m", conversation_id=conv.id)
    row = (await pg.execute(select(LLMUsage).where(LLMUsage.conversation_id == conv.id).order_by(LLMUsage.id.desc()))).scalars().first()
    assert row.client_id == conv.client_id


# ── API ──────────────────────────────────────────────────────────────────────

_FAKE_REPORT = {
    "from_date": date(2026, 10, 1), "to_date": date(2026, 10, 6), "timezone": "Asia/Kolkata",
    "totals": {"calls": 2, "prompt_tokens": 200, "completion_tokens": 20, "cost_inr": 1.5, "failures": 0, "avg_latency_ms": 90.0},
    "per_day": [], "per_purpose": [], "per_model": [], "per_conversation": [], "per_order": [],
}


def test_usage_llm_endpoint_is_scoped_to_the_caller(client, monkeypatch):
    """GET /usage/llm always queries the authenticated client's id and forwards from/to."""
    from app.main import app
    from app.routers.auth import get_owner_client

    report = AsyncMock(return_value=dict(_FAKE_REPORT))
    monkeypatch.setattr(llm_usage_service, "usage_report", report)
    app.dependency_overrides[get_owner_client] = lambda: SimpleNamespace(id=42)
    try:
        r = client.get("/usage/llm?from=2026-10-01&to=2026-10-06&client_id=999")
    finally:
        app.dependency_overrides.pop(get_owner_client, None)
    assert r.status_code == 200 and r.json()["totals"]["cost_inr"] == 1.5 and r.json()["client_id"] == 42
    kwargs = report.await_args.kwargs
    assert kwargs["client_id"] == 42 and kwargs["start"] == date(2026, 10, 1) and kwargs["end"] == date(2026, 10, 6)


def test_usage_llm_endpoint_requires_auth(client):
    """No bearer token -> 401/403, never data."""
    assert client.get("/usage/llm").status_code in (401, 403)


def test_admin_usage_llm_requires_admin_key(client):
    """/admin/usage/llm rejects a missing or wrong X-Admin-Key."""
    assert client.get("/admin/usage/llm").status_code == 401
    assert client.get("/admin/usage/llm", headers={"X-Admin-Key": "nope"}).status_code == 401


def test_admin_usage_llm_all_clients_or_one(client, monkeypatch):
    """Admin sees all clients by default and can narrow with client_id."""
    report = AsyncMock(return_value=dict(_FAKE_REPORT))
    monkeypatch.setattr(llm_usage_service, "usage_report", report)
    h = {"X-Admin-Key": "test-admin-key"}
    assert client.get("/admin/usage/llm", headers=h).status_code == 200
    assert report.await_args.kwargs["client_id"] is None
    assert client.get("/admin/usage/llm?client_id=7", headers=h).json()["client_id"] == 7
    assert report.await_args.kwargs["client_id"] == 7


# ── CI guard ─────────────────────────────────────────────────────────────────

def test_no_direct_provider_calls_outside_llm_client():
    """Every Groq/OpenAI call must go through llm_client (llm_call/llm_transcribe) so it is recorded."""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    banned = re.compile(
        r"chat\.completions|audio\.transcriptions|AsyncGroq|\bGroq\(|AsyncOpenAI\(|llm_client\.chat\("
    )
    offenders = [
        f"{p.relative_to(app_dir)}:{i}"
        for p in app_dir.rglob("*.py") if p.name != "llm_client.py"
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if banned.search(line) and not line.lstrip().startswith("#")
    ]
    assert offenders == []


def test_default_dates_span_last_30_days():
    """With no dates the report window is the 30 days ending today (report timezone)."""
    start, end, lo, hi = llm_usage_service._bounds(None, None, "Asia/Kolkata")
    assert (end - start) == timedelta(days=29) and hi - lo == timedelta(days=30)
    assert lo.tzinfo == timezone.utc
