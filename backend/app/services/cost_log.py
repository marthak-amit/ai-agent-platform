"""
Per-conversation message cost logging.

Tracks every inbound/outbound message for a conversation in memory and
prints a cost report when an order is placed. Template replies cost ₹0;
LLM replies are priced from the real token usage returned by Groq.

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

# Groq pricing in USD per 1M tokens (verify against
# https://groq.com/pricing before relying on these for billing).
_USD_TO_INR = 83.0

_USD_RATES_PER_1M = {
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "meta-llama/llama-4-scout-17b-16e-instruct": (0.11, 0.34),
    "qwen/qwen3-32b": (0.29, 0.59),
}

# ₹ per token, keyed by model name. Edit here if Groq pricing changes.
RATES: dict[str, tuple[float, float]] = {
    model: (
        usd_in * _USD_TO_INR / 1_000_000,
        usd_out * _USD_TO_INR / 1_000_000,
    )
    for model, (usd_in, usd_out) in _USD_RATES_PER_1M.items()
}

# Fallback rate (₹/token) for any model not in RATES.
_DEFAULT_IN_RATE, _DEFAULT_OUT_RATE = RATES["llama-3.3-70b-versatile"]

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
        in_rate, out_rate = RATES.get(model, (_DEFAULT_IN_RATE, _DEFAULT_OUT_RATE))
        cost = in_tok * in_rate + out_tok * out_rate
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


def print_report(conversation_id: int, order_number: str) -> None:
    """
    Print the cost report table for a conversation and clear its log.

    Args:
        conversation_id: PK of the Conversation row.
        order_number:    The ORD-xxxx number just assigned to the new order.
    """
    entries = _logs.get(conversation_id, [])

    header = f" COST REPORT  {order_number} ".center(60, "=")
    lines = [header, f"{'#':<3}{'DIR':<5}{'PATH':<16}{'TOK(in/out)':<13}{'₹':<9}TEXT"]

    running_total = 0.0
    template_count = 0
    llm_count = 0
    total_in_tok = 0
    total_out_tok = 0
    by_kind: dict[str, dict] = {
        "classify": {"count": 0, "cost": 0.0},
        "extract": {"count": 0, "cost": 0.0},
        "reply": {"count": 0, "cost": 0.0},
    }

    pending_kinds: list[str] = []  # classify/extract LLM calls since the last OUT row
    for i, e in enumerate(entries, start=1):
        running_total += e["cost"]
        kind = e.get("call_kind")
        if e["path"] == "LLM":
            llm_count += 1
            total_in_tok += e["in_tok"]
            total_out_tok += e["out_tok"]
            tok_str = f"{e['in_tok']}/{e['out_tok']}"
            if kind in by_kind:
                by_kind[kind]["count"] += 1
                by_kind[kind]["cost"] += e["cost"]
            if kind in ("classify", "extract"):
                pending_kinds.append(kind)
            path_label = "LLM"
        else:
            template_count += 1
            tok_str = "-"
            if pending_kinds and e["direction"] == "OUT":
                path_label = "TEMPLATE+" + "+".join(k.upper() for k in dict.fromkeys(pending_kinds))
            else:
                path_label = "TEMPLATE"

        if e["direction"] == "OUT":
            pending_kinds = []

        preview = e["text"].replace("\n", " ")[:60]
        lines.append(
            f"{i:<3}{e['direction']:<5}{path_label:<16}{tok_str:<13}{e['cost']:<9.4f}\"{preview}\""
        )

    total_messages = len(entries)
    per_msg = running_total / total_messages if total_messages else 0.0

    lines.append("-" * 60)
    lines.append(f"messages: {total_messages}  | template: {template_count}  | LLM: {llm_count}")
    lines.append(f"tokens: {total_in_tok} in / {total_out_tok} out")
    lines.append(
        f"classify calls: {by_kind['classify']['count']} (₹{by_kind['classify']['cost']:.4f}) | "
        f"extract calls: {by_kind['extract']['count']} (₹{by_kind['extract']['cost']:.4f}) | "
        f"reply calls: {by_kind['reply']['count']} (₹{by_kind['reply']['cost']:.4f})"
    )
    lines.append(f"total: ₹{running_total:.2f}   per-msg: ₹{per_msg:.4f}")
    lines.append("=" * 60)

    report = "\n".join(lines)
    print(report)
    logger.info("cost_report | order:%s | conv:%s | messages:%d | total:₹%.2f",
                order_number, conversation_id, total_messages, running_total)

    _logs.pop(conversation_id, None)


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
