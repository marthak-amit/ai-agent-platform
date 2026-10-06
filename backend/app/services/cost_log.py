"""
Per-conversation message timeline + the per-order COST REPORT.

Tracks every inbound/outbound message for a conversation in memory (which path
produced it: template vs LLM) and prints a cost report when an order is placed.
The report's LLM calls and ₹ figures are NOT taken from this module: they come
from the `llm_usage` table (app/services/llm_usage_service.py), which records
every provider call with its real token usage. This module only contributes the
message counts. LLM-path message entries here are priced with the same
`llm_usage_service.compute_cost_inr` so there is a single price source.

Every entry is also persisted to the cost_log_entries table (see
app/models/cost_log.py) so cost history survives restarts/deploys and is
visible across multiple Railway workers — the in-memory _logs dict remains
the source for print_report() since that only ever needs the current
conversation's still-open order. Persistence is fire-and-forget: log()
stays a plain sync function (its call sites don't await it), so the DB
write runs as a background asyncio task and never blocks the caller.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

_logs: dict[int, list[dict]] = {}

# Section 4: every LLM call's input-token count, kept independent of the
# per-conversation _logs (which is popped after each order's report) so the
# before/after prompt-trim comparison survives across many conversations.
_all_in_tok_samples: list[int] = []


def log(
    conversation_id: int,
    direction: str,
    text: str,
    path: str = "TEMPLATE",
    model: str | None = None,
    in_tok: int = 0,
    out_tok: int = 0,
    call_kind: str = "reply",
) -> None:
    """
    Append one message entry to the in-memory log for this conversation.

    Args:
        conversation_id: PK of the Conversation row.
        direction:       'IN' or 'OUT'.
        text:            Raw message text (truncated at print time).
        path:            'TEMPLATE' (₹0) or 'LLM'.
        model:           Groq model name, only set when path is 'LLM'.
        in_tok:          Real prompt tokens from the provider response.
        out_tok:         Real completion tokens from the provider response.
        call_kind:       'classify' | 'extract' | 'reply' — which Groq call
                         site produced this entry. Only meaningful when
                         path is 'LLM'.
    """
    if path == "LLM":
        from app.services import llm_usage_service

        cost = llm_usage_service.compute_cost_inr(model or "", in_tok, out_tok)
        _all_in_tok_samples.append(in_tok)
    else:
        cost = 0.0

    entry = {
        "direction": direction,
        "text": text or "",
        "path": path,
        "model": model,
        "in_tok": in_tok,
        "out_tok": out_tok,
        "cost": cost,
        "call_kind": call_kind if path == "LLM" else None,
    }
    _logs.setdefault(conversation_id, []).append(entry)

    try:
        asyncio.get_running_loop().create_task(_persist(conversation_id, entry))
    except RuntimeError:
        # No running event loop (e.g. a script/test calling log() outside
        # asyncio) — in-memory log above still recorded it, just skip persistence.
        pass


async def _persist(conversation_id: int, entry: dict) -> None:
    """
    Write one entry to cost_log_entries. Fire-and-forget background task —
    never raises into the caller; a failed persist only means that entry is
    missing from the DB history, not a broken request.
    """
    try:
        from app.db import _get_session_factory
        from app.models.cost_log import CostLogEntry

        factory = _get_session_factory()
        async with factory() as db:
            db.add(CostLogEntry(conversation_id=conversation_id, **entry))
            await db.commit()
    except Exception as exc:
        logger.warning("cost_log persist failed for conv=%s: %s", conversation_id, exc)


async def print_report(db, conversation_id: int, order_number: str, order_id: int) -> str | None:
    """
    Log the COST REPORT for a freshly created order, sourced from `llm_usage`.

    First attributes the conversation's not-yet-attributed llm_usage rows to the order, then
    renders them with the message counts from the in-memory timeline (which is then cleared).
    Never raises — a reporting failure must not break order creation. Returns the report text.
    """
    from app.services import llm_usage_service

    entries = _logs.pop(conversation_id, [])
    try:
        await llm_usage_service.attribute_to_order(db, conversation_id, order_id)
        rows = await llm_usage_service.fetch_order_rows(db, order_id)
    except Exception as exc:
        logger.warning("cost report failed for order %s: %s", order_number, exc)
        return None

    llm_msgs = sum(1 for e in entries if e["path"] == "LLM")
    report = llm_usage_service.format_cost_report(
        order_number, rows,
        {"messages": len(entries), "template": len(entries) - llm_msgs, "llm": llm_msgs},
    )
    print(report)
    logger.info("cost_report | order:%s | conv:%s | llm_calls:%d | messages:%d | total:₹%.2f",
                order_number, conversation_id, len(rows), len(entries), sum(r.cost_inr for r in rows))
    return report


def median_input_tokens() -> float:
    """
    Return the median input-token count across every LLM call logged so far
    (process-wide, since process start or the last reset_token_samples() call).

    Used to measure the Section 4 prompt-trim win: median in_tok should drop
    sharply (target well under ~6,600, ideally ~1,500-2,000) while the golden
    set (Section 5) stays green.
    """
    if not _all_in_tok_samples:
        return 0.0
    ordered = sorted(_all_in_tok_samples)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def reset_token_samples() -> None:
    """Clear the input-token sample history (e.g. before a before/after measurement run)."""
    _all_in_tok_samples.clear()
