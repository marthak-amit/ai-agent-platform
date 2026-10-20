"""
LLM usage ledger: price a provider call, persist it to `llm_usage`, roll it up.

Every provider call goes through app/services/llm_client.py (`llm_call`,
`llm_transcribe`), which reports here. Costs come from config (USD per 1M
tokens × USD_INR); real token counts come from `response.usage`.

Context: most call sites don't know the client/conversation (e.g.
gemini_service.generate_reply). The channel-neutral pipeline entry calls
`set_context()` once per inbound message and `llm_call` falls back to it;
explicit arguments always win. A missing client_id is resolved from the
conversation at persist time.
"""

from __future__ import annotations

import contextvars
import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Date, case, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.llm_usage import LLMUsage

logger = logging.getLogger(__name__)

_context: contextvars.ContextVar[tuple[int | None, int | None]] = contextvars.ContextVar(
    "llm_usage_context", default=(None, None)
)
_warned_models: set[str] = set()
persist_failures = 0  # process-wide count of rows that failed to persist (visible in tests/debugging)

# Whisper input is OGG/Opus voice notes at roughly 24 kbit/s; used only to estimate billed seconds.
_STT_BYTES_PER_SECOND = 3000
_STT_MIN_BILLED_SECONDS = 10


# ── context ──────────────────────────────────────────────────────────────────

def set_context(client_id: int | None, conversation_id: int | None) -> None:
    """Set the ambient (client_id, conversation_id) used when a call site passes none."""
    _context.set((client_id, conversation_id))


def get_context() -> tuple[int | None, int | None]:
    """Return the ambient (client_id, conversation_id)."""
    return _context.get()


# ── pricing ──────────────────────────────────────────────────────────────────

def _prices(model: str) -> tuple[float, float, float]:
    """(input, cached_input, output) USD per 1M tokens. A two-part (in, out) entry bills cached tokens at the input rate."""
    s = get_settings()
    prices = s.llm_price_usd_per_1m.get(model)
    if prices is None:
        prices = s.llm_price_default_usd_per_1m
        if model not in _warned_models:
            _warned_models.add(model)
            logger.warning("llm_usage: no price configured for model %r — using default rate", model)
    if len(prices) == 2:
        return prices[0], prices[0], prices[1]
    return prices[0], prices[1], prices[2]


def compute_cost_usd(model: str, prompt_tokens: int, cached_tokens: int, completion_tokens: int) -> float:
    """
    USD cost of a call: uncached input × in + cached input × cached_in + output × out, per 1M tokens.

    `prompt_tokens` INCLUDES the cached ones (that is how providers report it); `completion_tokens` includes reasoning.
    Unknown models use `llm_price_default_usd_per_1m` and log one warning per model so a new model never silently costs 0.
    """
    p_in, p_cached, p_out = _prices(model)
    cached = min(max(cached_tokens, 0), max(prompt_tokens, 0))
    return ((prompt_tokens - cached) * p_in + cached * p_cached + completion_tokens * p_out) / 1_000_000


def compute_cost_inr(model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float:
    """₹ cost of a call: ``compute_cost_usd × USD_INR``."""
    return compute_cost_usd(model, prompt_tokens, cached_tokens, completion_tokens) * get_settings().usd_inr


def stt_billed_seconds(duration_s: float | None = None, audio_bytes: int = 0) -> float:
    """Seconds Whisper bills: the provider's reported duration, else an estimate from the audio size; 10 s minimum."""
    seconds = float(duration_s) if duration_s else math.ceil(audio_bytes / _STT_BYTES_PER_SECOND)
    return float(max(_STT_MIN_BILLED_SECONDS, seconds))


def stt_cost_usd(seconds: float) -> float:
    """USD for `seconds` of Whisper audio (per-hour pricing)."""
    return seconds / 3600 * get_settings().llm_stt_usd_per_hour


def stt_cost_inr(audio_bytes: int) -> float:
    """Estimated ₹ for transcribing `audio_bytes` of voice note (per-hour pricing, 10 s minimum)."""
    return stt_cost_usd(stt_billed_seconds(None, audio_bytes)) * get_settings().usd_inr


def error_code_for(exc: BaseException) -> str:
    """Short error code for a failed call: the HTTP status when present, else the exception class."""
    status = getattr(exc, "status_code", None)
    return str(status) if status else type(exc).__name__


# ── what this turn / session / conversation cost — read from the ledger, nothing else ──────────

_turn_mark: contextvars.ContextVar["tuple[int, int, datetime] | None"] = contextvars.ContextVar("llm_usage_turn_mark", default=None)
SESSION_MAX_HOURS = 24


async def mark_turn_start(db: AsyncSession, conversation_id: int) -> None:
    """
    Remember where this turn begins in the ledger: the conversation's highest row id and the DATABASE's clock.

    Both come from the database in one query, so "this turn's rows" (id > mark) and the session window never depend on the app
    server's clock matching the database's. Called once per inbound message by the pipeline; never raises.
    """
    try:
        row = (await db.execute(
            select(func.coalesce(func.max(LLMUsage.id), 0), func.now()).where(LLMUsage.conversation_id == conversation_id)
        )).one()
        _turn_mark.set((conversation_id, int(row[0]), row[1]))
    except Exception as exc:  # noqa: BLE001 — a cost line must never break a turn
        logger.warning("mark_turn_start failed conv=%s: %s", conversation_id, exc)
        _turn_mark.set(None)


async def turn_ledger(db: AsyncSession, conversation_id: int) -> dict[str, Any]:
    """
    The ledger rows written since ``mark_turn_start`` for this conversation, summed and split by purpose:
    ``{"cost_inr", "cost_usd", "prompt_tokens", "cached_tokens", "completion_tokens", "by_purpose": {purpose: {...same keys + calls}}}``.
    Zeros when the turn has not been marked or the query fails.
    """
    empty = {"cost_inr": 0.0, "cost_usd": 0.0, "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0, "calls": 0}
    out: dict[str, Any] = {**empty, "by_purpose": {}}
    mark = _turn_mark.get()
    if mark is None or mark[0] != conversation_id:
        return out
    try:
        usd = func.coalesce(LLMUsage.cost_usd, LLMUsage.cost_inr / get_settings().usd_inr)
        rows = (await db.execute(
            select(
                LLMUsage.purpose, func.count(LLMUsage.id), func.coalesce(func.sum(LLMUsage.prompt_tokens), 0),
                func.coalesce(func.sum(LLMUsage.cached_tokens), 0), func.coalesce(func.sum(LLMUsage.completion_tokens), 0),
                func.coalesce(func.sum(LLMUsage.cost_inr), 0.0), func.coalesce(func.sum(usd), 0.0),
            ).where(LLMUsage.conversation_id == conversation_id, LLMUsage.id > mark[1]).group_by(LLMUsage.purpose)
        )).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("turn_ledger failed conv=%s: %s", conversation_id, exc)
        return out
    for purpose, calls, p_tok, c_tok, o_tok, inr, usd_sum in rows:
        part = {"calls": int(calls), "prompt_tokens": int(p_tok), "cached_tokens": int(c_tok), "completion_tokens": int(o_tok),
                "cost_inr": float(inr), "cost_usd": float(usd_sum)}
        out["by_purpose"][purpose] = part
        for k, v in part.items():
            out[k] += v
    return out


async def cost_summary(db: AsyncSession, conversation_id: int) -> dict[str, Any]:
    """
    ₹ cost of a conversation, from ``llm_usage`` only:

      turn      this message's calls (see ``turn_ledger``)
      session   since the later of (the moment the conversation's latest cart was CONFIRMED — ``confirm_order`` — before this turn
                began) and 24 hours before this turn began. Nothing else resets it: not an inbound photo, not a cancelled or
                expired cart, not an order that was not placed through confirm_order. (Conversations that never had a cart — the
                legacy flow — use their last placed order, any status but cancelled, as the confirmation.)
      lifetime  every call ever made for the conversation

    Returns ``{"turn": {...}, "session_inr", "lifetime_inr"}``; zeros when the query fails.
    """
    from app.models.conversation_cart import ConversationCart
    from app.models.order import Order

    out: dict[str, Any] = {"turn": await turn_ledger(db, conversation_id), "session_inr": 0.0, "lifetime_inr": 0.0}
    try:
        mark = _turn_mark.get()
        turn_start = mark[2] if mark and mark[0] == conversation_id else (await db.execute(select(func.now()))).scalar_one()
        has_carts = (await db.execute(
            select(func.count(ConversationCart.id)).where(ConversationCart.conversation_id == conversation_id)
        )).scalar_one() > 0
        if has_carts:
            confirmed_at = (await db.execute(
                select(func.max(ConversationCart.updated_at)).where(
                    ConversationCart.conversation_id == conversation_id, ConversationCart.status == "confirmed",
                    ConversationCart.updated_at < turn_start,
                )
            )).scalar_one_or_none()
        else:
            confirmed_at = (await db.execute(
                select(func.max(Order.created_at)).where(
                    Order.conversation_id == conversation_id, Order.status != "cancelled", Order.created_at < turn_start,
                )
            )).scalar_one_or_none()
        since = turn_start - timedelta(hours=SESSION_MAX_HOURS)
        if confirmed_at is not None and confirmed_at > since:
            since = confirmed_at
        row = (await db.execute(
            select(
                func.coalesce(func.sum(case((LLMUsage.created_at >= since, LLMUsage.cost_inr), else_=0.0)), 0.0),
                func.coalesce(func.sum(LLMUsage.cost_inr), 0.0),
            ).where(LLMUsage.conversation_id == conversation_id)
        )).one()
        out["session_inr"], out["lifetime_inr"] = float(row[0]), float(row[1])
    except Exception as exc:  # noqa: BLE001
        logger.warning("cost_summary failed conv=%s: %s", conversation_id, exc)
    return out


async def backfill_cost_usd(db: AsyncSession, *, dry_run: bool = False) -> int:
    """
    One-off: set ``cost_usd = cost_inr / USD_INR`` on every ledger row where it is NULL (rows written before migration 0073).

    Idempotent (only NULL rows are touched). Returns how many rows were (or, with ``dry_run``, would be) updated.
    """
    todo = int((await db.execute(select(func.count(LLMUsage.id)).where(LLMUsage.cost_usd.is_(None)))).scalar_one())
    if todo and not dry_run:
        await db.execute(update(LLMUsage).where(LLMUsage.cost_usd.is_(None)).values(cost_usd=LLMUsage.cost_inr / get_settings().usd_inr))
        await db.commit()
    return todo


async def client_cost_last_7d(db: AsyncSession, client_id: int | None = None) -> list[dict[str, Any]]:
    """
    Per client, for the last 7 days: LLM ₹ / $, calls, confirmed orders and ``avg_inr_per_confirmed_order``.

    A confirmed order is one the customer placed in chat: any status but ``cancelled``, sandbox (simulator) orders excluded.
    Sandbox LLM calls still count towards the cost (they are real spend). Clients with no confirmed order get avg None.
    """
    from app.models.client import Client
    from app.models.order import Order

    start = func.now() - timedelta(days=7)
    usd = func.coalesce(LLMUsage.cost_usd, LLMUsage.cost_inr / get_settings().usd_inr)
    cost_q = select(LLMUsage.client_id, func.count(LLMUsage.id), func.coalesce(func.sum(LLMUsage.cost_inr), 0.0), func.coalesce(func.sum(usd), 0.0)) \
        .where(LLMUsage.created_at >= start, LLMUsage.client_id.is_not(None)).group_by(LLMUsage.client_id)
    order_q = select(Order.client_id, func.count(Order.id)) \
        .where(Order.created_at >= start, Order.status != "cancelled", Order.is_sandbox.is_(False)).group_by(Order.client_id)
    if client_id is not None:
        cost_q, order_q = cost_q.where(LLMUsage.client_id == client_id), order_q.where(Order.client_id == client_id)
    costs = {cid: (int(n), float(inr), float(u)) for cid, n, inr, u in (await db.execute(cost_q)).all()}
    orders = {cid: int(n) for cid, n in (await db.execute(order_q)).all()}
    ids = sorted(set(costs) | set(orders))
    names = {cid: name for cid, name in (await db.execute(select(Client.id, Client.business_name).where(Client.id.in_(ids)))).all()} if ids else {}
    out = []
    for cid in ids:
        calls, inr, usd_sum = costs.get(cid, (0, 0.0, 0.0))
        confirmed = orders.get(cid, 0)
        out.append({
            "client_id": cid, "business_name": names.get(cid), "llm_calls": calls, "cost_inr": round(inr, 4), "cost_usd": round(usd_sum, 6),
            "confirmed_orders": confirmed, "avg_inr_per_confirmed_order": round(inr / confirmed, 4) if confirmed else None,
        })
    return sorted(out, key=lambda r: -r["cost_inr"])


# ── recording ────────────────────────────────────────────────────────────────

async def _persist_row(row: dict[str, Any]) -> None:
    """Insert one llm_usage row in its own session (never the caller's transaction)."""
    from app.db import _get_session_factory
    from app.models.conversation import Conversation

    async with _get_session_factory()() as db:
        if row.get("client_id") is None and row.get("conversation_id") is not None:
            row["client_id"] = (await db.execute(
                select(Conversation.client_id).where(Conversation.id == row["conversation_id"])
            )).scalar_one_or_none()
        db.add(LLMUsage(**row))
        await db.commit()


async def record_usage(
    *,
    purpose: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    audio_seconds: float = 0.0,
    latency_ms: int = 0,
    success: bool = True,
    error_code: str | None = None,
    client_id: int | None = None,
    conversation_id: int | None = None,
    order_id: int | None = None,
    cost_usd: float | None = None,
    cost_inr: float | None = None,
) -> float:
    """
    Persist one call and return its ₹ cost. Never raises: a failed insert is logged and
    counted, because losing a telemetry row must not break a customer's reply.

    The cost is `cost_usd` when given (Whisper), else priced from the tokens (cached ones at the cached rate); `cost_inr` is
    USD × USD_INR unless given. The ledger row is the only record: the COST line and the reports read it back.
    """
    global persist_failures
    ctx_client, ctx_conv = get_context()
    if cost_usd is None:
        cost_usd = 0.0 if not success else (
            cost_inr / get_settings().usd_inr if cost_inr is not None
            else compute_cost_usd(model, prompt_tokens, cached_tokens, completion_tokens))
    cost = cost_inr if cost_inr is not None else cost_usd * get_settings().usd_inr
    conv = conversation_id if conversation_id is not None else ctx_conv
    row = {
        "client_id": client_id if client_id is not None else ctx_client,
        "conversation_id": conv,
        "order_id": order_id, "purpose": purpose, "model": model,
        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "cached_tokens": cached_tokens,
        "audio_seconds": audio_seconds, "cost_usd": cost_usd,
        "cost_inr": cost, "latency_ms": latency_ms, "success": success, "error_code": error_code,
    }
    try:
        await _persist_row(row)
    except Exception as exc:
        persist_failures += 1
        # Full message, not just the class: "ProgrammingError" alone hides e.g. a missing-table cause.
        logger.warning(
            "llm_usage persist failed (purpose=%s model=%s): %s: %s",
            purpose, model, type(exc).__name__, str(exc)[:500],
        )
    return cost


async def attribute_to_order(db: AsyncSession, conversation_id: int, order_id: int) -> int:
    """Stamp order_id on the conversation's not-yet-attributed rows; return how many."""
    result = await db.execute(
        update(LLMUsage)
        .where(LLMUsage.conversation_id == conversation_id, LLMUsage.order_id.is_(None))
        .values(order_id=order_id)
    )
    await db.commit()
    return result.rowcount or 0


async def fetch_order_rows(db: AsyncSession, order_id: int) -> list[LLMUsage]:
    """All llm_usage rows attributed to an order, oldest first."""
    return list((await db.execute(
        select(LLMUsage).where(LLMUsage.order_id == order_id).order_by(LLMUsage.created_at, LLMUsage.id)
    )).scalars().all())


def format_cost_report(order_number: str, rows: list[LLMUsage], message_counts: dict[str, int] | None = None) -> str:
    """
    Render the per-order COST REPORT from llm_usage rows (same layout family as before:
    per-call lines, token totals, per-purpose subtotals, total and per-message ₹).
    """
    lines = [f" COST REPORT  {order_number} ".center(70, "="),
             f"{'#':<3}{'PURPOSE':<16}{'MODEL':<26}{'TOK(in/out)':<13}{'₹':<9}{'ms':<7}OK"]
    by_purpose: dict[str, list[float]] = {}
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{i:<3}{r.purpose[:15]:<16}{r.model[:25]:<26}{f'{r.prompt_tokens}/{r.completion_tokens}':<13}"
            f"{r.cost_inr:<9.4f}{r.latency_ms:<7}{'yes' if r.success else 'ERR ' + (r.error_code or '')}"
        )
        agg = by_purpose.setdefault(r.purpose, [0, 0.0])
        agg[0] += 1
        agg[1] += r.cost_inr
    total = sum(r.cost_inr for r in rows)
    lines.append("-" * 70)
    counts = message_counts or {}
    if counts:
        lines.append(f"messages: {counts.get('messages', 0)}  | template: {counts.get('template', 0)}  | LLM: {counts.get('llm', 0)}")
    lines.append(f"LLM calls: {len(rows)} ({sum(not r.success for r in rows)} failed)  | tokens: "
                 f"{sum(r.prompt_tokens for r in rows)} in / {sum(r.completion_tokens for r in rows)} out")
    lines.append(" | ".join(f"{p}: {n} (₹{c:.4f})" for p, (n, c) in sorted(by_purpose.items())) or "no LLM calls")
    msgs = counts.get("messages", 0)
    lines.append(f"total: ₹{total:.2f}" + (f"   per-msg: ₹{total / msgs:.4f}" if msgs else ""))
    lines.append("=" * 70)
    return "\n".join(lines)


# ── reporting ────────────────────────────────────────────────────────────────

def _bounds(start: date | None, end: date | None, tz_name: str) -> tuple[date, date, datetime, datetime]:
    """Resolve [start, end] (inclusive dates in the report timezone) to UTC datetimes; default last 30 days."""
    tz = ZoneInfo(tz_name)
    end = end or datetime.now(tz).date()
    start = start or end - timedelta(days=29)
    lo = datetime.combine(start, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    return start, end, lo, hi


def _metrics() -> list:
    """Aggregate columns shared by every rollup."""
    return [
        func.count(LLMUsage.id).label("calls"),
        func.coalesce(func.sum(LLMUsage.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(LLMUsage.completion_tokens), 0).label("completion_tokens"),
        func.coalesce(func.sum(LLMUsage.cached_tokens), 0).label("cached_tokens"),
        func.coalesce(func.sum(LLMUsage.audio_seconds), 0.0).label("audio_seconds"),
        func.coalesce(func.sum(func.coalesce(LLMUsage.cost_usd, LLMUsage.cost_inr / get_settings().usd_inr)), 0.0).label("cost_usd"),
        func.coalesce(func.sum(LLMUsage.cost_inr), 0.0).label("cost_inr"),
        func.coalesce(func.sum(case((LLMUsage.success.is_(False), 1), else_=0)), 0).label("failures"),
        func.coalesce(func.avg(LLMUsage.latency_ms), 0.0).label("avg_latency_ms"),
    ]


def _clean(row: Any, *keys: str) -> dict[str, Any]:
    """Row -> plain dict with rounded floats."""
    d = {k: getattr(row, k) for k in keys}
    d.update(
        calls=int(row.calls), prompt_tokens=int(row.prompt_tokens), completion_tokens=int(row.completion_tokens),
        cached_tokens=int(row.cached_tokens), audio_seconds=round(float(row.audio_seconds), 1),
        cost_usd=round(float(row.cost_usd), 6), cost_inr=round(float(row.cost_inr), 4), failures=int(row.failures), avg_latency_ms=round(float(row.avg_latency_ms), 1),
    )
    return d


async def usage_report(
    db: AsyncSession, *, client_id: int | None, start: date | None = None, end: date | None = None, limit: int = 100,
) -> dict[str, Any]:
    """
    Roll up llm_usage for [start, end] (inclusive, in `usage_report_timezone`).

    client_id=None means every client (admin scope). Returns totals plus breakdowns per day,
    purpose, model, conversation and order; the last two are the `limit` most expensive.
    """
    from app.models.order import Order

    tz_name = get_settings().usage_report_timezone
    start, end, lo, hi = _bounds(start, end, tz_name)
    where = [LLMUsage.created_at >= lo, LLMUsage.created_at < hi]
    if client_id is not None:
        where.append(LLMUsage.client_id == client_id)

    async def grouped(cols: list, *extra_where, order_by=None, row_limit: int | None = None, join_orders: bool = False):
        """Run one GROUP BY rollup."""
        q = select(*cols, *_metrics())
        if join_orders:
            q = q.select_from(LLMUsage).outerjoin(Order, Order.id == LLMUsage.order_id)
        q = q.where(*where, *extra_where).group_by(*cols)
        q = q.order_by(order_by) if order_by is not None else q.order_by(*cols)
        if row_limit:
            q = q.limit(row_limit)
        return (await db.execute(q)).all()

    total = (await db.execute(select(*_metrics()).where(*where))).one()
    day_col = cast(func.timezone(tz_name, LLMUsage.created_at), Date).label("day")
    cost_sum = func.sum(LLMUsage.cost_inr).desc()
    order_where = [Order.created_at >= lo, Order.created_at < hi, Order.is_sandbox.is_(False)]
    if client_id is not None:
        order_where.append(Order.client_id == client_id)
    orders_created = int((await db.execute(select(func.count(Order.id)).where(*order_where))).scalar_one() or 0)
    totals = _clean(total)
    return {
        "orders_created": orders_created,
        "cost_per_order_created_inr": round(totals["cost_inr"] / orders_created, 4) if orders_created else None,
        "cost_per_order_created_usd": round(totals["cost_usd"] / orders_created, 6) if orders_created else None,
        "from_date": start, "to_date": end, "timezone": tz_name,
        "totals": totals,
        "per_day": [_clean(r, "day") for r in await grouped([day_col])],
        "per_purpose": [_clean(r, "purpose") for r in await grouped([LLMUsage.purpose], order_by=cost_sum)],
        "per_model": [_clean(r, "model") for r in await grouped([LLMUsage.model], order_by=cost_sum)],
        "per_conversation": [
            _clean(r, "conversation_id")
            for r in await grouped([LLMUsage.conversation_id], LLMUsage.conversation_id.is_not(None), order_by=cost_sum, row_limit=limit)
        ],
        "per_order": [
            _clean(r, "order_id", "order_number")
            for r in await grouped([LLMUsage.order_id, Order.order_number], LLMUsage.order_id.is_not(None),
                                   order_by=cost_sum, row_limit=limit, join_orders=True)
        ],
    }
