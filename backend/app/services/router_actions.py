"""
ROUTER_V2 front door + deterministic action handlers.

`run_router_front_door()` runs where the keyword greeting/order-status step used to run (after the
early guards, before SKU/name pinning). It either answers the turn itself (returns an
`early_result`) or hands the turn on to the legacy pipeline:

  * fast paths (₹0) and slot answers go on UNCHANGED or with a *canonical* text — the router acts as a
    normaliser ("pink wala" → "Pink", "kab aayega" never reaches the slot machine) so the existing
    slot machine / cart engine / OOS gates stay the single place that writes order state;
  * every other action is executed here from database rows and rendered from EN/HI/GU templates
    (`rt_*` keys in language_templates.py). The LLM's text is only ever used for
    general_answer / smalltalk, after passing the guard in `guard_reply_hint`.

If the LLM is unavailable or returns invalid JSON twice, `RouterOutcome.use_keyword_fallback` tells
the caller to run the old keyword detectors instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select

from app.config import get_settings
from app.schemas.router import RouterAction, RouterDecision, RouterTrace
from app.services import (
    catalogue_service, conversation_control, conversation_flow, conversation_service, customer_service,
    intent_router, knowledge_service, llm_health, payment_verification_service,
)
from app.services import order_pipeline as op
from app.services.delivery_service import order_eta_window, to_ist_date
from app.services.language_templates import format_price, get_template

logger = logging.getLogger("app.services.router_actions")

# router slot name -> Conversation column (direct writes by change_slot)
_SLOT_COLUMN = {
    "color": "selected_color", "size": "selected_size", "material": "selected_material",
    "quantity": "pending_order_quantity", "name": "customer_name", "address": "delivery_address",
    "phone": "mobile_number", "payment_method": "payment_method",
}
# legacy next_slot name -> router slot name
_NEXT_SLOT_TO_ROUTER = {
    "color": "color", "cart_item_color": "color", "size": "size", "cart_item_size": "size",
    "material": "material", "cart_item_material": "material", "quantity": "quantity",
    "cart_item_qty": "quantity", "customer_name": "name", "delivery_address": "address",
    "mobile_number": "phone", "payment_method": "payment_method", "variant_mode": "variant_mode",
    "cart_breakdown_confirm": "confirmation", "cart_breakdown": "breakdown",
}
_PASSTHROUGH_SLOTS = frozenset({"name", "address", "phone", "breakdown"})   # free text: never rewritten
_PII_SLOTS = frozenset({"name", "address", "phone"})
_ORDER_DRAFT_STAGES = frozenset({"order_collection", "awaiting_final_confirmation", "awaiting_switch_confirm"})
_MAX_LIST = 5

# General-answer guard: the model must not state facts the engine owns.
_UNSAFE_HINT_RE = re.compile(
    r"\d|₹|\brs\b|\brupee|\bsku\b|\b(dispatch\w*|shipp\w*|deliver\w*|paid|payment|refund\w*|stock|available|"
    r"discount\w*|offer\w*|free|guarantee\w*|promise\w*|order\w*|cod|upi|price\w*|cost\w*)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?।])\s+")


@dataclass
class RouterOutcome:
    """What the front door decided for this turn."""

    early_result: "op.PipelineResult | None" = None
    user_text: str = ""                    # text the legacy pipeline should see (canonicalised or original)
    force_order: bool = False              # start_order: treat the pinned product as a buy intent
    use_keyword_fallback: bool = False     # LLM unavailable/invalid → run the keyword detectors
    trace: RouterTrace = field(default_factory=RouterTrace)


# ── language ─────────────────────────────────────────────────────────────────

_LANG_FROM_ROUTER = {"en": "english", "hi": "hindi_roman", "gu": "gujarati_roman", "hinglish": "hinglish"}


def template_language(decision: RouterDecision | None, user_text: str, conv) -> str:
    """
    Template language key for the reply: script wins (Devanagari/Gujarati), else the router's
    language, else the conversation's last language.
    """
    detected = op._lang_svc.detect_language(user_text, previous_language=getattr(conv, "last_customer_language", None) or "english")
    if detected in ("hindi_devanagari", "gujarati_script"):
        return detected
    if decision is not None:
        return _LANG_FROM_ROUTER.get(decision.language, "english")
    return (getattr(conv, "last_customer_language", None) or "english").lower()


def _t(lang: str, key: str, **kw) -> str:
    """get_template shorthand."""
    return get_template(lang, key, **kw)


def _days_text(lang: str, lo: int, hi: int) -> str:
    """'3–7 business days' (or '5 business days') in the customer's language."""
    if lo == hi:
        return _t(lang, "rt_days_fixed", n=lo)
    return _t(lang, "rt_days_range", lo=lo, hi=hi)


def _fmt_date(d) -> str:
    """'12 Oct' (English month abbreviations read fine in every supported language)."""
    return f"{d:%d %b}"


def _catalogue_url(client) -> str:
    """Public catalogue link for this shop."""
    settings = get_settings()
    slug = getattr(client, "catalogue_slug", None)
    return f"{settings.public_shop_base_url}/{slug}" if slug else settings.public_shop_base_url


# ── persistence / sending helpers ────────────────────────────────────────────

async def _finish(
    db, conv, client, original_text: str, wamid, reply: str, record_usage, *, route: str, path: str = "TEMPLATE",
    model: str | None = None, result: "op.PipelineResult | None" = None,
) -> "op.PipelineResult":
    """Save the customer message + our reply, count usage, and return the PipelineResult to send."""
    op._log_route(conv.id, "LLM" if path == "LLM" else "TEMPLATE", f"router_{route}")
    try:
        await conversation_service.save_message(db, conv.id, "user", original_text, wamid=wamid)
        await conversation_service.save_message(db, conv.id, "assistant", reply, path=path, model=model)
    except Exception as exc:  # noqa: BLE001
        logger.error("router save error (%s): %s", route, exc)
    try:
        await record_usage(db, client, conv)
    except Exception as exc:  # noqa: BLE001
        logger.error("router usage tracking error (%s): %s", route, exc)
    if result is not None:
        result.text = reply
        return result
    return op.PipelineResult(text=reply)


async def _clear_menu(db, conv) -> bool:
    """Close an open 'which one?' menu (the customer moved on). Returns True when one was open."""
    had = bool(getattr(conv, "pending_choice_skus", None))
    if had:
        await op._clear_pending_choice(db, conv)
    return had


def _product_line(product) -> str:
    """'Name [SKU] — ₹price' for a product row (facts straight from the DB)."""
    return f"{product.name} [{product.sku}] — {format_price(product.price or 0)}"


async def _send_with_buttons(db, conv, client, stage, text, next_slot, pinned_product, variant_info):
    """Let the pipeline decide buttons/list/carousel for `text` (same rules as every other reply)."""
    return await op.decide_send_instruction(
        db, conv, client, stage, text, next_slot, conv.channel == "whatsapp", pinned_product, variant_info,
    )


def canonical_product_text(product, all_products: list) -> str:
    """
    The message text that makes the legacy exact-match path pin `product` deterministically.

    Legacy recognises a bare SKU only when it looks like AB12345 (2–4 letters + 4–6 digits); other
    seller SKUs ("A-001", "VR_0001") are pinned via the unique exact product name instead.
    """
    sku = (product.sku or "").strip()
    if sku and catalogue_service.extract_skus_from_text(sku) == [sku.upper()]:
        return sku
    same_name = [p for p in all_products if op._normalize_name(p.name) == op._normalize_name(product.name)]
    if len(same_name) == 1:
        return product.name
    return sku or product.name


# ── order status ─────────────────────────────────────────────────────────────

async def _find_order(db, client, conv, sender_phone: str, order_id: str | None, recent: list):
    """The order to talk about: a quoted order id (own orders only) or the newest one; None if none."""
    from app.models.order import Order

    if order_id:
        wanted = order_id.strip().upper()
        for order in recent:
            if (order.order_number or "").upper() == wanted:
                return order
        stmt = select(Order).where(
            Order.client_id == client.id,
            or_(Order.customer_phone == sender_phone, Order.conversation_id == conv.id),
            func.upper(Order.order_number) == wanted,
        )
        return (await db.execute(stmt.limit(1))).scalar_one_or_none()
    return recent[0] if recent else None


async def render_order_status(db, order, client, focus: str, lang: str) -> str:
    """
    Order-status reply for one order, entirely from DB rows and templates (never the LLM).

    focus: status | delivery | payment | items. Dates come from order_eta_window(); a pending UPI
    order gets the payment reminder; dispatched orders show courier/tracking when set.
    """
    status = order.status or "new"
    label = _t(lang, f"rt_status_{status}") or status.replace("_", " ").title()
    head = _t(lang, "rt_order_line", order_number=order.order_number, status=label)
    is_cod = (order.payment_method or "").upper() == "COD"
    total = format_price(order.total_amount or 0)
    eta = order_eta_window(order, client, await intent_router.order_product_days(db, order))

    def _eta_line() -> str:
        """Delivery estimate line for the current state of the order."""
        if status == "cancelled":
            return _t(lang, "rt_cancelled_line")
        if status == "delivered":
            when = to_ist_date(order.delivered_at)
            return _t(lang, "rt_delivered_on", date=_fmt_date(when)) if when else _t(lang, "rt_status_delivered")
        days = _days_text(lang, *eta["days"])
        if eta["state"] == "awaiting":
            return _t(lang, "rt_eta_awaiting", days=days)
        if eta["state"] == "overdue":
            return _t(lang, "rt_eta_overdue", days=days)
        return _t(lang, "rt_eta_window", date_from=_fmt_date(eta["from"]), date_to=_fmt_date(eta["to"]))

    def _tracking_line() -> str:
        """Courier/tracking line for dispatched orders (empty when the seller hasn't entered it)."""
        if status == "dispatched" and (order.courier_name or order.tracking_number):
            return _t(lang, "rt_tracking", courier=order.courier_name or "—", tracking=order.tracking_number or "—")
        return ""

    def _pay_line() -> str:
        """Payment state line."""
        if status == "cancelled":
            return _t(lang, "rt_cancelled_line")
        if status == "pending_payment" and not is_cod:
            upi_id = getattr(client, "upi_id", None)
            if upi_id:
                return _t(lang, "order_status_pay_reminder", amount=total, upi_id=upi_id)
            return _t(lang, "pay_send_screenshot")
        if status == "payment_submitted":
            return _t(lang, "pay_proof_received")
        if order.paid_at:
            return _t(lang, "rt_pay_received", date=_fmt_date(to_ist_date(order.paid_at)))
        if is_cod:
            return _t(lang, "rt_pay_cod", amount=total)
        return _t(lang, "rt_pay_received", date=_fmt_date(to_ist_date(order.created_at)))

    if focus == "items":
        return _t(lang, "rt_items_head", order_number=order.order_number, items=op._format_order_items(order), total=total)
    if focus == "delivery":
        return "\n".join(p for p in (head, _tracking_line(), _eta_line()) if p)
    if focus == "payment":
        return f"{head}\n{_pay_line()}"
    # status: the classic summary + the one line that matters for this state
    summary = _t(
        lang, "order_status_summary", order_number=order.order_number, items=op._format_order_items(order),
        total=total, status=label,
    )
    extra = {
        "pending_payment": _pay_line, "payment_submitted": _pay_line,
        "dispatched": lambda: "\n".join(p for p in (_tracking_line(), _eta_line()) if p),
        "paid": _eta_line, "confirmed": _eta_line, "processing": _eta_line,
        "new": _eta_line if is_cod else (lambda: ""),
    }.get(status, lambda: "")()
    return f"{summary}\n\n{extra}" if extra else summary


async def handle_order_status(rc, decision: RouterDecision):
    """order_status(focus): newest (or quoted) order of THIS customer, or 'no orders' + catalogue."""
    lang = template_language(decision, rc.user_text, rc.conv)
    await _clear_menu(rc.db, rc.conv)
    order = await _find_order(rc.db, rc.client, rc.conv, rc.sender_phone, decision.args.order_id, rc.ctx.order_rows)
    if order is None:
        if decision.args.order_id:
            reply = _t(lang, "order_status_id_not_found", order_number=decision.args.order_id.upper())
        else:
            reply = _t(lang, "no_orders", catalogue_url=_catalogue_url(rc.client))
        return await rc.reply(reply, "order_status_none", lang)
    reply = await render_order_status(rc.db, order, rc.client, decision.args.focus or "status", lang)
    return await rc.reply(reply, "order_status", lang)


# ── catalogue search / product questions ─────────────────────────────────────

def _norm(s: str | None) -> str:
    """Lower-case + collapsed spaces."""
    return " ".join((s or "").lower().split())


def _variant_values(product, attr: str, in_stock_only: bool = True) -> list[str]:
    """Distinct variant values (colour/size) of a product, optionally only those with stock."""
    out: list[str] = []
    for v in intent_router._active_variants(product):
        if in_stock_only and (v.stock or 0) <= 0:
            continue
        value = getattr(v, attr, None)
        if value and value not in out:
            out.append(value)
    return out


def _matches_filters(product, f) -> int:
    """Number of the customer's filters this product satisfies (price/colour/size/category); -1 = a hard miss."""
    score = 0
    price = product.price or 0
    if f.max_price is not None:
        if price > f.max_price:
            return -1
        score += 1
    if f.min_price is not None:
        if price < f.min_price:
            return -1
        score += 1
    if f.color or f.size:
        live = [v for v in intent_router._active_variants(product) if (v.stock or 0) > 0]
        want_c, want_s = _norm(f.color), _norm(f.size)

        def _color_ok(v) -> bool:
            """The variant's colour matches the requested one (substring either way: 'light pink' ~ 'pink')."""
            have = _norm(v.color)
            return bool(have) and (want_c == have or want_c in have or have in want_c)

        def _size_ok(v) -> bool:
            """The variant's size equals the requested one."""
            return _norm(v.size) == want_s

        if f.color and f.size:
            ok = any(_color_ok(v) and _size_ok(v) for v in live)       # ONE variant must satisfy both
        elif f.color:
            ok = any(_color_ok(v) for v in live) or (
                not live and not getattr(product, "has_variants", False)
                and want_c in _norm(f"{product.name} {product.description or ''}")
            )
        else:
            ok = any(_size_ok(v) for v in live)
        if not ok:
            return -1
        score += (1 if f.color else 0) + (1 if f.size else 0)
    if f.category:
        want = _norm(f.category)
        if want in _norm(f"{product.category or ''} {product.name}"):
            score += 1
        else:
            return -1
    return score


def search_products(all_products: list, query: str, f) -> list:
    """In-stock products matching the fuzzy query AND every filter; best score first, then cheapest."""
    live = [p for p in all_products if intent_router.product_in_stock(p)]
    base = (
        [(s, p) for s, p in catalogue_service.search_products_with_scores(live, query, top_k=len(live))]
        if query.strip() else [(0, p) for p in live]
    )
    hits = [(s, p) for s, p in base if _matches_filters(p, f) >= 0]
    hits.sort(key=lambda sp: (-sp[0], sp[1].price or 0))
    return [p for _, p in hits]


def closest_alternatives(all_products: list, query: str, f, k: int = 3) -> list:
    """
    Nearest in-stock options when nothing matches exactly: products that satisfy the most criteria
    (same category / available size & colour / price), then nearest to the requested price.
    """
    live = [p for p in all_products if intent_router.product_in_stock(p)]
    if not live:
        return []
    scores = {id(p): s for s, p in catalogue_service.search_products_with_scores(live, query, top_k=len(live))} if query.strip() else {}
    pool = [p for p in live if id(p) in scores] or live
    target = f.max_price if f.max_price is not None else f.min_price

    def _soft_matches(p) -> int:
        """Criteria satisfied individually (a hard miss on one doesn't disqualify an alternative)."""
        n = 0
        for single in (
            type(f)(max_price=f.max_price), type(f)(min_price=f.min_price), type(f)(color=f.color),
            type(f)(size=f.size), type(f)(category=f.category),
        ):
            n += 1 if _matches_filters(p, single) > 0 else 0
        return n

    def _key(p):
        """Sort: most criteria met, then nearest to the target price, then fuzzy score."""
        gap = abs((p.price or 0) - target) if target is not None else 0
        return (-_soft_matches(p), gap, -scores.get(id(p), 0))

    return sorted(pool, key=_key)[:k]


async def _list_reply(rc, lang: str, header_keys: list[str], products: list, *, route: str, footer: bool = True):
    """Numbered product list + an open pick-menu (outside an order) + the pipeline's button/list rules."""
    lines = []
    for key in header_keys:
        lines.append(_t(lang, key))
    lines.append("")
    lines.extend(f"{i}. {_product_line(p)}" for i, p in enumerate(products, 1))
    mid_order = rc.stage in intent_router.ORDER_ACTIVE_STAGES
    if mid_order:
        pinned = rc.pinned_product
        lines += ["", _t(lang, "rt_order_open_note", product=getattr(pinned, "name", None) or "")]
        text, result = "\n".join(lines), None
    else:
        if footer:
            lines += ["", _t(lang, "rt_search_footer")]
        skus = [p.sku for p in products if p.sku]
        await conversation_service.set_pending_choice_skus(rc.db, rc.conv.id, skus)
        rc.conv.pending_choice_skus = json.dumps(skus)
        text = "\n".join(lines)
        result = await _send_with_buttons(rc.db, rc.conv, rc.client, rc.stage, text, None, None, {})
    return await rc.reply(text, route, lang, result=result)


async def handle_search_catalog(rc, decision: RouterDecision):
    """search_catalog(query, filters): DB filter + fuzzy; 0 hits → generic not-found + closest alternatives."""
    lang = template_language(decision, rc.user_text, rc.conv)
    args = decision.args
    query = args.query or ""
    hits = search_products(rc.all_products, query, args.filters)
    mid_order = rc.stage in intent_router.ORDER_ACTIVE_STAGES
    if len(hits) == 1 and not mid_order:
        # One clear product → the normal product card (image, variants, "order?") via the exact-SKU path.
        return RouterOutcomeHandled.passthrough(rc, canonical_product_text(hits[0], rc.all_products))
    if hits:
        return await _list_reply(rc, lang, ["rt_search_header"], hits[:_MAX_LIST], route="search")
    alternatives = closest_alternatives(rc.all_products, query, args.filters)
    if not alternatives:
        reply = f"{_t(lang, 'rt_not_found')}\n{_t(lang, 'rt_browse_more', catalogue_url=_catalogue_url(rc.client))}"
        return await rc.reply(reply, "search_none", lang)
    return await _list_reply(
        rc, lang, ["rt_not_found", "rt_alternatives_header"], alternatives, route="search_alternatives",
    )


def _variant_answer(lang: str, product, f) -> str | None:
    """
    Deterministic yes/no for 'is <size>/<colour> available?' from the product's variant rows.
    None when the product has no variants to check against (the plain card is shown instead).
    """
    variants = intent_router._active_variants(product)
    if not variants or not (f.size or f.color):
        return None
    all_colors, all_sizes = _variant_values(product, "color", False), _variant_values(product, "size", False)

    def _canon(value: str | None, pool: list[str]) -> str | None:
        """Catalogue spelling of a requested value (e.g. 'xxl' → 'XXL'), or the value as typed."""
        if not value:
            return None
        return next((p for p in pool if _norm(p) == _norm(value)), value.strip())

    color, size = _canon(f.color, all_colors), _canon(f.size, all_sizes)
    ok = any(
        (v.stock or 0) > 0
        and (not color or _norm(v.color) == _norm(color))
        and (not size or _norm(v.size) == _norm(size))
        for v in variants
    )
    option = " / ".join(x for x in (color, size) if x)
    if ok:
        return f"{_t(lang, 'rt_variant_yes', option=option, product=product.name)}\n{_t(lang, 'rt_order_cta')}"
    # What IS available, given what they asked about.
    if color and not any(_norm(v.color) == _norm(color) and (v.stock or 0) > 0 for v in variants):
        options_line = _options_line(lang, "rt_opt_colors", _variant_values(product, "color"))
    elif size and color:
        sizes = [v.size for v in variants if _norm(v.color) == _norm(color) and (v.stock or 0) > 0 and v.size]
        options_line = _options_line(lang, "rt_opt_sizes", list(dict.fromkeys(sizes)))
    else:
        options_line = _options_line(lang, "rt_opt_sizes", _variant_values(product, "size"))
    return _t(lang, "rt_variant_no", option=option, product=product.name, options_line=options_line)


def _options_line(lang: str, key: str, options: list[str]) -> str:
    """' Available sizes: S, M.' — or the out-of-stock line when nothing is left."""
    return _t(lang, key, options=", ".join(options)) if options else _t(lang, "rt_opt_out")


async def handle_show_product(rc, decision: RouterDecision):
    """show_product(sku[, filters]): size/colour question → answer from variant rows; else the normal card."""
    lang = template_language(decision, rc.user_text, rc.conv)
    product = next((p for p in rc.all_products if (p.sku or "").upper() == (decision.args.sku or "").upper()), None)
    if product is None:
        return await handle_clarify(rc, decision)
    answer = _variant_answer(lang, product, decision.args.filters)
    mid_order = rc.stage in intent_router.ORDER_ACTIVE_STAGES
    if answer is not None:
        if mid_order and product.sku == rc.conv.pending_product_sku:
            answer = f"{answer}\n\n{_t(lang, 'rt_order_open_note', product=product.name)}"
        elif not mid_order:
            try:
                await conversation_service.set_last_shown_sku(rc.db, rc.conv.id, product.sku)
                rc.conv.last_shown_sku = product.sku
            except Exception as exc:  # noqa: BLE001
                logger.error("router last_shown_sku error: %s", exc)
        return await rc.reply(answer, "show_product_variant", lang)
    if mid_order:
        opts = intent_router.in_stock_options(product)
        options_line = ""
        if opts["colors"]:
            options_line += _t(lang, "rt_opt_colors", options=", ".join(opts["colors"]))
        if opts["sizes"]:
            options_line += _t(lang, "rt_opt_sizes", options=", ".join(opts["sizes"]))
        card = _t(lang, "rt_card", name=product.name, sku=product.sku, price=format_price(product.price or 0), options_line=options_line)
        return await rc.reply(
            f"{card}\n\n{_t(lang, 'rt_order_open_note', product=getattr(rc.pinned_product, 'name', '') or product.name)}",
            "show_product_card", lang,
        )
    return RouterOutcomeHandled.passthrough(rc, canonical_product_text(product, rc.all_products))


async def handle_start_order(rc, decision: RouterDecision):
    """start_order(sku): pin via the exact-SKU path, then enter order_collection (first slot question)."""
    sku = decision.args.sku
    product = next((p for p in rc.all_products if (p.sku or "").upper() == (sku or "").upper()), None)
    if product is None:
        return await handle_clarify(rc, decision)
    text = canonical_product_text(product, rc.all_products)
    if rc.stage in intent_router.ORDER_ACTIVE_STAGES and rc.conv.pending_product_sku:
        if (rc.conv.pending_product_sku or "").upper() != (sku or "").upper():
            return RouterOutcomeHandled.passthrough(rc, text)   # legacy switch-confirm owns product changes
        if rc.next_slot:                                         # already ordering it: just re-ask the open step
            return await _reask(rc, template_language(decision, rc.user_text, rc.conv), None, "start_order_resume")
        return RouterOutcomeHandled.passthrough(rc, rc.user_text)
    outcome = RouterOutcomeHandled.passthrough(rc, text)
    outcome.force_order = True
    return outcome


# ── slots ────────────────────────────────────────────────────────────────────

def _match_option(value: str | None, options: list[str]) -> str | None:
    """The catalogue spelling of `value` among `options` (exact, then unique containment), else None."""
    want = _norm(value)
    if not want:
        return None
    for o in options:
        if _norm(o) == want:
            return o
    near = [o for o in options if _norm(o) in want.split() or want in _norm(o)]
    return near[0] if len(near) == 1 else None


def _payment_value(client, value: str | None) -> str | None:
    """'COD' / 'UPI' when the value names a payment method this shop accepts, else None."""
    v = _norm(value)
    if v in ("cod", "cash", "cash on delivery") and getattr(client, "accepts_cod", False):
        return "COD"
    if v in ("upi", "online", "gpay", "phonepe", "paytm") and getattr(client, "accepts_upi", True):
        return "UPI"
    return None


def canonical_answer(rc, slot: str, value: str | None) -> str | None:
    """
    Validate an answer against the allowed options and return the canonical text the legacy slot
    machine understands ("Pink", "2", "COD", "yes", "same"), or None when it isn't acceptable.
    """
    vi = rc.variant_info or {}
    if slot in ("color", "size", "material"):
        return _match_option(value, vi.get({"color": "available_colors", "size": "available_sizes", "material": "available_materials"}[slot], []))
    if slot == "quantity":
        m = re.fullmatch(r"\s*(\d{1,4})\s*", value or "")
        return str(int(m.group(1))) if m and int(m.group(1)) >= 1 else None
    if slot == "payment_method":
        return _payment_value(rc.client, value)
    if slot == "confirmation":
        v = _norm(value)
        return "yes" if v in ("yes", "y", "haan", "ha", "ok", "confirm", "true") else "no" if v in ("no", "n", "nahi", "false", "cancel") else None
    if slot == "variant_mode":
        v = _norm(value)
        return "same" if "same" in v else "different" if "diff" in v else None
    return None


async def _reask(rc, lang: str, prefix_key: str | None, route: str):
    """Re-ask the open slot (with its options/buttons), optionally prefixed by a short template line."""
    pinned, vi = rc.pinned_product, rc.variant_info or {}
    profile = rc.profile
    question = op._build_slot_question(
        rc.next_slot, rc.conv, vi, lang, customer_profile=profile,
        accepts_cod=getattr(rc.client, "accepts_cod", False), product_name=getattr(pinned, "name", "the product"),
    )
    text = f"{_t(lang, prefix_key)}\n\n{question}" if prefix_key else question
    result = await _send_with_buttons(rc.db, rc.conv, rc.client, rc.stage, text, rc.next_slot, pinned, vi)
    return await rc.reply(text, route, lang, result=result)


async def handle_answer_slot(rc, decision: RouterDecision):
    """answer_slot(slot, value): validate; free text passes through untouched, options go on canonicalised."""
    lang = template_language(decision, rc.user_text, rc.conv)
    expected = _NEXT_SLOT_TO_ROUTER.get(rc.next_slot or "")
    slot = decision.args.slot
    if slot != expected:
        return await handle_change_slot(rc, decision)
    if slot in _PASSTHROUGH_SLOTS:
        return RouterOutcomeHandled.passthrough(rc, rc.user_text)
    canonical = canonical_answer(rc, slot, decision.args.value)
    if canonical is None:
        return await _reask(rc, lang, "rt_slot_invalid", "answer_slot_invalid")
    return RouterOutcomeHandled.passthrough(rc, canonical)


async def _variant_stock(rc, color, size, material) -> tuple[bool, int | None]:
    """(available, stock) for a colour/size/material combo of the pinned product (stock None = untracked)."""
    product = rc.pinned_product
    if not getattr(product, "has_variants", False):
        stock = product.stock
        return (stock is None or stock > 0), stock
    ok, stock = await catalogue_service.variant_available(rc.db, product.id, color, size, material)
    return ok, stock


async def handle_change_slot(rc, decision: RouterDecision):
    """
    change_slot(slot, value): validate against the product's real options/stock, write it, then
    re-render the next step (slot question or the order summary) through the normal renderers.
    """
    lang = template_language(decision, rc.user_text, rc.conv)
    conv, slot, value = rc.conv, decision.args.slot, decision.args.value
    if rc.stage in ("payment", "awaiting_switch_confirm", "completed"):
        return await rc.reply(_t(lang, "rt_change_not_now"), "change_slot_blocked", lang)
    if rc.stage not in intent_router.ORDER_SLOT_STAGES or rc.pinned_product is None:
        return await rc.reply(_t(lang, "rt_change_no_order"), "change_slot_no_order", lang)
    if conv.cart_items or getattr(conv, "cart_variant_mode", None) == "different":
        return await rc.reply(_t(lang, "rt_change_multi"), "change_slot_multi", lang)
    column = _SLOT_COLUMN.get(slot or "")
    if column is None:
        return await _reask(rc, lang, "rt_slot_invalid", "change_slot_invalid") if rc.next_slot else await rc.reply(_t(lang, "rt_slot_invalid"), "change_slot_invalid", lang)

    new_value: object | None
    clearing = slot in ("name", "address") and not (value or "").strip()
    if clearing:
        new_value = ""            # "address badalna hai": clear the slot below and ask for it again
    elif slot in ("name", "address", "phone"):
        new_value = (value or "").strip() or None
    elif slot == "quantity":
        canon = canonical_answer(rc, "quantity", value)
        new_value = int(canon) if canon else None
    elif slot == "payment_method":
        new_value = _payment_value(rc.client, value)
    else:
        new_value = canonical_answer(rc, slot, value)
    if new_value is None:
        return await rc.reply(_t(lang, "rt_slot_invalid"), "change_slot_invalid", lang) if not rc.next_slot else await _reask(rc, lang, "rt_slot_invalid", "change_slot_invalid")

    # Stock / combination checks use the values AFTER the change.
    product, vi = rc.pinned_product, rc.variant_info or {}
    color = new_value if slot == "color" else conv.selected_color
    size = new_value if slot == "size" else conv.selected_size
    material = new_value if slot == "material" else conv.selected_material
    qty = new_value if slot == "quantity" else conv.pending_order_quantity
    clear_qty = False
    if slot in ("color", "size", "material", "quantity") and (qty or slot != "quantity"):
        needs_all = all(
            (not vi.get(f"needs_{k}") or v) for k, v in (("color", color), ("size", size), ("material", material))
        )
        if needs_all and getattr(product, "has_variants", False):
            ok, stock = await _variant_stock(rc, color if vi.get("needs_color") else None, size if vi.get("needs_size") else None, material if vi.get("needs_material") else None)
            if not ok:
                options = await catalogue_service.get_in_stock_options(
                    rc.db, product.id, color=color if slot != "size" else None, size=size if slot != "color" else None,
                )
                kind, key = ("sizes", "rt_opt_sizes") if slot == "color" else ("colors", "rt_opt_colors")
                options_line = _options_line(lang, key, options[kind])
                combo = " / ".join(x for x in (color, size, material) if x)
                return await rc.reply(_t(lang, "rt_combo_oos", product=product.name, option=combo, options_line=options_line), "change_slot_oos", lang)
            if qty and stock is not None and qty > stock:
                if slot == "quantity":
                    return await rc.reply(_t(lang, "quantity_exceeds_stock", stock=str(stock), product=product.name), "change_slot_qty_stock", lang)
                clear_qty = True   # the new variant has fewer pieces than they asked for: re-ask quantity
        elif slot == "quantity" and not getattr(product, "has_variants", False) and product.stock is not None and new_value > product.stock:
            return await rc.reply(_t(lang, "quantity_exceeds_stock", stock=str(product.stock), product=product.name), "change_slot_qty_stock", lang)

    stored_value = None if clearing else new_value
    await conversation_service.update_order_field(rc.db, conv.id, column, stored_value)
    setattr(conv, column, stored_value)
    if clear_qty:
        await conversation_service.update_order_field(rc.db, conv.id, "pending_order_quantity", None)
        conv.pending_order_quantity = None
    if getattr(conv, "summary_shown", False):
        await conversation_service.update_order_field(rc.db, conv.id, "summary_shown", False)
        conv.summary_shown = False

    next_slot = conversation_flow.get_next_required_slot(conv, vi)
    stage = "order_collection" if next_slot else "awaiting_final_confirmation"
    if stage != (conv.current_stage or ""):
        await conversation_service.update_stage(rc.db, conv.id, stage)
        conv.current_stage = stage
    available_stock = None
    if not next_slot:
        try:
            ok, available_stock = await _variant_stock(rc, conv.selected_color, conv.selected_size, conv.selected_material)
        except Exception:  # noqa: BLE001
            available_stock = None
    from app.services.order_state_machine import RenderError

    try:
        body = await op._render_order_reply(
            action="ask_slot" if next_slot else "show_summary", conv=conv, db=rc.db, client=rc.client,
            next_slot=next_slot, variant_info=vi, customer_profile=rc.profile, available_stock=available_stock,
            declined_saved_address=clearing and slot == "address", lang=lang,
        )
    except RenderError as exc:
        logger.error("router change_slot render error conv=%s: %s", conv.id, exc)
        return await rc.reply(_t(lang, "rt_slot_updated", label=_label(lang, slot), value=_shown(slot, new_value)), "change_slot", lang)
    if not next_slot:
        await conversation_service.update_order_field(rc.db, conv.id, "summary_shown", True)
        conv.summary_shown = True
    updated = _t(lang, "rt_slot_updated", label=_label(lang, slot), value=_shown(slot, new_value))
    text = body if clearing else f"{updated}\n\n{body}"
    result = await _send_with_buttons(rc.db, conv, rc.client, stage, text, next_slot, product, vi)
    return await rc.reply(text, "change_slot", lang, result=result)


def _label(lang: str, slot: str) -> str:
    """Localised slot label ('Quantity', 'रंग', …)."""
    return _t(lang, f"rt_label_{slot}") or slot


def _shown(slot: str, value) -> str:
    """Value as shown in the 'Updated ✅' line (address/phone are acknowledged, not echoed back)."""
    return "✓" if slot in ("address", "phone") else str(value)


# ── cancel ───────────────────────────────────────────────────────────────────

async def handle_cancel_order(rc, decision: RouterDecision):
    """cancel_order: a draft or an unpaid order can be cancelled; verifying/paid orders are explained."""
    from app.models.order import Order

    lang = template_language(decision, rc.user_text, rc.conv)
    conv, db = rc.conv, rc.db
    await _clear_menu(db, conv)

    async def _reset_and_ok(route: str):
        """Clear the draft slots, back to greeting, 'order cancelled'."""
        for field_name, field_value in op.CANCEL_RESET_FIELDS:
            try:
                await conversation_service.update_order_field(db, conv.id, field_name, field_value)
                setattr(conv, field_name, field_value)
            except Exception as exc:  # noqa: BLE001
                logger.error("router cancel reset (%s): %s", field_name, exc)
        await conversation_service.update_stage(db, conv.id, "greeting")
        conv.current_stage = "greeting"
        return await rc.reply(_t(lang, "rt_cancel_ok"), route, lang)

    if rc.stage in _ORDER_DRAFT_STAGES and conv.pending_product_sku:
        return await _reset_and_ok("cancel_draft")
    open_orders = (await db.execute(
        select(Order).where(
            Order.client_id == rc.client.id, Order.conversation_id == conv.id,
            Order.status.in_(("pending_payment", "payment_submitted")),
        ).order_by(Order.created_at.desc())
    )).scalars().all()
    pending = next((o for o in open_orders if o.status == "pending_payment"), None)
    if pending is not None:
        try:
            await payment_verification_service.cancel_by_customer(db, pending)
        except Exception as exc:  # noqa: BLE001
            logger.error("router cancel failed order=%s: %s", pending.order_number, exc)
            return await rc.reply(_t(lang, "rt_cancel_not_allowed", order_number=pending.order_number, status=_t(lang, f"rt_status_{pending.status}")), "cancel_failed", lang)
        return await _reset_and_ok("cancel_order")
    if any(o.status == "payment_submitted" for o in open_orders):
        return await rc.reply(_t(lang, "rt_cancel_verifying"), "cancel_verifying", lang)
    latest = rc.ctx.order_rows[0] if rc.ctx.order_rows else None
    if latest is not None and latest.status not in ("cancelled",):
        return await rc.reply(
            _t(lang, "rt_cancel_not_allowed", order_number=latest.order_number, status=_t(lang, f"rt_status_{latest.status}") or latest.status),
            "cancel_not_allowed", lang,
        )
    return await rc.reply(_t(lang, "rt_cancel_none"), "cancel_none", lang)


# ── faq / handoff / greeting / free text ─────────────────────────────────────

async def handle_faq(rc, decision: RouterDecision):
    """faq: the seller's approved knowledge-base answer; nothing found → offer a human (never invent)."""
    lang = template_language(decision, rc.user_text, rc.conv)
    await _clear_menu(rc.db, rc.conv)
    query = decision.args.query or rc.user_text
    entries = []
    try:
        entries = await knowledge_service.search_knowledge(client_id=rc.client.id, query=query, db=rc.db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("router faq KB search failed: %s", exc)
    if entries:
        try:
            entries[0].usage_count = (entries[0].usage_count or 0) + 1
            await rc.db.commit()
        except Exception:  # noqa: BLE001
            pass
        return await rc.reply(entries[0].answer, "faq_kb", lang)
    return await rc.reply(_t(lang, "rt_handoff_offer"), "faq_handoff_offer", lang)


async def handle_handoff(rc, decision: RouterDecision):
    """handoff_human: pause the bot for this conversation, flag it on the dashboard, tell the customer."""
    lang = template_language(decision, rc.user_text, rc.conv)
    await _clear_menu(rc.db, rc.conv)
    try:
        rc.conv.escalation_count = (rc.conv.escalation_count or 0) + 1
        rc.conv.last_escalation_at = op.datetime.now(op.timezone.utc)
        await conversation_control.pause_bot(
            rc.db, rc.client, rc.conv, source="escalation", note="Customer asked for a human (router)",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("router handoff pause failed conv=%s: %s", rc.conv.id, exc)
    return await rc.reply(_t(lang, "rt_handoff"), "handoff", lang)


async def handle_greeting(rc, decision: RouterDecision):
    """greeting: welcome template; with a product pinned mid-browse, the welcome-back + resume line."""
    conv, db, client = rc.conv, rc.db, rc.client
    had_menu = await _clear_menu(db, conv)
    pinned = bool(conv.pending_product_sku)
    if not pinned and rc.stage in op._ROUTER_GREETING_STAGES:
        result = await op._send_plain_greeting(
            db, conv, client, rc.sender_phone, rc.user_text, rc.wamid, rc.language, rc.record_usage, had_menu,
        )
        return RouterOutcomeHandled.answered(result)
    if pinned and rc.stage in op._RESUME_GREETING_STAGES:
        resumed = await op._send_greeting_resume(
            db, conv, client, rc.sender_phone, rc.user_text, rc.wamid, rc.language, rc.record_usage, had_menu,
        )
        if resumed is not None:
            return RouterOutcomeHandled.answered(resumed)
    # Mid-order (slot / summary / payment): the slot machine owns the "welcome back + re-ask" for a bare hello.
    return RouterOutcomeHandled.passthrough(rc, "hi")


def guard_reply_hint(hint: str | None, candidates: list) -> str | None:
    """
    The only place model-written text can reach a customer. Returns the hint trimmed to 2 short
    sentences, or None when it states anything the engine owns (numbers, prices, SKUs, dates,
    stock/payment/order/delivery claims) or fails the phantom-SKU/price guard.
    """
    hint = " ".join((hint or "").split())
    if not hint or _UNSAFE_HINT_RE.search(hint) or catalogue_service._SKU_IN_REPLY_RE.search(hint):
        return None
    hint = " ".join(_SENTENCE_SPLIT_RE.split(hint)[:2])[:240].strip()
    if not hint:
        return None
    guarded = catalogue_service.guard_product_reply(hint, candidates, query=None) if candidates else hint
    return hint if guarded == hint else None


async def handle_free_text(rc, decision: RouterDecision):
    """general_answer / smalltalk: the model's one-liner after the guard; otherwise a fixed friendly template."""
    lang = template_language(decision, rc.user_text, rc.conv)
    await _clear_menu(rc.db, rc.conv)
    safe = guard_reply_hint(decision.reply_hint, rc.ctx.candidate_products)
    if safe:
        return await rc.reply(safe, f"{decision.action.value}_hint", lang, path="LLM", model=get_settings().llm_model_classifier)
    if decision.reply_hint:
        logger.info("router: reply_hint rejected by guard (conv=%s)", rc.conv.id)
    return await rc.reply(_t(lang, "rt_general_fallback"), f"{decision.action.value}_fallback", lang)


async def handle_clarify(rc, decision: RouterDecision | None):
    """Low confidence: 'Did you mean A or B?' (top 2 catalogue candidates) or 'what are you looking for?' — never echoes the text."""
    lang = template_language(decision, rc.user_text, rc.conv)
    cands = [p for p in rc.ctx.candidate_products if intent_router.product_in_stock(p)][:2]
    if len(cands) == 2:
        reply = _t(lang, "rt_clarify_two", a=_product_line(cands[0]), b=_product_line(cands[1]))
    else:
        reply = _t(lang, "rt_clarify_open")
    return await rc.reply(reply, "clarify", lang)


# ── handled-result plumbing ──────────────────────────────────────────────────

class RouterOutcomeHandled:
    """Result of one handler: either an answered turn or a (possibly rewritten) hand-off to the pipeline."""

    @staticmethod
    def answered(result: "op.PipelineResult") -> RouterOutcome:
        """The turn is fully answered."""
        return RouterOutcome(early_result=result)

    @staticmethod
    def passthrough(rc, text: str) -> RouterOutcome:
        """Continue in the legacy pipeline with `text` as the message."""
        return RouterOutcome(user_text=text)


@dataclass
class RouterCtx:
    """Per-turn bundle every handler works from (keeps handler signatures short)."""

    db: object
    conv: object
    client: object
    message: object
    user_text: str
    sender_phone: str
    wamid: str | None
    stage: str
    language: str
    record_usage: object
    ctx: intent_router.RouterContext
    all_products: list
    pinned_product: object
    variant_info: dict
    next_slot: str | None
    profile: object

    async def reply(self, text: str, route: str, lang: str, *, result=None, path: str = "TEMPLATE", model=None) -> RouterOutcome:
        """Save + return a fully answered turn."""
        pr = await _finish(
            self.db, self.conv, self.client, self.user_text, self.wamid, text, self.record_usage,
            route=route, path=path, model=model, result=result,
        )
        return RouterOutcome(early_result=pr)


_HANDLERS = {
    RouterAction.GREETING: handle_greeting,
    RouterAction.ORDER_STATUS: handle_order_status,
    RouterAction.SEARCH_CATALOG: handle_search_catalog,
    RouterAction.SHOW_PRODUCT: handle_show_product,
    RouterAction.START_ORDER: handle_start_order,
    RouterAction.ANSWER_SLOT: handle_answer_slot,
    RouterAction.CHANGE_SLOT: handle_change_slot,
    RouterAction.CANCEL_ORDER: handle_cancel_order,
    RouterAction.FAQ: handle_faq,
    RouterAction.HANDOFF_HUMAN: handle_handoff,
    RouterAction.GENERAL_ANSWER: handle_free_text,
    RouterAction.SMALLTALK: handle_free_text,
}


async def _remember_language(db, conv, lang: str, user_text: str) -> None:
    """
    Persist the customer's language like the legacy pipeline does at the end of a normal turn.

    The router answers many turns itself, so without this the welcome-back / slot templates would keep
    using a stale language. One-word messages are skipped (too ambiguous: "ok", "M", "2").
    """
    if lang == (getattr(conv, "last_customer_language", None) or "") or len(user_text.split()) < 2:
        return
    try:
        await conversation_service.update_language(db, conv.id, lang)
        conv.last_customer_language = lang
    except Exception as exc:  # noqa: BLE001
        logger.error("router language update error: %s", exc)


def _log_line(conv_id, trace: RouterTrace, version: str) -> None:
    """ONE line per turn: ROUTER conv=.. action=.. conf=.. args=.. fast_path=.. ms=.. (PII slot values redacted)."""
    args = dict(trace.args)
    if args.get("slot") in _PII_SLOTS | {"breakdown"} and "value" in args:
        args["value"] = "<redacted>"
    logger.info(
        "ROUTER conv=%s action=%s conf=%s args=%s fast_path=%s fallback=%s ms=%s v=%s",
        conv_id, trace.action or "-", f"{trace.confidence:.2f}" if trace.confidence is not None else "-",
        json.dumps(args, ensure_ascii=False, separators=(",", ":"))[:200], trace.fast_path or "-",
        trace.fallback or "-", trace.ms, version,
    )


def _args_for_log(decision: RouterDecision) -> dict:
    """Non-empty args of a decision, for the log line."""
    raw = decision.args.model_dump(exclude_none=True)
    if raw.get("query"):
        raw["query"] = intent_router.redact(raw["query"], 60)
    if not decision.args.filters or decision.args.filters.is_empty():
        raw.pop("filters", None)
    else:
        raw["filters"] = decision.args.filters.model_dump(exclude_none=True)
    return raw


async def run_router_front_door(
    db, conv, client, message, user_text: str, sender_phone: str, wamid: str | None, stored_stage: str,
    language: str, record_usage, *, llm_budget: str = "ok",
) -> RouterOutcome:
    """
    The ROUTER_V2 entry point for one inbound turn (see the module docstring).

    Returns a RouterOutcome: `early_result` when the router answered the turn; otherwise
    `user_text` (original or canonicalised) for the legacy pipeline, `force_order` for start_order,
    or `use_keyword_fallback` when the LLM could not be used.
    """
    t0 = time.perf_counter()
    settings = get_settings()
    prompt = intent_router.load_prompt()
    prompt_version = f"{prompt.version}/{hashlib.sha1(prompt.system.encode()).hexdigest()[:8]}"
    outcome = RouterOutcome(user_text=user_text)
    trace = outcome.trace

    def _done(o: RouterOutcome) -> RouterOutcome:
        """Stamp timing and write the ROUTER log line."""
        trace.ms = int((time.perf_counter() - t0) * 1000)
        _log_line(conv.id, trace, prompt_version)
        return o

    all_products = await catalogue_service.list_products(db, client.id, is_active=True)
    pinned = None
    if conv.pending_product_sku:
        pinned = await catalogue_service.find_product_by_sku(db, client.id, conv.pending_product_sku)
    variant_info: dict = {}
    if pinned is not None:
        try:
            variant_info = await catalogue_service.get_product_variant_info(db, pinned)
        except Exception as exc:  # noqa: BLE001
            logger.error("router variant_info error: %s", exc)
    if conv.channel == "whatsapp" and not getattr(conv, "mobile_number", None):
        conv.mobile_number = sender_phone    # same in-memory fill the slot machine does; the sender IS the number
    next_slot = (
        conversation_flow.get_next_required_slot(conv, variant_info)
        if pinned is not None and stored_stage in intent_router.ORDER_SLOT_STAGES else None
    )

    fast = await intent_router.fast_path_reason(
        conv, message, user_text, stored_stage, pinned_product=pinned, variant_info=variant_info,
        all_products=all_products, next_slot=next_slot,
    )
    if fast:
        trace.fast_path = fast
        return _done(outcome)
    if llm_budget == "soft":
        trace.fallback = "soft_llm_cap"
        outcome.use_keyword_fallback = True
        return _done(outcome)

    profile = None
    try:
        profile = await customer_service.get_customer(db, client_id=client.id, phone=sender_phone)
    except Exception:  # noqa: BLE001
        pass
    history = await conversation_service.get_history(db, conv.id, limit=10)
    ctx = await intent_router.build_context(
        db, conv, client, user_text, sender_phone=sender_phone, stage=stored_stage, pinned_product=pinned,
        variant_info=variant_info, all_products=all_products, profile=profile, history_rows=history,
        next_slot=next_slot,
    )
    call = await intent_router.call_router(ctx, user_text, client_id=client.id, conversation_id=conv.id)
    try:
        conv.llm_calls_today = (conv.llm_calls_today or 0) + 1
        await conversation_service.update_order_field(db, conv.id, "llm_calls_today", conv.llm_calls_today)
    except Exception as exc:  # noqa: BLE001
        logger.error("router llm_calls_today increment error: %s", exc)
    if call.decision is None:
        trace.fallback = call.error or "failed"
        outcome.use_keyword_fallback = True
        return _done(outcome)

    decision, notes = intent_router.sanitize_decision(call.decision, ctx)
    trace.action, trace.confidence, trace.args = decision.action.value, decision.confidence, _args_for_log(decision)
    if notes:
        trace.args = {**trace.args, "repaired": notes}
    if decision.confidence >= settings.router_confidence_threshold:
        await _remember_language(db, conv, template_language(decision, user_text, conv), user_text)
    rc = RouterCtx(
        db=db, conv=conv, client=client, message=message, user_text=user_text, sender_phone=sender_phone,
        wamid=wamid, stage=stored_stage, language=language, record_usage=record_usage, ctx=ctx,
        all_products=all_products, pinned_product=pinned, variant_info=variant_info, next_slot=next_slot,
        profile=profile,
    )
    try:
        if decision.confidence < settings.router_confidence_threshold:
            if next_slot in intent_router.FREE_TEXT_SLOTS:
                # Unsure while a free-text answer (name/address) is expected: it is most likely that answer.
                trace.action = f"{trace.action}->slot_passthrough"
                handled = RouterOutcomeHandled.passthrough(rc, user_text)
            else:
                trace.action = f"{trace.action}->clarify"
                handled = await handle_clarify(rc, decision)
        else:
            handled = await _HANDLERS[decision.action](rc, decision)
    except Exception as exc:  # noqa: BLE001 — a handler bug must never lose the customer's message
        logger.exception("router handler failed conv=%s action=%s: %s", conv.id, decision.action.value, exc)
        llm_health.record_failure("router_handler", exc)
        await db.rollback()
        trace.fallback = f"handler_error:{type(exc).__name__}"
        outcome.use_keyword_fallback = True
        return _done(outcome)
    handled.trace = trace
    return _done(handled)
