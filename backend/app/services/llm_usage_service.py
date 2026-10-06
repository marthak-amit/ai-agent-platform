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

def compute_cost_inr(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """
    ₹ cost of a call: (prompt × in_price + completion × out_price) / 1M × USD_INR.

    Unknown models use `llm_price_default_usd_per_1m` and log one warning per model
    so a new model never silently costs ₹0.
    """
    s = get_settings()
    prices = s.llm_price_usd_per_1m.get(model)
    if prices is None:
        prices = s.llm_price_default_usd_per_1m
        if model not in _warned_models:
            _warned_models.add(model)
            logger.warning("llm_usage: no price configured for model %r — using default rate", model)
    usd = (prompt_tokens * prices[0] + completion_tokens * prices[1]) / 1_000_000
    return usd * s.usd_inr


def stt_cost_inr(audio_bytes: int) -> float:
    """Estimated ₹ for transcribing `audio_bytes` of voice note (per-hour pricing, 10 s minimum)."""
    s = get_settings()
    seconds = max(_STT_MIN_BILLED_SECONDS, math.ceil(audio_bytes / _STT_BYTES_PER_SECOND))
    return seconds / 3600 * s.llm_stt_usd_per_hour * s.usd_inr


def error_code_for(exc: BaseException) -> str:
    """Short error code for a failed call: the HTTP status when present, else the exception class."""
    status = getattr(exc, "status_code", None)
    return str(status) if status else type(exc).__name__


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
    latency_ms: int = 0,
    success: bool = True,
    error_code: str | None = None,
    client_id: int | None = None,
    conversation_id: int | None = None,
    order_id: int | None = None,
    cost_inr: float | None = None,
) -> float:
    """
    Persist one call and return its ₹ cost. Never raises: a failed insert is logged and
    counted, because losing a telemetry row must not break a customer's reply.
    """
    global persist_failures
    ctx_client, ctx_conv = get_context()
    cost = compute_cost_inr(model, prompt_tokens, completion_tokens) if cost_inr is None else cost_inr
    row = {
        "client_id": client_id if client_id is not None else ctx_client,
        "conversation_id": conversation_id if conversation_id is not None else ctx_conv,
        "order_id": order_id, "purpose": purpose, "model": model,
        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
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
        func.coalesce(func.sum(LLMUsage.cost_inr), 0.0).label("cost_inr"),
        func.coalesce(func.sum(case((LLMUsage.success.is_(False), 1), else_=0)), 0).label("failures"),
        func.coalesce(func.avg(LLMUsage.latency_ms), 0.0).label("avg_latency_ms"),
    ]


def _clean(row: Any, *keys: str) -> dict[str, Any]:
    """Row -> plain dict with rounded floats."""
    d = {k: getattr(row, k) for k in keys}
    d.update(
        calls=int(row.calls), prompt_tokens=int(row.prompt_tokens), completion_tokens=int(row.completion_tokens),
        cost_inr=round(float(row.cost_inr), 4), failures=int(row.failures), avg_latency_ms=round(float(row.avg_latency_ms), 1),
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
    return {
        "from_date": start, "to_date": end, "timezone": tz_name,
        "totals": _clean(total),
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
