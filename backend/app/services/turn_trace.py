"""
Per-turn conversation trace: one log block per customer message showing WHAT came in, WHAT went out and WHY.

The pipeline already logs its decisions (``ROUTER …``, ``ROUTE …``, ``ROUTE_STEP …``, ``SIDE_QUESTION …``,
"Slot filled", "Mid-order product name …") but scattered between unrelated lines and other customers' turns. While a
turn runs, a logging handler on the ``app`` logger collects every INFO+ line that turn emits (via a ContextVar, so
concurrent turns never mix); when the turn ends ``log_turn`` prints them under the customer message and the reply::

    TURN conv=60 stage=order_collection→order_collection 412ms
      USER : 'PR42546'
      REPLY: 'Sorry, X is currently out of stock. ⏎ ⏎ Quantity?' [buttons: Yes | Change]
      WHY  :
        · router_actions: ROUTER conv=60 action=- conf=- … fast_path=exact_sku …
        · order_pipeline: Mid-order sold-out product: conv=60 … told, open step re-asked.

Switch off with ``TURN_TRACE_ENABLED=false``. Customer text appears in this line exactly as in ``CONV-TRANSCRIPT``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MAX_STEPS = 40
_STEP_CHARS = 220
_TEXT_CHARS = 320
# Lines that add noise, not reasoning (the transcript dump is multi-line; the trace's own line must not recurse).
_SKIP_PREFIXES = ("CONV-TRANSCRIPT", "SLOT-DEBUG", "TURN ", "TURN-SQL", "HTTP Request")

_steps: ContextVar["list[str] | None"] = ContextVar("turn_trace_steps", default=None)
_spans: ContextVar["dict[str, float] | None"] = ContextVar("turn_trace_spans", default=None)
_pending: ContextVar["PendingTurn | None"] = ContextVar("turn_trace_pending", default=None)
_notes: ContextVar["dict | None"] = ContextVar("turn_trace_notes", default=None)
_queries: ContextVar["list[tuple[float, str]] | None"] = ContextVar("turn_trace_queries", default=None)
SLOWEST_QUERIES = 3

# Order of the TIME line. Spans OVERLAP: `db` is the time spent inside database calls and is also part of search / planner /
# render; `wait` is the artificial reply delay plus any message-batching wait (it is deliberate, not work). `wa_connect` / `wa_request` split the WhatsApp send into opening a connection (DNS/TCP/TLS — ~0 on a warm keep-alive) and the request itself.
SPAN_ORDER = ("wait", "db", "search", "planner", "render", "localizer", "wa_send", "wa_connect", "wa_request", "image_send")


def _clip(text: str, limit: int) -> str:
    """One-line, length-limited form of *text* (newlines shown as ⏎)."""
    flat = re.sub(r"\s*\n+\s*", " ⏎ ", (text or "").strip())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class _TraceHandler(logging.Handler):
    """Appends each INFO+ ``app.*`` record to the running turn's step list (no-op outside a turn)."""

    def emit(self, record: logging.LogRecord) -> None:
        """Collect one log record into the current turn's trace, if a turn is being traced."""
        steps = _steps.get()
        if steps is None or len(steps) >= MAX_STEPS:
            return
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — a bad format string must never break the turn
            return
        if message.startswith(_SKIP_PREFIXES):
            return
        prefix = f"{record.levelname}! " if record.levelno >= logging.WARNING else ""
        steps.append(f"{prefix}{record.module}: {_clip(message, _STEP_CHARS)}")


_handler = _TraceHandler(level=logging.INFO)


def install() -> None:
    """Attach the collector to the ``app`` logger and the DB timer once (idempotent)."""
    app_logger = logging.getLogger("app")
    if _handler not in app_logger.handlers:
        app_logger.addHandler(_handler)
    _install_db_timer()


def start(credit: float = 0.0) -> float:
    """
    Begin tracing the current turn; returns a start timestamp to hand back to ``log_turn``.

    `credit` = seconds this message already waited before the pipeline ran (message batching): the TIME total counts from the
    moment the customer's message was accepted, and the wait appears as the ``wait`` span.
    """
    install()
    _steps.set([])
    _spans.set({"wait": float(credit)} if credit > 0 else {})
    _pending.set(None)
    _notes.set({})
    _queries.set([])
    return time.perf_counter() - max(0.0, float(credit))


# ── spans ────────────────────────────────────────────────────────────────────

def add_span(name: str, seconds: float) -> None:
    """Add `seconds` to the named span of the running turn (no-op outside a turn)."""
    spans = _spans.get()
    if spans is not None and seconds > 0:
        spans[name] = spans.get(name, 0.0) + seconds


@contextmanager
def span(name: str):
    """``with turn_trace.span("planner"): …`` — time a block into the turn's span `name` (works around ``await`` too)."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        add_span(name, time.perf_counter() - t0)


def note(**facts: object) -> None:
    """
    Attach facts to the running turn's header: ``stage`` (the conversation's CURRENT activity — browsing / collecting / confirming)
    and ``pending`` (orders still waiting for money: ``["ORD-2026-0068:payment_submitted"]``). The activity and the orders are
    separate things; an unpaid old order must not make a customer who is browsing look like they are "in payment".
    """
    notes = _notes.get()
    if notes is not None:
        notes.update(facts)


_STATEMENT_CLIP = 130


def summarise_statement(statement: str) -> str:
    """One-line form of a SQL statement for the slow-query list: ``SELECT … FROM orders WHERE …`` (column lists dropped)."""
    flat = re.sub(r"\s+", " ", statement or "").strip()
    head = flat.split(" ", 1)[0].upper() if flat else ""
    if head == "SELECT" and " FROM " in flat:
        flat = "SELECT …" + flat[flat.index(" FROM "):]
    return flat if len(flat) <= _STATEMENT_CLIP else flat[: _STATEMENT_CLIP - 1] + "…"


def slowest_queries(limit: int = SLOWEST_QUERIES) -> tuple[int, list[tuple[float, str]]]:
    """(statements run this turn, the `limit` slowest as (seconds, one-line SQL))."""
    queries = _queries.get() or []
    return len(queries), sorted(queries, key=lambda q: q[0], reverse=True)[:limit]


def current_spans() -> dict[str, float]:
    """A copy of the running turn's spans in seconds."""
    return dict(_spans.get() or {})


def _install_db_timer() -> None:
    """Count every statement's execution time into the ``db`` span (one global listener on the SQLAlchemy Engine class)."""
    global _db_timer_installed
    if _db_timer_installed:
        return
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    @event.listens_for(Engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        """Remember when the statement started."""
        conn.info.setdefault("_turn_trace_t0", []).append(time.perf_counter())

    @event.listens_for(Engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        """Add the statement's duration to the turn's db span."""
        starts = conn.info.get("_turn_trace_t0")
        if starts:
            took = time.perf_counter() - starts.pop()
            add_span("db", took)
            queries = _queries.get()
            if queries is not None and len(queries) < 500:
                queries.append((took, statement))

    _db_timer_installed = True


_db_timer_installed = False


def describe_reply(result: object) -> str:
    """'<text> [buttons: A | B] [list: n rows] [images: n]' for a PipelineResult (or '(no reply sent)')."""
    if result is None:
        return "(no result)"
    text = getattr(result, "text", None)
    parts = [repr(_clip(text, _TEXT_CHARS)) if text else "(no reply sent)"]
    buttons = getattr(result, "buttons", None)
    if buttons:
        parts.append("[buttons: " + " | ".join(str(getattr(b, "title", b)) for b in buttons) + "]")
    rows = getattr(result, "list_options", None)
    if rows:
        parts.append(f"[list: {len(rows)} rows]")
    images = (getattr(result, "images", None) or []) + (getattr(result, "pre_images", None) or [])
    if images:
        parts.append(f"[images: {len(images)}]")
    if getattr(result, "skip_send", False):
        parts.append("[skip_send]")
    status = getattr(result, "status", None)
    if status and status != "ok":
        parts.append(f"[status={status}]")
    return " ".join(parts)


def format_cost_line(*, turn: float, session: float, lifetime: float) -> str:
    """
    ``COST turn=₹0.15 session=₹0.32 lifetime=₹4.79`` — ₹ read from the ``llm_usage`` ledger: this message's calls, the session
    (since the conversation's last placed order, at most 24 h) and the conversation's whole life.
    """
    return f"COST turn=₹{turn:.2f} session=₹{session:.2f} lifetime=₹{lifetime:.2f}"


def format_time_line(spans: dict[str, float], total_ms: int, loop_lag: "tuple[float, float, float] | None" = None) -> str:
    """
    ``TIME total=2500ms wait=… db=… search=… planner=… render=… localizer=… wa_send=… image_send=… loop_lag=0/1/3ms`` (spans overlap, see
    SPAN_ORDER). ``loop_lag`` = (at the start of the turn, at its end, the worst seen in between), seconds: how late the event loop ran
    a zero-length hop. Near 0 means the loop was free and a slow ``db`` span is really the database; a big number means something
    synchronous held the loop (the ``LOOP_BLOCKED`` log lines show the stack).
    """
    parts = " ".join(f"{name}={int(round(spans.get(name, 0.0) * 1000))}ms" for name in SPAN_ORDER)
    lag = ""
    if loop_lag is not None:
        lag = " loop_lag=" + "/".join(str(int(round(v * 1000))) for v in loop_lag) + "ms"
    return f"TIME total={total_ms}ms {parts}{lag}"


_TABLE_RE = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+\"?(\w+)\"?", re.IGNORECASE)


def statement_key(statement: str) -> str:
    """``SELECT orders`` / ``INSERT messages`` / ``UPDATE conversations`` — what a statement does to which table (for spotting N+1 patterns)."""
    flat = (statement or "").lstrip()
    verb = flat.split(" ", 1)[0].upper() if flat else "?"
    m = _TABLE_RE.search(flat)
    return f"{verb} {m.group(1) if m else '?'}"


def repeated_statements(queries: "list[tuple[float, str]]", *, at_least: int = 3, limit: int = 5) -> list[tuple[str, int, float]]:
    """(statement key, how many times, total seconds) for the statement kinds a turn ran ``at_least``+ times — the N+1 / round-trip suspects."""
    agg: dict[str, list[float]] = {}
    for sec, sql in queries:
        agg.setdefault(statement_key(sql), []).append(sec)
    rows = [(key, len(v), sum(v)) for key, v in agg.items() if len(v) >= at_least]
    return sorted(rows, key=lambda r: r[1], reverse=True)[:limit]


def format_db_line(count: int, total_ms: int, slowest: "list[tuple[float, str]]", repeated: "list[tuple[str, int, float]] | None" = None) -> str:
    """
    ``DB   : 23 statements 1703ms · slowest: 312ms SELECT … FROM orders WHERE … | 210ms … · repeated: SELECT messages×6`` — where a slow
    turn's database time went. On a remote database every statement is a network round trip, so the COUNT matters as much as the slowest query.
    """
    top = " | ".join(f"{int(round(sec * 1000))}ms {summarise_statement(sql)}" for sec, sql in slowest)
    again = ", ".join(f"{key}×{n}" for key, n, _ in (repeated or []))
    return f"DB   : {count} statements {total_ms}ms" + (f" · slowest: {top}" if top else "") + (f" · repeated: {again}" if again else "")


def format_header(conv_id: object, stage_before: object, stage_after: object, elapsed_ms: int, notes: "dict | None" = None) -> str:
    """
    ``TURN conv=60 stage=order_collection→awaiting_order_switch_confirm 19ms`` — or, for an agent-v3 turn that reported its activity,
    ``TURN conv=60 stage=browsing pending_orders=[ORD-2026-0068:payment_submitted] 19ms``.
    """
    notes = notes or {}
    if notes.get("stage"):
        pending = notes.get("pending") or []
        return f"TURN conv={conv_id} stage={notes['stage']}" + (f" pending_orders=[{', '.join(pending)}]" if pending else "") + f" {elapsed_ms}ms"
    return f"TURN conv={conv_id} stage={stage_before}→{stage_after} {elapsed_ms}ms"


def format_turn(
    conv_id: object, stage_before: object, stage_after: object, user_text: object, result: object,
    steps: "list[str]", elapsed_ms: int, *, cost_line: str | None = None, time_line: str | None = None,
    notes: "dict | None" = None, db_line: str | None = None,
) -> str:
    """The multi-line TURN block (pure function — see the module docstring for the shape)."""
    lines = [
        format_header(conv_id, stage_before, stage_after, elapsed_ms, notes),
        f"  USER : {_clip(str(user_text or ''), _TEXT_CHARS)!r}",
        f"  REPLY: {describe_reply(result)}",
    ]
    if (notes or {}).get("planner_model"):
        n = notes
        lines.append(
            f"  PLANNER: model_used={n['planner_model']} fallback_reason={n.get('planner_fallback', '-')} "
            f"prompt_tokens={n.get('planner_prompt_tokens', 0)} cached_tokens={n.get('planner_cached_tokens', 0)}"
        )
    if cost_line:
        lines.append(f"  {cost_line}")
    if time_line:
        lines.append(f"  {time_line}")
    if db_line:
        lines.append(f"  {db_line}")
    lines.append("  WHY  :" + ("" if steps else " (no decision lines were logged)"))
    lines.extend(f"    · {step}" for step in steps)
    return "\n".join(lines)


@dataclass
class PendingTurn:
    """A finished turn whose TURN block is printed later (after the reply was sent, so the send spans are in it)."""

    started: float
    conv_id: object
    stage_before: object
    stage_after: object
    user_text: object
    reply: object
    steps: list[str] = field(default_factory=list)
    cost_line: str | None = None
    notes: dict = field(default_factory=dict)
    queries: list = field(default_factory=list)        # (seconds, sql) of every statement the turn ran
    cost_fn: object = None                             # async () -> {"turn","session","lifetime"} | None, awaited AFTER the reply was sent


def _loop_lag(p: "PendingTurn") -> "tuple[float, float, float] | None":
    """(start, end, worst) event-loop lag of the turn in seconds, or None when the start was not measured."""
    from app.services import loop_monitor

    start = p.notes.get("loop_lag_start")
    if start is None:
        return None
    return float(start), float(p.notes.get("loop_lag_end", 0.0)), max(loop_monitor.max_since(p.started), float(start), float(p.notes.get("loop_lag_end", 0.0)))


def _emit(p: PendingTurn) -> None:
    """Print one TURN block from `p` and the spans collected so far (``TURN_TRACE_SQL=1`` also lists every statement, in order)."""
    elapsed_ms = int((time.perf_counter() - p.started) * 1000)
    ordered = sorted(p.queries, key=lambda q: q[0], reverse=True)[:SLOWEST_QUERIES]
    db_ms = int(round(sum(sec for sec, _ in p.queries) * 1000))
    block = format_turn(
        p.conv_id, p.stage_before, p.stage_after, p.user_text, p.reply, p.steps, elapsed_ms,
        cost_line=p.cost_line, time_line=format_time_line(current_spans(), elapsed_ms, _loop_lag(p)), notes=p.notes,
        db_line=format_db_line(len(p.queries), db_ms, ordered, repeated_statements(p.queries)) if p.queries else None,
    )
    if os.environ.get("TURN_TRACE_SQL") == "1":
        # Still ONE logger call per turn: the statement list is part of the same block (separate calls from other threads / handlers could
        # land in the middle of it and tear lines such as "COST turn=₹0.00INFO: …").
        listing = "\n".join(f"  TURN-SQL conv={p.conv_id} #{i:02d} {sec * 1000:4.1f}ms {summarise_statement(sql)}" for i, (sec, sql) in enumerate(p.queries, start=1))
        block = f"{block}\n{listing}" if listing else block
    logger.info("%s", block)


def log_turn(
    started: float, *, enabled: bool, conv: object, stage_before: object, user_text: object, result: object,
    cost: dict | None = None, defer: bool = False, cost_fn: object = None,
) -> None:
    """
    Print the TURN block for the turn begun by ``start()`` and stop collecting the WHY steps. Never raises.

    `cost` = {"turn": …, "session": …, "lifetime": …} (₹) for the COST line. With `defer=True` the block is held back until
    ``await emit_pending()`` (the webhook calls it after sending the reply, so ``wa_send`` / ``image_send`` are in the TIME line) and the
    cost can be given as `cost_fn` (an async callable) instead: the ledger queries behind the COST line are a diagnostic, so they run
    after the customer has their answer, not before.
    """
    steps = _steps.get() or []
    _steps.set(None)
    if not enabled:
        return
    try:
        pending = PendingTurn(
            started=started, conv_id=getattr(conv, "id", None), stage_before=stage_before,
            stage_after=getattr(conv, "current_stage", None), user_text=user_text, reply=result, steps=list(steps),
            cost_line=format_cost_line(**cost) if cost else None, notes=dict(_notes.get() or {}), queries=list(_queries.get() or []),
            cost_fn=cost_fn if defer else None,
        )
        if defer:
            _pending.set(pending)
        else:
            _emit(pending)
    except Exception as exc:  # noqa: BLE001 — diagnostics must never break a reply
        logger.debug("turn trace failed: %s", exc)


async def emit_pending() -> None:
    """Print the held-back TURN block, if any, after computing its COST line (idempotent; never raises)."""
    pending = _pending.get()
    _pending.set(None)
    if pending is None:
        return
    try:
        from app.services import loop_monitor

        pending.notes["loop_lag_end"] = await loop_monitor.measure()
        if pending.cost_fn is not None and pending.cost_line is None:
            cost = await asyncio.wait_for(pending.cost_fn(), timeout=3.0)
            pending.cost_line = format_cost_line(**cost) if cost else None
        _emit(pending)
    except Exception as exc:  # noqa: BLE001
        logger.debug("turn trace failed: %s", exc)
