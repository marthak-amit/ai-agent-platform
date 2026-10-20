"""
Agent v3 — the planner: ONE structured-output LLM call that turns the customer's message into closed-set actions.

The model never answers the customer. It sees the cart, the stage, the open orders, the top catalogue hits (real variants and
stock), the shop's policies and the last six turns, and returns ``{"actions": [...], "reply_intent": "..."}`` (see
``app/schemas/agent_actions.py``). Model ``LLM_MODEL_PLANNER`` (default ``openai/gpt-oss-120b`` on Groq), temperature 0,
Groq ``json_schema`` response format; if the provider rejects the schema the call is retried once in plain JSON mode (the
schema is described in the prompt either way). Every call goes through ``llm_client.llm_call`` — one ``llm_usage`` row,
purpose ``planner``.

``plan()`` never raises: a provider failure / unusable output is counted in ``llm_health`` and returns ``None`` (the
orchestrator then answers deterministically).
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings
from app.schemas.agent_actions import parse_planner_output, union_wire_schema, wire_schema
from app.services import agent_executor as ex
from app.services import conversation_service, delivery_service, intent_router, llm_client, llm_health, llm_rate_limiter, turn_trace
from app.services.agent_cart import CartData
from app.services.agent_catalog import Catalogue, ProductView

logger = logging.getLogger("app.services.agent_planner")

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "agent_planner_v1.md"
MAX_HITS = 8
MAX_VARIANT_ROWS = 24
# A 429 (Groq's tokens-per-minute cap) is retried ONCE after this many seconds, or after the "try again in Xs" the provider
# names (capped at MAX_RETRY_WAIT_SECONDS). Tests set it to 0.
RETRY_BACKOFF_SECONDS = 1.5
MAX_RETRY_WAIT_SECONDS = 6.0
_RETRY_IN_RE = re.compile(r"try again in ([\d.]+)\s*(ms|s)", re.IGNORECASE)
RAW_LOG_CHARS = 700
_last_usage: contextvars.ContextVar[dict | None] = contextvars.ContextVar("planner_last_usage", default=None)

# ── prompt ───────────────────────────────────────────────────────────────────

def load_prompt():
    """The planner prompt (SYSTEM / USER_TEMPLATE / version), cached by ``intent_router.load_prompt``."""
    return intent_router.load_prompt(str(PROMPT_PATH))


def prompt_tag() -> str:
    """'agent_planner_v1.0/ab12cd34' — logged with every call so replies can be traced to a prompt version."""
    prompt = load_prompt()
    return f"{prompt.version}/{hashlib.sha1(prompt.system.encode()).hexdigest()[:8]}"


def render_user_prompt(template: str, **parts: str) -> str:
    """Fill the {{placeholders}} (plain replace — braces in the data are safe)."""
    out = template
    for key, value in parts.items():
        out = out.replace("{{" + key + "}}", value)
    return out


def _dump(obj) -> str:
    """Compact JSON for the prompt (non-ASCII kept — customers write Gujarati / Hindi)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def select_hits(catalogue: Catalogue, cart: CartData, message: str, last_clarify: dict | None) -> list[ProductView]:
    """
    The products the planner may mention: those already in play (cart, focus, an open product choice) plus the top keyword
    hits for the message; a message with no hits ("show me products") gets the first in-stock products so the planner
    sees what the shop really sells. At most ``MAX_HITS`` (cart products are always kept).
    """
    wanted: list[str] = list(cart.skus)
    if cart.state.get("focus_sku"):
        wanted.append(cart.state["focus_sku"])
    if last_clarify and last_clarify.get("slot") == "product":
        wanted.extend(last_clarify.get("options") or [])
    views: list[ProductView] = []
    seen: set[str] = set()
    for sku in wanted:
        v = catalogue.get(sku)
        if v is not None and v.sku not in seen:
            views.append(v)
            seen.add(v.sku)
    for v in catalogue.search(message, top_k=MAX_HITS):
        if v.sku not in seen and len(views) < MAX_HITS:
            views.append(v)
            seen.add(v.sku)
    if len(views) < min(MAX_HITS, 3):
        for v in catalogue.views:
            if v.sku not in seen and v.in_stock() and len(views) < MAX_HITS:
                views.append(v)
                seen.add(v.sku)
    return views[: max(MAX_HITS, len(cart.skus))]


def product_for_prompt(view: ProductView) -> dict:
    """
    One product as the planner sees it: sku, name, price, category and the REAL stock per variant, compactly.

    Stock is a nested map keyed by the attributes the customer chooses — ``{"Maroon": {"S": 3, "M": 4}}`` for colour × size,
    ``{"Red": 5}`` for one attribute — instead of one JSON row per variant (a 5-size × 2-colour kurti is 10 rows, ~3x the tokens).
    Every number is the same one the executor validates against.
    """
    out: dict = {"sku": view.sku, "name": view.name, "price": int(round(view.price)), "category": view.product.category}
    if view.has_variants:
        attrs = view.needed_attrs[:3]
        out["choose"] = view.needed_attrs
        if attrs:
            stock: dict = {}
            for v in view.variants[: MAX_VARIANT_ROWS * 4]:
                node = stock
                for a in attrs[:-1]:
                    node = node.setdefault(v.attr(a) or "-", {})
                node[v.attr(attrs[-1]) or "-"] = v.available
            out["stock_by"] = "/".join(attrs)
            out["stock"] = stock
        else:
            out["stock"] = view.available({})
    else:
        out["stock"] = view.available({})
    if not view.in_stock():
        out["sold_out"] = True
    return out


def shop_for_prompt(client, catalogue: Catalogue) -> dict:
    """Shop facts the planner may need to choose actions (never to quote): name, payment methods, delivery estimate."""
    lo, hi = delivery_service.order_delivery_days(client)
    return {
        "name": getattr(client, "business_name", None) or "the shop",
        "payment_methods": ex.payment_options(client),
        "delivery_business_days": [lo, hi],
        "categories": catalogue.categories()[:10],
    }


def history_block(rows: list, cart: CartData, limit: int) -> str:
    """The last `limit` messages (redacted; the customer's own name / address as stored are masked too)."""
    rows = [m for m in rows if (getattr(m, "content", None) or "").strip()][-limit:]
    if not rows:
        return "(no earlier messages)"
    secrets = [s for s in (cart.customer.get("name"), cart.customer.get("address")) if s]
    lines = []
    for m in rows:
        text = m.content
        for s in secrets:
            text = text.replace(s, "<customer detail>")
        lines.append(f"{'customer' if m.role == 'user' else 'shop'}: {intent_router.redact(text)}")
    return "\n".join(lines)


def build_messages(
    *, client, catalogue: Catalogue, cart: CartData, message: str, history_rows: list, pending_orders: list,
    last_clarify: dict | None, repair: "ex.RepairRequest | None" = None, language: str = "english",
) -> list[dict]:
    """The two chat messages (system, user) for one planner call. `language` = the customer's language (context for the one free sentence)."""
    settings = get_settings()
    prompt = load_prompt()
    hits = select_hits(catalogue, cart, message, last_clarify)
    state = {"stage": cart.stage, **cart.to_prompt(), "gaps": [g.slot for g in ex.gaps(cart, catalogue, client)]}
    orders = [
        {"number": o.order_number, "status": o.status, "total": int(round(o.total_amount or 0))} for o in pending_orders
    ] or "none"
    repair_block = ""
    if repair is not None:
        repair_block = "\nREPAIR\n" + _dump({
            "your_previous_plan_had_errors": [i.as_dict() for i in repair.issues],
            "already_applied": repair.applied,
            "cart_now": repair.cart.to_prompt(),
        }) + "\n"
    user = render_user_prompt(
        prompt.user_template, state=_dump(state), orders=_dump(orders),
        catalogue=_dump([product_for_prompt(v) for v in hits]),
        last_clarify=_dump(last_clarify) if last_clarify else "none", language=language,
        history=history_block(history_rows, cart, settings.agent_history_turns), repair=repair_block, message=message.strip(),
    )
    return [{"role": "system", "content": system_message(client, catalogue)}, {"role": "user", "content": user}]


def system_message(client, catalogue: Catalogue) -> str:
    """
    Everything that is the same on every call: the rules / action reference, then this shop's store policies.

    Providers cache the longest identical PREFIX of a prompt (the response-format schema counts too), so all static text is here
    and byte-identical call after call; the user message carries only what changes (history, state, catalogue hits, message).
    """
    prompt = load_prompt()
    return f"{prompt.system}\n\n### STORE POLICIES (this shop — the same on every call)\n{_dump(shop_for_prompt(client, catalogue))}"


# ── the call ─────────────────────────────────────────────────────────────────

async def call_model(messages: list[dict], *, client_id: int | None, conversation_id: int | None, model: str | None = None) -> dict | None:
    """
    THE place the planner touches the provider (tests replace this function with a scripted one).

    Structured output first; if the provider refuses the schema, one retry in plain JSON mode. Returns the parsed JSON
    object or None when the model's text is not JSON. Provider errors propagate to ``plan``.
    """
    settings = get_settings()
    model = model or settings.llm_model_planner
    union = {"type": "json_schema", "json_schema": {"name": "agent_plan", "strict": True, "schema": union_wire_schema()}}
    flat = {"type": "json_schema", "json_schema": {"name": "agent_plan", "strict": True, "schema": wire_schema()}}
    # Typed union first (a split_line without line_id / parts cannot even be generated); if the provider refuses it, the flat
    # schema; if it refuses that too, plain JSON mode (the actions are described in the prompt either way).
    for response_format in (union, flat, {"type": "json_object"}):
        try:
            resp = await llm_client.llm_call(
                "planner", model, messages, client_id, conversation_id,
                max_tokens=settings.agent_planner_max_tokens, temperature=0.0, response_format=response_format,
            )
        except llm_health.LLMUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            text = str(exc).lower()
            is_schema = response_format["type"] == "json_schema"
            _log_provider_error(exc, is_schema)
            schema_rejected = is_schema and ("json_schema" in text or "response_format" in text or "schema" in text or "anyof" in text)
            if schema_rejected and getattr(exc, "status_code", 400) in (400, 422):
                logger.warning("planner: provider rejected the %s schema (%s) — trying the next form", "union" if response_format is union else "flat", str(exc)[:120])
                continue
            raise
        usage = getattr(resp, "usage", None)
        _last_usage.set({
            "model": model, "prompt_tokens": llm_client._tokens(usage, "prompt_tokens"),
            "completion_tokens": llm_client._tokens(usage, "completion_tokens"), "cached_tokens": llm_client._cached_tokens(usage),
        })
        raw = llm_client.final_text(resp)
        parsed = llm_client.parse_json_object(raw)
        if parsed is None:
            logger.warning(
                "PLANNER invalid output finish=%s raw=%r", getattr(resp.choices[0], "finish_reason", None) if resp.choices else None,
                (raw or "")[:RAW_LOG_CHARS],
            )
        return parsed
    return None


def _log_provider_error(exc: BaseException, strict: bool) -> None:
    """WARNING with the provider's status and — for a failed JSON generation — the text it failed to produce."""
    body = getattr(exc, "body", None)
    failed = ""
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        failed = str((err or {}).get("failed_generation") or "")
    logger.warning(
        "PLANNER provider error status=%s strict_schema=%s detail=%r failed_generation=%r",
        getattr(exc, "status_code", None), strict, str(exc)[:RAW_LOG_CHARS], failed[:RAW_LOG_CHARS],
    )


def _retry_wait(exc: BaseException) -> float:
    """Seconds to wait before the single 429 retry: the provider's "try again in Xs" (capped), else the default backoff."""
    m = _RETRY_IN_RE.search(str(exc))
    if m and RETRY_BACKOFF_SECONDS > 0:
        wait = float(m.group(1)) / (1000.0 if m.group(2).lower() == "ms" else 1.0)
        return min(max(wait, RETRY_BACKOFF_SECONDS), MAX_RETRY_WAIT_SECONDS)
    return RETRY_BACKOFF_SECONDS


@dataclass
class CallTrace:
    """Which model answered a planner call and why an earlier one did not (for the TURN trace)."""

    model: str | None = None            # the model whose answer was used
    reason: str | None = None           # why the primary was skipped: 429:<model> · bucket:<model> · error:<model>:<status> · invalid_json:<model>
    unavailable: bool = False           # the circuit breaker is open
    last_error: BaseException | None = None
    attempts: list[str] = field(default_factory=list)
    limited: bool = False               # every model was out of capacity (bucket / 429): a limit, not a failure of the provider


def _status_of(exc: BaseException) -> str:
    """'429' / '503' / 'timeout' / the exception class — a short label for the trace."""
    status = getattr(exc, "status_code", None)
    return str(status) if status else type(exc).__name__


def _retry_after_of(exc: BaseException) -> float | None:
    """The provider's own hint after a 429: the ``retry-after`` header, else "try again in Xs" in the message."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    try:
        hint = llm_rate_limiter.parse_duration(headers.get("retry-after")) if headers is not None else None
    except Exception:  # noqa: BLE001
        hint = None
    if hint is not None:
        return hint
    m = _RETRY_IN_RE.search(str(exc))
    return float(m.group(1)) / (1000.0 if m.group(2).lower() == "ms" else 1.0) if m else None


def planner_models() -> list[str]:
    """The planner model followed by its fallback (when one is configured and different)."""
    settings = get_settings()
    primary, fallback = settings.llm_model_planner, (settings.llm_model_planner_fallback or "").strip()
    return [primary] + ([fallback] if fallback and fallback != primary else [])


async def call_with_fallback(messages: list[dict], *, client_id: int | None, conversation_id: int | None) -> tuple[dict | None, CallTrace]:
    """
    The planner call with the model chain: primary → fallback.

    Each model is first checked against the client-side token bucket (``llm_rate_limiter``; the primary may queue ≤ 2 s for room, the
    fallback hardly at all). A 429 / 5xx / timeout / unusable output moves to the next model IMMEDIATELY (no sleep). When every model
    is exhausted the result is ``(None, trace)`` and the caller answers deterministically.
    """
    settings = get_settings()
    models = planner_models()
    trace = CallTrace()
    est = llm_rate_limiter.estimate_tokens(messages, schema_chars=3500, completion=settings.agent_planner_max_tokens // 2)
    for i, model in enumerate(models):
        entry = await llm_rate_limiter.acquire(model, est, max_wait=None if i == 0 else 0.5)
        if entry is None:
            trace.reason = trace.reason or f"bucket:{model}"
            trace.attempts.append(f"bucket:{model}")
            continue
        _last_usage.set(None)
        try:
            with turn_trace.span("planner" if i == 0 else "planner_fallback"):
                raw = await call_model(messages, client_id=client_id, conversation_id=conversation_id, **({} if i == 0 else {"model": model}))
        except llm_health.LLMUnavailableError as exc:
            llm_rate_limiter.settle(model, entry, None)
            trace.unavailable, trace.reason = True, str(exc)
            return None, trace
        except Exception as exc:  # noqa: BLE001 — any provider failure means "try the next model, then answer deterministically"
            llm_rate_limiter.settle(model, entry, None)
            trace.last_error = exc
            if llm_health.is_rate_limited(exc):
                llm_rate_limiter.note_rate_limited(model, _retry_after_of(exc))
                label = f"429:{model}"
            else:
                label = f"error:{model}:{_status_of(exc)}"
            trace.reason = trace.reason or label
            trace.attempts.append(label)
            logger.warning("PLANNER provider failure %s — %s", label, "trying the fallback model now" if i + 1 < len(models) else "no fallback left")
            continue
        usage = _last_usage.get() or {}
        llm_rate_limiter.settle(model, entry, (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)) or None)
        if raw is None:
            trace.reason = trace.reason or f"invalid_json:{model}"
            trace.attempts.append(f"invalid_json:{model}")
            trace.last_error = None
            continue
        trace.model, trace.last_error = model, None
        break
    if trace.model is None and trace.attempts and all(a.startswith(("bucket", "429")) for a in trace.attempts):
        trace.limited, trace.last_error = True, None
    turn_trace.note(planner_model=trace.model or "none", planner_fallback=trace.reason or "-")
    return (raw if trace.model else None), trace


async def plan(
    *, db, client, conv, catalogue: Catalogue, cart: CartData, message: str, pending_orders: list,
    last_clarify: dict | None, repair: "ex.RepairRequest | None" = None, language: str = "english",
) -> "ex.PlanOutcome | None":
    """
    One planner round → ``PlanOutcome`` (parsed plan + per-action parse issues), or None when the planner is unusable.

    Never raises. Logs ``PLANNER conv=.. round=.. actions=[..] intent=.. parse_issues=.. ms=.. v=<prompt tag>``.
    """
    t0 = time.perf_counter()
    history = await conversation_service.get_history(db, conv.id)
    messages = build_messages(
        client=client, catalogue=catalogue, cart=cart, message=message, history_rows=history, pending_orders=pending_orders,
        last_clarify=last_clarify, repair=repair, language=language,
    )
    from app.db import release_connection

    await release_connection(db)          # the LLM call takes seconds and needs no connection: do not hold one of the pool's for it
    raw, tried = await call_with_fallback(messages, client_id=getattr(client, "id", None), conversation_id=conv.id)
    if raw is None and tried.unavailable:
        logger.warning("PLANNER conv=%s unavailable: %s", conv.id, tried.reason)
        return None
    if raw is None and tried.limited:
        logger.warning("PLANNER conv=%s rate limited on every model (%s) — answering deterministically", conv.id, ", ".join(tried.attempts))
        return None
    if raw is None and tried.last_error is not None:
        llm_health.record_failure("planner", tried.last_error)
        return None
    if raw is None:
        llm_health.record_failure("planner_invalid_json", "no JSON object in the planner output")
        return None
    output, issues = parse_planner_output(raw)
    if issues:
        logger.warning(
            "PLANNER invalid actions conv=%s issues=%s raw=%s", conv.id, [f"{i.code}@{i.index}: {i.message}" for i in issues],
            json.dumps(raw, ensure_ascii=False)[:RAW_LOG_CHARS],
        )
    logger.info(
        "PLANNER conv=%s round=%s actions=%s intent=%s parse_issues=%s ms=%d model=%s fallback=%s v=%s",
        conv.id, "repair" if repair else "first", [a.type for a in output.actions], output.reply_intent,
        [f"{i.code}@{i.index}" for i in issues], int((time.perf_counter() - t0) * 1000), tried.model, tried.reason or "-", prompt_tag(),
    )
    return ex.PlanOutcome(output=output, parse_issues=issues)
