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

import difflib
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
    intent_router, knowledge_service, llm_health, order_recovery, payment_verification_service, shop_context_service,
    spam_guard,
)
from app.services import language_service as _lang_svc
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
# "Did you mean A or B?" is only shown for guesses whose name matches at least this share of the customer's words.
_CLARIFY_MIN_MATCH_SCORE = 0.6
# The model's verdicts that a deterministic "I'm lost" phrase overrules (a complaint/handoff or a real request is left alone).
_CONFUSION_OVERRIDES = frozenset({
    RouterAction.CANCEL_ORDER, RouterAction.SMALLTALK, RouterAction.GENERAL_ANSWER, RouterAction.OUT_OF_SCOPE,
    RouterAction.FAQ, RouterAction.GREETING,
})
# Invalid answers to the same colour/size question before the reply also offers the catalogue / a human.
_INVALID_OPTION_ESCAPE_AT = 2
# While a free-text slot (name/address/phone) is open, an out_of_scope verdict needs at least this confidence.
_OUT_OF_SCOPE_FREE_TEXT_MIN = 0.9

# General-answer guard: the model must not state facts the engine owns.
_UNSAFE_HINT_RE = re.compile(
    r"\d|₹|कीमत|दाम|डिलीवरी|ऑर्डर|भुगतान|पेमेंट|मुफ़्त|मुफ्त|छूट|स्टॉक|उपलब्ध|किंमत|मोफत|"
    r"કિંમત|ભાવ|ડિલિવરી|ઓર્ડર|ચુકવણી|પેમેન્ટ|મફત|ડિસ્કાઉન્ટ|સ્ટોક|ઉપલબ્ધ|\brs\b|\brupee|\bsku\b|\b(dispatch\w*|shipp\w*|deliver\w*|paid|payment|refund\w*|stock|available|"
    r"mufat|muft|kimat|keemat|daam|chhoot|discount\w*|offer\w*|free|guarantee\w*|promise\w*|order\w*|cod|upi|price\w*|cost\w*)\b",
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

def template_language(decision: RouterDecision | None, user_text: str, conv) -> str:
    """
    Language key for the reply: the conversation language, which the pipeline synced from this very
    message before routing (script / keyword detection — the customer's own words win). The router's
    guess is only used for a first, signal-less message ("ok", "2") when nothing is stored yet.
    """
    stored = op._lang_svc.normalize_language(getattr(conv, "last_customer_language", None))
    detection = op._lang_svc.detect(user_text, previous_language=stored)
    if detection.confident:
        return detection.lang
    # No signal in the text itself: trust the router's reading of a Roman-script Hindi/Gujarati message
    # only while nothing better is known (stored language still the English default).
    if decision is not None and stored == "english":
        return {"hi": "hindi_roman", "hinglish": "hindi_roman", "gu": "gujarati_roman"}.get(decision.language, stored)
    return stored


def _t(lang: str, key: str, **kw) -> str:
    """get_template shorthand."""
    return get_template(lang, key, **kw)


def _days_text(lang: str, lo: int, hi: int) -> str:
    """'3–7 business days' (or '5 business days') in the customer's language."""
    if lo == hi:
        return _t(lang, "rt_days_fixed", n=lo)
    return _t(lang, "rt_days_range", lo=lo, hi=hi)


_MONTHS = {
    "hindi_devanagari": "जनवरी फ़रवरी मार्च अप्रैल मई जून जुलाई अगस्त सितंबर अक्टूबर नवंबर दिसंबर".split(),
    "gujarati_script": "જાન્યુઆરી ફેબ્રુઆરી માર્ચ એપ્રિલ મે જૂન જુલાઈ ઑગસ્ટ સપ્ટેમ્બર ઑક્ટોબર નવેમ્બર ડિસેમ્બર".split(),
}


def _fmt_date(d, lang: str = "english") -> str:
    """'12 Oct' — month names in the customer's own script where we have them (other languages: English abbreviation)."""
    months = _MONTHS.get(lang)
    return f"{d:%d} {months[d.month - 1]}" if months else f"{d:%d %b}"


def eta_window_line(lang: str, date_from, date_to) -> str:
    """"Expected delivery: 20 Oct – 24 Oct." — or just "20 Oct" when both ends are the same day (never "20 Oct – 20 Oct")."""
    if date_from == date_to:
        return _t(lang, "rt_eta_single", date=_fmt_date(date_from, lang))
    return _t(lang, "rt_eta_window", date_from=_fmt_date(date_from, lang), date_to=_fmt_date(date_to, lang))


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
            return _t(lang, "rt_delivered_on", date=_fmt_date(when, lang)) if when else _t(lang, "rt_status_delivered")
        days = _days_text(lang, *eta["days"])
        if eta["state"] == "awaiting":
            return _t(lang, "rt_eta_awaiting", days=days)
        if eta["state"] == "overdue":
            return _t(lang, "rt_eta_overdue", days=days)
        return eta_window_line(lang, eta["from"], eta["to"])

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
            return _t(lang, "rt_pay_received", date=_fmt_date(to_ist_date(order.paid_at), lang))
        if is_cod:
            return _t(lang, "rt_pay_cod", amount=total)
        return _t(lang, "rt_pay_received", date=_fmt_date(to_ist_date(order.created_at), lang))

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
    if query.strip() and not scores:
        return []     # the shop sells nothing like what was asked ("shoes" at a kurti shop): never suggest unrelated items
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


def out_of_stock_matches(all_products: list, query: str, k: int = 2) -> list:
    """
    Sold-out catalogue products the query names — only when they match it better than anything in stock does
    ("red silk saree" with that saree sold out), so a colour/price miss on in-stock products is never reported as "out of stock".
    """
    if not query.strip():
        return []
    live = [p for p in all_products if intent_router.product_in_stock(p)]
    sold_out = [p for p in all_products if not intent_router.product_in_stock(p)]
    dead_hits = catalogue_service.search_products_with_scores(sold_out, query, top_k=k)
    if not dead_hits:
        return []
    live_hits = catalogue_service.search_products_with_scores(live, query, top_k=1)
    best_live = live_hits[0][0] if live_hits else 0
    return [p for score, p in dead_hits if score > best_live]


_PHOTO_WORDS = ("image", "images", "photo", "photos", "picture", "pictures", "foto", "fotos")
_MAX_PHOTOS = 3


def wants_photo(decision: RouterDecision | None, user_text: str) -> bool:
    """
    True when the customer asked to SEE pictures.

    The model's `want_photo` flag is trusted, but the check is also done deterministically (the legacy
    image-request regex plus a typo-tolerant match on 5+ letter words: "imagea", "photoo") so a
    mis-transcribed "images" can never silently turn into a repeat of the previous text card.
    """
    if decision is not None and decision.args.want_photo:
        return True
    if op._is_image_request(user_text or ""):
        return True
    return any(
        len(tok) >= 5 and difflib.get_close_matches(tok, _PHOTO_WORDS, n=1, cutoff=0.85)
        for tok in re.findall(r"[a-z]+", (user_text or "").lower())
    )


async def _photo_images(rc, products: list, *, captions: bool) -> list[tuple[str, str | None]]:
    """(url, caption) pairs for the products that have a usable image (watermarked at send time), max 3."""
    images: list[tuple[str, str | None]] = []
    for p in products:
        url = await op.watermarked_product_image_url(rc.client, p)
        if not url:
            continue
        images.append((url, _product_line(p) if captions else None))
        if len(images) >= _MAX_PHOTOS:
            break
    return images


def _order_note(rc, lang: str, about_skus: "list[str] | None" = None) -> str:
    """
    The "your order for X is still open — answer the last question or type cancel" line, or "".

    Only appended while that draft is genuinely live: the conversation is mid slot-collection (not at
    payment, where there is no "last question"), a product is still pinned, the draft is within the
    flow-state TTL (an abandoned one must not leak into an unrelated reply), and the reply isn't already
    about other products (`about_skus` — a search list / card of something else).
    """
    conv, pinned = rc.conv, rc.pinned_product
    if (
        pinned is None or not getattr(conv, "pending_product_sku", None)
        or rc.stage not in intent_router.ORDER_SLOT_STAGES
    ):
        return ""
    flow_at = getattr(conv, "flow_state_at", None)
    if flow_at is not None and op.datetime.now(op.timezone.utc) - flow_at > op._flow_state_ttl(rc.client):
        return ""
    if about_skus and (pinned.sku or "").upper() not in {(x or "").upper() for x in about_skus}:
        return ""
    return _t(lang, "rt_order_open_note", product=getattr(pinned, "name", None) or "")


async def _switch_offer(rc, lang: str, product) -> str:
    """
    Mid-order, the customer asked about a DIFFERENT product: offer to switch to it.

    Stashes the product as `interrupted_sku` and parks the conversation in the legacy switch-confirm stage
    (`awaiting_order_switch_confirm`, or `awaiting_afc_switch_confirm` at the summary) so the next "yes"
    switches the order to it and "no" resumes the open question — instead of a "yes" being read as an
    answer for the product that was NOT being discussed. Returns the offer line ("" when it doesn't apply).
    """
    pinned = rc.pinned_product
    if (
        pinned is None or rc.stage not in intent_router.ORDER_SLOT_STAGES
        or (product.sku or "").upper() == (pinned.sku or "").upper()
    ):
        return ""
    stage = "awaiting_afc_switch_confirm" if rc.stage == "awaiting_final_confirmation" else "awaiting_order_switch_confirm"
    try:
        await conversation_service.update_order_field(rc.db, rc.conv.id, "interrupted_sku", product.sku)
        rc.conv.interrupted_sku = product.sku
        await conversation_service.update_stage(rc.db, rc.conv.id, stage)
        rc.conv.current_stage = stage
    except Exception as exc:  # noqa: BLE001 — without the stash the offer would be a lie
        logger.error("router switch-offer stash error conv=%s: %s", rc.conv.id, exc)
        return ""
    return _t(lang, "switch_offer3", product=product.name, pinned=pinned.name)


def extract_variant_filters(product, text: str):
    """RouterFilters-like (colour, size) the text asks about, matched against `product`'s real options."""
    from types import SimpleNamespace

    colors, sizes = _variant_values(product, "color", False), _variant_values(product, "size", False)
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    color = next((c for c in colors if _norm(c) in tokens), None)
    if color is None:
        named = next((t for t in tokens if t in intent_router._COLOR_WORDS), None)
        color = intent_router._COLOR_ALIASES.get(named, named).title() if named else None
    size = next((s for s in sizes if _norm(s) in tokens), None)
    return SimpleNamespace(color=color, size=size)


def _product_card(lang: str, product) -> str:
    """'Name [SKU] — ₹price. Available colors… sizes…' from the product's in-stock variant rows."""
    opts = intent_router.in_stock_options(product)
    options_line = ""
    if opts["colors"]:
        options_line += _t(lang, "rt_opt_colors", options=", ".join(opts["colors"]))
    if opts["sizes"]:
        options_line += _t(lang, "rt_opt_sizes", options=", ".join(opts["sizes"]))
    return _t(lang, "rt_card", name=product.name, sku=product.sku, price=format_price(product.price or 0), options_line=options_line)


def _order_is_just_opened(rc) -> bool:
    """True while the draft has nothing committed yet (first questions only: no quantity / payment / summary)."""
    conv = rc.conv
    return (
        rc.stage == "order_collection"
        and not getattr(conv, "pending_order_quantity", None)
        and not getattr(conv, "payment_method", None)
        and not getattr(conv, "summary_shown", False)
        and not getattr(conv, "interrupted_sku", None)
    )


async def _own_product_reply(rc, lang: str, product, route: str, *, answer: str | None = None, images: list | None = None):
    """
    The customer asked about the product they are ALREADY ordering ("DP73950 is available?", its name, its photo).

    Never the canned "your order is still open — answer the last question or type cancel" line. A draft that
    has only just opened (no quantity yet) shows the ordinary browse card + "Would you like to order? (Yes / No)"
    with its buttons, exactly like a product the customer had not started ordering (the stage goes back to
    product_inquiry — the same demotion as `order_pipeline.run_empty_order_product_reference_guard`; a "yes"
    re-enters the order with colour / size kept). A real order (quantity given or beyond) keeps its state: the
    reply is the card (or the answer) followed by the open question itself, re-asked with its buttons.

    `answer` is a ready reply to a specific question (a colour / size check) — it is sent instead of the plain
    card. Returns None when the draft is not live / nothing applies, so the caller keeps its own reply.
    """
    if not _order_note(rc, lang, [product.sku]):
        return None                                    # not a live draft (payment stage, stale, nothing pinned)
    cta = f"\n{_t(lang, 'rt_order_cta')}"
    try:
        variant_info = await catalogue_service.get_product_variant_info(rc.db, product)
        if _order_is_just_opened(rc):
            await conversation_service.update_stage(rc.db, rc.conv.id, "product_inquiry")
            rc.conv.current_stage = "product_inquiry"
            await conversation_service.set_last_shown_sku(rc.db, rc.conv.id, product.sku)
            rc.conv.last_shown_sku = product.sku
            text = answer if answer else op.render_product_card(lang, product, variant_info, ask_order=True)
            if cta.strip() not in text:
                text = f"{text}\n{cta}"
            result = await _send_with_buttons(rc.db, rc.conv, rc.client, "product_inquiry", text, None, product, variant_info)
            if images:
                result.pre_images = images
            return await rc.reply(text, route, lang, result=result)
        body = (answer or op.render_product_card(lang, product, variant_info, ask_order=False)).replace(cta, "")
        if rc.next_slot:
            outcome = await _reask(rc, lang, None, route, prefix_text=body)
        elif rc.stage == "awaiting_final_confirmation":
            summary = await op._render_order_reply(
                action="show_summary", conv=rc.conv, db=rc.db, client=rc.client, next_slot=None, variant_info=variant_info,
                customer_profile=rc.profile, available_stock=None, declined_saved_address=False, lang=lang,
            )
            text = f"{body}\n\n{summary}"
            result = await _send_with_buttons(rc.db, rc.conv, rc.client, rc.stage, text, None, product, variant_info)
            outcome = await rc.reply(text, route, lang, result=result)
        else:
            return None
        if images and outcome.early_result is not None:
            outcome.early_result.pre_images = images
        return outcome
    except Exception as exc:  # noqa: BLE001 — the caller's own reply is the fallback
        logger.warning("router own-product reply failed conv=%s: %s", rc.conv.id, exc)
        return None


async def _photo_reply(rc, lang: str, product):
    """One product's photo + card; says so (instead of repeating the card) when no photo is on file."""
    images = await _photo_images(rc, [product], captions=False)
    card = _product_card(lang, product)
    text = card if images else f"{_t(lang, 'rt_no_photo', product=product.name)}\n\n{card}"
    if rc.stage in intent_router.ORDER_ACTIVE_STAGES and product.sku == rc.conv.pending_product_sku:
        no_photo = None if images else _t(lang, "rt_no_photo", product=product.name)
        own = await _own_product_reply(
            rc, lang, product, "show_product_photo", images=images or None,
            answer=f"{no_photo}\n\n{_product_card(lang, product)}" if no_photo else None,
        )
        if own is not None:
            return own
    if rc.stage in intent_router.ORDER_ACTIVE_STAGES:
        note = _order_note(rc, lang, [product.sku]) or await _switch_offer(rc, lang, product)
        text = f"{text}\n\n{note}" if note else text
    else:
        text = f"{text}\n\n{_t(lang, 'rt_order_cta')}"
        try:
            await conversation_service.set_last_shown_sku(rc.db, rc.conv.id, product.sku)
            rc.conv.last_shown_sku = product.sku
        except Exception as exc:  # noqa: BLE001
            logger.error("router last_shown_sku error: %s", exc)
    result = op.PipelineResult(text=text, pre_images=images or None)
    return await rc.reply(text, "show_product_photo", lang, result=result)


async def _list_reply(
    rc, lang: str, header_keys: list[str], products: list, *, route: str, footer: bool = True,
    images: list | None = None, lead_lines: list[str] | None = None,
):
    """Numbered product list + an open pick-menu (outside an order) + the pipeline's button/list rules."""
    lines = list(lead_lines or [])
    for key in header_keys:
        lines.append(_t(lang, key))
    lines.append("")
    lines.extend(f"{i}. {_product_line(p)}" for i, p in enumerate(products, 1))
    mid_order = rc.stage in intent_router.ORDER_ACTIVE_STAGES
    if mid_order:
        note = _order_note(rc, lang, [p.sku for p in products])
        if note:
            lines += ["", note]
        text = "\n".join(lines)
        result = op.PipelineResult(text=text) if images else None
    else:
        if footer:
            lines += ["", _t(lang, "rt_search_footer")]
        skus = [p.sku for p in products if p.sku]
        await conversation_service.set_pending_choice_skus(rc.db, rc.conv.id, skus)
        rc.conv.pending_choice_skus = json.dumps(skus)
        text = "\n".join(lines)
        result = await _send_with_buttons(rc.db, rc.conv, rc.client, rc.stage, text, None, None, {})
    if images and result is not None:
        result.pre_images = images
    return await rc.reply(text, route, lang, result=result)


# ── "show me more" / "another one?" — the next items of the SAME search, never the product already in play ──────────

_MORE_PHRASES: tuple[str, ...] = (
    "more products", "more like", "some more", "show more", "show me more", "more options", "more designs", "more items",
    "more of these", "any more products", "another one", "another product", "another design", "another item", "another option",
    "similar", "like that", "like this", "like it", "other options", "other products", "other designs", "other items",
    "something else", "other one", "aur dikhao", "aur batao", "aur dikhana", "aur kuch", "aur products", "aur options",
    "kuch aur", "dusra dikhao", "dusre dikhao", "doosra dikhao", "biju batavo", "biju joiye", "bija batavo", "bija joiye", "biju kai",
    "और दिखाओ", "और बताओ", "कुछ और", "दूसरा दिखाओ", "બીજું બતાવો", "બીજું કંઈક", "બીજા બતાવો", "બીજું જોઈએ",
)


def is_more_request(text: str) -> bool:
    """"show me some more products like that", "another one?", "aur dikhao" — wants further items, not the current one."""
    from app.services import command_words as cw

    norm = cw.normalize((text or "").replace("'", ""))
    if not norm:
        return False
    padded = f" {norm} "
    return any(f" {cw.normalize(p)} " in padded for p in _MORE_PHRASES)


def _ordered_skus(rc) -> set[str]:
    """SKUs of this customer's recent orders (header row + every line item), upper-case."""
    skus: set[str] = set()
    rows = list(rc.ctx.order_rows) if rc.ctx is not None and rc.ctx.order_rows else []
    for order in rows:
        if getattr(order, "product_sku", None):
            skus.add(order.product_sku.upper())
        for li in getattr(order, "line_items", None) or []:
            if getattr(li, "product_sku", None):
                skus.add(li.product_sku.upper())
    return skus


async def _recent_order_skus(rc) -> set[str]:
    """`_ordered_skus`, loading the customer's newest orders when the router context is not available."""
    skus = _ordered_skus(rc)
    if skus or rc.ctx is not None:
        return skus
    try:
        rows = await intent_router.fetch_recent_orders(rc.db, rc.client, rc.conv, rc.sender_phone)
    except Exception:  # noqa: BLE001
        return skus
    for order in rows:
        if getattr(order, "product_sku", None):
            skus.add(order.product_sku.upper())
        for li in getattr(order, "line_items", None) or []:
            if getattr(li, "product_sku", None):
                skus.add(li.product_sku.upper())
    return skus


async def _remember_search(rc, query: str, f, shown: list, *, replace: bool) -> None:
    """Store {query, filters, shown_skus, ts} so "another one?" continues this search (shown SKUs accumulate)."""
    previous = rc.conv.last_search if isinstance(getattr(rc.conv, "last_search", None), dict) else {}
    shown_skus = [] if replace else list(previous.get("shown_skus") or [])
    shown_skus += [p.sku for p in shown if p.sku and p.sku not in shown_skus]
    memo = {
        "query": query, "filters": f.model_dump(exclude_none=True) if f is not None else {},
        "shown_skus": shown_skus[-30:], "ts": op.datetime.now(op.timezone.utc).isoformat(),
    }
    try:
        await conversation_service.update_order_field(rc.db, rc.conv.id, "last_search", memo)
        rc.conv.last_search = memo
    except Exception as exc:  # noqa: BLE001 - a search memo is a nicety
        logger.error("last_search write error conv=%s: %s", rc.conv.id, exc)


async def _reference_product(rc, all_products: list):
    """The product "that" / "this" / "another one" is relative to: pinned, last shown, else the last ordered one."""
    by_sku = {(p.sku or "").upper(): p for p in all_products if p.sku}
    for sku in (rc.conv.pending_product_sku, rc.conv.last_shown_sku):
        if sku and sku.upper() in by_sku:
            return by_sku[sku.upper()]
    for sku in sorted(await _recent_order_skus(rc)):
        if sku in by_sku:
            return by_sku[sku]
    return None


async def handle_more_like_this(
    rc, lang: str, *, query: str = "", filters=None, cheaper_than: float | None = None, route: str = "search_more",
    header_key: str = "rt_more_header", none_key: str = "rt_more_none", prefix_text: str | None = None,
):
    """
    The next 3–5 items of the customer's search, without the product already in play.

    Excluded: the pinned product, the last shown one, the customer's recent orders and everything this search has
    already shown. The query is the customer's own (`last_search`, then the model's), falling back to the category of
    the product "that" refers to. Never re-pins anything: the reply is a list (WhatsApp list / buttons) whose taps
    pick a product the normal way. `cheaper_than` keeps only cheaper items (the discount follow-up).
    """
    from app.schemas.router import RouterFilters

    all_products = rc.all_products or await catalogue_service.list_products(rc.db, rc.client.id, is_active=True)
    last = rc.conv.last_search if isinstance(getattr(rc.conv, "last_search", None), dict) else {}
    f = filters if filters is not None and not filters.is_empty() else RouterFilters(**(last.get("filters") or {}))
    q = (query or last.get("query") or "").strip()
    ref = await _reference_product(rc, all_products)
    exclude = {(x or "").upper() for x in (last.get("shown_skus") or [])} | await _recent_order_skus(rc)
    exclude |= {(x or "").upper() for x in (rc.conv.pending_product_sku, rc.conv.last_shown_sku) if x}
    if ref is not None:
        exclude.add((ref.sku or "").upper())

    def _pick(pool: list) -> list:
        """In-stock, not excluded, (cheaper), best first."""
        out = [p for p in pool if (p.sku or "").upper() not in exclude]
        if cheaper_than is not None:
            out = [p for p in out if (p.price or 0) < cheaper_than]
        return out

    hits = _pick(search_products(all_products, q, f)) if q else []
    if not hits and ref is not None and ref.category:
        hits = _pick(search_products(all_products, ref.category, RouterFilters()))
    if not hits and cheaper_than is not None:
        hits = sorted(_pick([p for p in all_products if intent_router.product_in_stock(p)]), key=lambda p: -(p.price or 0))
    if not hits and not q and ref is None:
        hits = _pick([p for p in all_products if intent_router.product_in_stock(p)])
    shown = hits[:_MAX_LIST]
    if not shown:
        text = _t(lang, none_key, catalogue_url=_catalogue_url(rc.client))
        text = f"{prefix_text}\n\n{text}" if prefix_text else text
        return await rc.reply(text, f"{route}_none", lang)
    await _remember_search(rc, q or (ref.category if ref is not None and ref.category else ""), f, shown, replace=False)
    return await _list_reply(
        rc, lang, [header_key], shown, route=route, lead_lines=[prefix_text, ""] if prefix_text else None,
    )


async def handle_search_catalog(rc, decision: RouterDecision):
    """
    search_catalog(query, filters): DB filter + fuzzy.

    A sold-out product that matches the query better than anything in stock → "<product> is currently out of
    stock" (+ similar in-stock items, if any); 0 hits and nothing alike in the catalogue → "not available" + the
    catalogue link; 0 hits on filters only → generic not-found + the closest in-stock alternatives.
    """
    lang = template_language(decision, rc.user_text, rc.conv)
    args = decision.args
    query = args.query or ""
    if is_more_request(rc.user_text) and rc.stage not in intent_router.ORDER_ACTIVE_STAGES:
        # "show me some more products like that": the next items of the same search — never the product already in play.
        return await handle_more_like_this(rc, lang, query=query, filters=args.filters)
    hits = search_products(rc.all_products, query, args.filters)
    browse = _t(lang, "rt_browse_more", catalogue_url=_catalogue_url(rc.client))
    sold_out = out_of_stock_matches(rc.all_products, query)
    if sold_out:
        # The shop does sell it, it is just out of stock right now → say so (catalogue names only, never the customer's text).
        oos_line = _t(lang, "rt_search_oos", product=", ".join(p.name for p in sold_out))
        similar = hits[:_MAX_LIST] or closest_alternatives(rc.all_products, query, args.filters)
        if not similar:
            return await rc.reply(f"{oos_line}\n{browse}", "search_out_of_stock", lang)
        return await _list_reply(
            rc, lang, ["rt_alternatives_header"], similar, route="search_out_of_stock", lead_lines=[oos_line],
        )
    mid_order = rc.stage in intent_router.ORDER_ACTIVE_STAGES
    photo = wants_photo(decision, rc.user_text)
    if len(hits) == 1 and photo:
        return await _photo_reply(rc, lang, hits[0])
    if len(hits) == 1 and not mid_order:
        # One clear product → the normal product card (image, variants, "order?") via the exact-SKU path.
        return RouterOutcomeHandled.passthrough(rc, canonical_product_text(hits[0], rc.all_products))
    if hits:
        shown = hits[:_MAX_LIST]
        images = await _photo_images(rc, shown, captions=True) if photo else None
        if not mid_order:
            await _remember_search(rc, query, args.filters, shown, replace=True)
        return await _list_reply(rc, lang, ["rt_search_header"], shown, route="search", images=images)
    alternatives = closest_alternatives(rc.all_products, query, args.filters)
    if not alternatives:
        # Nothing like it in the catalogue at all: "not available" + the catalogue link, no unrelated suggestions.
        return await rc.reply(f"{_t(lang, 'rt_not_stocked')}\n{browse}", "search_none", lang)
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
    photo = wants_photo(decision, rc.user_text)
    if photo and answer is None:
        return await _photo_reply(rc, lang, product)
    if answer is not None:
        if mid_order and product.sku == rc.conv.pending_product_sku:
            images = await _photo_images(rc, [product], captions=False) if photo else []
            own = await _own_product_reply(rc, lang, product, "show_product_variant", answer=answer, images=images or None)
            if own is not None:
                return own
        elif mid_order:
            # A different product than the one being ordered: answer, then offer to switch (its own "order it?"
            # prompt is replaced — a bare "yes" must mean "switch", not an answer to the open question).
            offer = await _switch_offer(rc, lang, product)
            if offer:
                answer = f"{answer.replace(chr(10) + _t(lang, 'rt_order_cta'), '')}\n\n{offer}"
        elif not mid_order:
            try:
                await conversation_service.set_last_shown_sku(rc.db, rc.conv.id, product.sku)
                rc.conv.last_shown_sku = product.sku
            except Exception as exc:  # noqa: BLE001
                logger.error("router last_shown_sku error: %s", exc)
        images = await _photo_images(rc, [product], captions=False) if photo else []
        result = op.PipelineResult(text=answer, pre_images=images) if images else None
        if mid_order and rc.conv.interrupted_sku:
            # A switch offer is part of the answer: give it its Switch / Add both / Keep current buttons.
            result = result or op.PipelineResult(text=answer)
            result.buttons = op.switch_offer_buttons(lang, rc.conv)
        return await rc.reply(answer, "show_product_variant", lang, result=result)
    if mid_order:
        if product.sku == rc.conv.pending_product_sku:
            own = await _own_product_reply(rc, lang, product, "show_product_card")
            if own is not None:
                return own
        card = _product_card(lang, product)
        note = _order_note(rc, lang, [product.sku]) or await _switch_offer(rc, lang, product)
        buttons = op.switch_offer_buttons(lang, rc.conv) if rc.conv.interrupted_sku and not _order_note(rc, lang, [product.sku]) else None
        text = f"{card}\n\n{note}" if note else card
        return await rc.reply(text, "show_product_card", lang, result=op.PipelineResult(text=text, buttons=buttons) if buttons else None)
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


async def _reask(rc, lang: str, prefix_key: str | None, route: str, *, prefix_text: str | None = None):
    """Re-ask the open slot (with its options/buttons), optionally prefixed by a short template line."""
    pinned, vi = rc.pinned_product, rc.variant_info or {}
    profile = rc.profile
    question = op._build_slot_question(
        rc.next_slot, rc.conv, vi, lang, customer_profile=profile,
        accepts_cod=getattr(rc.client, "accepts_cod", False), product_name=getattr(pinned, "name", "the product"),
    )
    prefix = prefix_text or (_t(lang, prefix_key) if prefix_key else "")
    text = f"{prefix}\n\n{question}" if prefix else question
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
    if (
        slot == "quantity" and new_value and new_value > 1 and getattr(product, "has_variants", False)
        and not conv.cart_variant_mode and rc.next_slot != "variant_mode"
        and all((not vi.get(f"needs_{k}") or v) for k, v in (("color", color), ("size", size), ("material", material)))
    ):
        # "sorry i want 2 quantity" (order already past the split question) with the colour/size already chosen:
        # keep that choice for every piece and go straight back to the summary. The "same or different for each?"
        # question is for a customer who is deciding that, or who asks for different colours/sizes themselves.
        await conversation_service.update_order_field(rc.db, conv.id, "cart_variant_mode", "same")
        conv.cart_variant_mode = "same"
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
        """Snapshot the draft (so "continue" can restore it), clear the slots, back to greeting, 'order cancelled'."""
        await order_recovery.close_cancel_confirm(db, conv)
        await order_recovery.save_cancelled_draft(db, conv)
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


def has_cancellable_order(rc) -> bool:
    """True when there is a draft or an unpaid order that a cancel would actually act on."""
    if rc.stage in _ORDER_DRAFT_STAGES and rc.conv.pending_product_sku:
        return True
    latest = rc.ctx.order_rows[0] if rc.ctx is not None and rc.ctx.order_rows else None
    return latest is not None and latest.status in ("pending_payment", "payment_submitted")


async def ask_cancel_confirmation(rc, decision: RouterDecision | None):
    """
    "Cancel this order? (Yes / No)" — the router *thought* the customer wants to cancel but they did not say so in
    explicit words (or the model was not sure). Nothing changes: stage and slots stay, `cancel_confirm_at` opens the
    question and the next Yes/No is read by `order_recovery.run_recovery_guard`.
    """
    lang = template_language(decision, rc.user_text, rc.conv)
    await _clear_menu(rc.db, rc.conv)
    await order_recovery.open_cancel_confirm(rc.db, rc.conv)
    text = _t(lang, "rt_cancel_confirm")
    result = op.PipelineResult(
        text=text,
        buttons=[
            op.ButtonSpec(id="cancel_yes", title=_t(lang, "btn_cancel_yes")),
            op.ButtonSpec(id="cancel_no", title=_t(lang, "btn_cancel_no")),
        ] if rc.conv.channel == "whatsapp" else None,
    )
    return await rc.reply(text, "cancel_confirm_ask", lang, result=result)


async def handle_cancel_declined(rc):
    """The customer said No to "Cancel this order?": nothing was cancelled, carry on where the order was."""
    lang = template_language(None, rc.user_text, rc.conv)
    resumed = await resume_open_step(rc, lang, prefix_key="rt_cancel_kept", route="cancel_kept")
    if resumed is not None:
        return resumed
    return await rc.reply(_t(lang, "rt_cancel_kept"), "cancel_kept", lang)


async def render_summary_text(rc, lang: str, product=None, vi: dict | None = None) -> str | None:
    """
    The order summary for a complete draft (and mark it shown), or None when something is still missing.

    Same renderer the slot machine uses, so wording, totals, ETA and payment line are identical everywhere.
    """
    from app.services.order_state_machine import RenderError

    conv = rc.conv
    product = product or rc.pinned_product
    vi = vi if vi is not None else (rc.variant_info or {})
    available_stock = None
    try:
        saved = rc.pinned_product
        rc.pinned_product = product
        ok, available_stock = await _variant_stock(rc, conv.selected_color, conv.selected_size, conv.selected_material)
        rc.pinned_product = saved
    except Exception:  # noqa: BLE001
        available_stock = None
    try:
        body = await op._render_order_reply(
            action="show_summary", conv=conv, db=rc.db, client=rc.client, next_slot=None, variant_info=vi,
            customer_profile=rc.profile, available_stock=available_stock, declined_saved_address=False, lang=lang,
        )
    except RenderError as exc:
        logger.warning("render_summary_text conv=%s: %s", conv.id, exc)
        return None
    await conversation_service.update_order_field(rc.db, conv.id, "summary_shown", True)
    conv.summary_shown = True
    return body


async def resume_open_step(rc, lang: str, *, prefix_key: str | None = None, prefix_text: str | None = None, route: str = "resume_step"):
    """
    Re-send the question the customer was last asked, with its buttons, optionally after a one-line prefix.

    * an order slot is open  -> that slot question (`_reask`);
    * the order summary is waiting for Confirm -> the summary again;
    * a product card is on offer ("Would you like to order X?") -> that question;
    * an open "which one?" menu -> the menu again.
    Returns None when there is nothing to resume (the caller then sends its own text). Order state is never changed.
    """
    conv, db, client = rc.conv, rc.db, rc.client
    pinned, vi = rc.pinned_product, rc.variant_info or {}
    prefix = prefix_text or (_t(lang, prefix_key) if prefix_key else "")
    if pinned is not None and rc.stage in intent_router.ORDER_SLOT_STAGES:
        if rc.next_slot:
            return await _reask(rc, lang, None, route, prefix_text=prefix or None)
        if rc.stage == "awaiting_final_confirmation":
            body = await render_summary_text(rc, lang)
            if body:
                text = f"{prefix}\n\n{body}" if prefix else body
                result = await _send_with_buttons(db, conv, client, "awaiting_final_confirmation", text, None, pinned, vi)
                return await rc.reply(text, route, lang, result=result)
    if getattr(conv, "pending_choice_skus", None):
        try:
            skus = json.loads(conv.pending_choice_skus)
        except (TypeError, ValueError):
            skus = []
        products = [p for p in [await catalogue_service.find_product_by_sku(db, client.id, k) for k in skus] if p]
        if products:
            lines = ([prefix, ""] if prefix else []) + [_t(lang, "rt_pick_header")] + [f"{i}. {_product_line(p)}" for i, p in enumerate(products, 1)]
            text = "\n".join(lines)
            result = await _send_with_buttons(db, conv, client, rc.stage, text, None, None, {})
            return await rc.reply(text, route, lang, result=result)
    if pinned is not None and rc.stage not in intent_router.ORDER_ACTIVE_STAGES and conv.pending_product_sku:
        ask = _t(lang, "would_you_like_to_order_named", name=pinned.name, sku=pinned.sku)
        text = f"{prefix}\n\n{ask}" if prefix else ask
        result = await _send_with_buttons(db, conv, client, rc.stage, text, None, pinned, vi)
        return await rc.reply(text, route, lang, result=result)
    return None


_SPLIT_SLOTS = frozenset({
    "variant_mode", "cart_item_qty", "cart_item_color", "cart_item_size", "cart_item_material", "cart_breakdown",
    "cart_breakdown_confirm",
})


async def _abort_variant_split(rc) -> bool:
    """
    Drop a "different colours/sizes for each piece" split of the PINNED product and go back to the single-variant
    path (`cart_variant_mode = "same"`). Items of other products in the cart (an "Add X too" order) are kept untouched.
    Returns True when a split was actually aborted.
    """
    conv = rc.conv
    pinned_sku = (conv.pending_product_sku or "").upper()
    items = list(conv.cart_items or [])
    foreign = [i for i in items if (i.get("sku") or "").upper() != pinned_sku]
    splitting = (
        conv.cart_variant_mode == "different" or rc.next_slot in _SPLIT_SLOTS
        or any((i.get("sku") or "").upper() == pinned_sku for i in items)
    )
    if not splitting or rc.pinned_product is None or rc.stage not in intent_router.ORDER_SLOT_STAGES:
        return False
    for name, value in (
        ("cart_items", foreign or None), ("cart_collection_mode", None), ("cart_wip_item", None),
        ("cart_pending_confirmation", None), ("cart_variant_mode", "same"), ("summary_shown", False),
    ):
        await conversation_service.update_order_field(rc.db, conv.id, name, value)
        setattr(conv, name, value)
    rc.next_slot = conversation_flow.get_next_required_slot(conv, rc.variant_info or {})
    stage = "order_collection" if rc.next_slot else "awaiting_final_confirmation"
    if stage != (conv.current_stage or ""):
        await conversation_service.update_stage(rc.db, conv.id, stage)
        conv.current_stage = stage
    rc.stage = stage
    return True


async def handle_confused(rc, decision: RouterDecision | None):
    """
    confused: the customer does not follow ("I can't get it", "what are you trying to say", "samjh nahi aaya", "?").

    Nothing is cancelled and no answer is guessed. The last question goes out again in its simplest form with its
    buttons; a "different colour for each piece" split is abandoned in favour of the single-variant path.
    """
    lang = template_language(decision, rc.user_text, rc.conv)
    if order_recovery.cancel_confirm_open(rc.conv):
        await order_recovery.close_cancel_confirm(rc.db, rc.conv)
    aborted = await _abort_variant_split(rc)
    if rc.stage == "payment":
        return await rc.reply(_t(lang, "rt_confused_payment"), "confused_payment", lang)
    resumed = await resume_open_step(
        rc, lang, prefix_key="rt_confused_split" if aborted else "rt_confused_prefix", route="confused",
    )
    if resumed is not None:
        return resumed
    business = getattr(rc.client, "business_name", None) or "our shop"
    return await rc.reply(
        _t(lang, "rt_confused_idle", business=business, catalogue_url=_catalogue_url(rc.client)), "confused_idle", lang,
    )


async def handle_restore_draft(rc):
    """
    The customer wants the order they cancelled a moment ago ("I want to continue this", "yes"): put the saved draft
    back and carry on from its next open step ("Restored your order: …"). Expired / sold out -> an honest message.
    """
    lang = template_language(None, rc.user_text, rc.conv)
    conv, db, client = rc.conv, rc.db, rc.client
    snap = order_recovery.restorable_draft(conv)
    sku = (snap or {}).get("fields", {}).get("pending_product_sku")
    product = await catalogue_service.find_product_by_sku(db, client.id, sku) if sku else None
    if product is None or product.is_active is False or await op._is_product_out_of_stock(db, product):
        await order_recovery.clear_cancelled_draft(db, conv)
        return await rc.reply(_t(lang, "rt_restore_unavailable", catalogue_url=_catalogue_url(client)), "restore_unavailable", lang)
    line = order_recovery.summary_line(snap, product.name)
    fields = await order_recovery.restore_draft_fields(db, conv)
    if fields is None:
        return await rc.reply(_t(lang, "rt_restore_expired", catalogue_url=_catalogue_url(client)), "restore_expired", lang)
    vi = await catalogue_service.get_product_variant_info(db, product)
    if rc.profile is None:
        try:
            rc.profile = await customer_service.get_customer(db, client_id=client.id, phone=rc.sender_phone)
        except Exception:  # noqa: BLE001
            rc.profile = None
    rc.pinned_product, rc.variant_info = product, vi
    if conv.channel == "whatsapp" and not getattr(conv, "mobile_number", None):
        conv.mobile_number = rc.sender_phone
    rc.next_slot = conversation_flow.get_next_required_slot(conv, vi)
    stage = "order_collection" if rc.next_slot else "awaiting_final_confirmation"
    await conversation_service.update_stage(db, conv.id, stage)
    conv.current_stage = rc.stage = stage
    prefix = _t(lang, "rt_restored", summary=line)
    logger.info("DRAFT restored conv=%s sku=%s next_slot=%s", conv.id, product.sku, rc.next_slot)
    resumed = await resume_open_step(rc, lang, prefix_text=prefix, route="restore_draft")
    if resumed is not None:
        return resumed
    return await rc.reply(prefix, "restore_draft", lang)


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


def is_pure_ack(text: str | None) -> bool:
    """A reply that carries no information and asks nothing: fewer than 6 words, no question mark, no number, no link."""
    t = " ".join((text or "").split())
    if not t:
        return True
    return len(t.split()) < 6 and "?" not in t and "؟" not in t and not any(ch.isdigit() for ch in t) and "http" not in t.lower()


def _is_social_message(text: str) -> bool:
    """Thanks / goodbye / a compliment: a short friendly reply is the right answer to these."""
    from app.services import side_questions

    return cw_matches_social(text) or side_questions.detect_topic(text) in ("thanks", "bye", "positive")


def cw_matches_social(text: str) -> bool:
    """command_words thanks/bye (kept separate so tests can reach it without side_questions)."""
    from app.services import command_words as cw

    return cw.matches("thanks", text) or cw.matches("bye", text)


async def _last_bot_asked_question(rc) -> bool:
    """True when the shop's last message was a question the customer has not answered (so small talk must not bury it)."""
    try:
        rows = await conversation_service.get_history(rc.db, rc.conv.id, limit=4)
    except Exception:  # noqa: BLE001
        return False
    last = next((m for m in reversed(rows) if m.role in ("assistant", "model") and (m.content or "").strip()), None)
    return bool(last is not None and ("?" in last.content or "؟" in last.content))


async def handle_free_text(rc, decision: RouterDecision):
    """
    general_answer / smalltalk: the model's one-liner after the guard; otherwise a fixed friendly template.

    Small talk never buries an open question: when a cancelled draft can be restored, a product is pinned or the shop's
    last message was a question, the customer gets that step back (or their order restored on "continue"), not a bare
    "Sure, let's continue!". Replies that are pure acknowledgements are not sent for small talk at all.
    """
    lang = template_language(decision, rc.user_text, rc.conv)
    conv = rc.conv
    social = _is_social_message(rc.user_text)
    if decision.action == RouterAction.SMALLTALK and not social:
        draft = order_recovery.restorable_draft(conv) is not None and not conv.pending_product_sku
        if draft and rc.stage not in intent_router.ORDER_ACTIVE_STAGES and order_recovery.is_resume_request(rc.user_text):
            return await handle_restore_draft(rc)
        if draft or conv.pending_product_sku or getattr(conv, "pending_choice_skus", None) or await _last_bot_asked_question(rc):
            resumed = await resume_open_step(rc, lang, route="smalltalk_resume")
            if resumed is not None:
                return resumed
            if draft:
                return await rc.reply(_t(lang, "rt_restore_offer"), "smalltalk_restore_offer", lang)
    await _clear_menu(rc.db, conv)
    safe = guard_reply_hint(decision.reply_hint, rc.ctx.candidate_products)
    # The hint must be in the customer's own language and script; the claim guard above only knows
    # English / Hindi / Gujarati / Marathi words, so any other language gets the (localised) template.
    if safe and not (
        _lang_svc.reply_matches_language(safe, lang) and _lang_svc.has_claim_guard(lang)
    ):
        logger.info("router: reply_hint dropped (language/script mismatch, lang=%s conv=%s)", lang, conv.id)
        safe = None
    ack_dropped = False
    if safe and decision.action == RouterAction.SMALLTALK and not social and is_pure_ack(safe):
        logger.info("router: pure-ack reply_hint dropped (conv=%s)", conv.id)
        safe, ack_dropped = None, True
    if safe:
        return await rc.reply(safe, f"{decision.action.value}_hint", lang, path="LLM", model=get_settings().llm_model_classifier)
    if decision.reply_hint:
        logger.info("router: reply_hint rejected by guard (conv=%s)", conv.id)
    if ack_dropped:
        # "Sure, let's continue!" tells the customer nothing: send the menu of what we can do instead.
        return await rc.reply(
            _t(lang, "rt_menu_fallback", catalogue_url=_catalogue_url(rc.client)), "smalltalk_menu", lang,
        )
    return await rc.reply(_t(lang, "rt_general_fallback"), f"{decision.action.value}_fallback", lang)


async def _reset_off_topic(db, conv) -> None:
    """
    The customer is back on topic: clear the consecutive irrelevant counter (one write, only when it was set).

    A running mute is left alone — an exact SKU or a menu number must not lift it (it ends by time, or when staff resume).
    """
    if spam_guard.is_muted(conv):
        return
    await spam_guard.clear(db, conv)


async def handle_out_of_scope(rc, decision: RouterDecision):
    """
    out_of_scope: the message has nothing to do with this business → a FIXED template, never model text.

    Counts toward the shared consecutive-irrelevant counter (`spam_guard`). At the limit the reply is the ONE final
    notice and the conversation is muted for a while (no router call, no LLM, no reply — see spam_guard); during an
    order the reply instead offers a human (nothing is ever muted mid-order). Mid-order the open question is re-asked
    after a one-line prefix, and no order state is touched.
    """
    conv, client = rc.conv, rc.client
    lang = template_language(decision, rc.user_text, conv)
    await _clear_menu(rc.db, conv)
    order_active = rc.stage in spam_guard.ORDER_STAGES
    strike = await spam_guard.register_strike(rc.db, conv, client, may_mute=not order_active)
    business = getattr(client, "business_name", None) or "our shop"
    url = _catalogue_url(client)
    if strike.reached:
        logger.warning(
            "OFF_TOPIC streak (router): conv=%s count=%d muted=%s — final notice.", conv.id, strike.count, strike.muted,
        )
    if rc.next_slot and rc.stage in intent_router.ORDER_SLOT_STAGES:
        key = "rt_out_of_scope_repeat" if strike.reached else "rt_out_of_scope_midorder"
        return await _reask(rc, lang, None, "out_of_scope", prefix_text=_t(lang, key, business=business))
    if strike.muted:
        text = _t(lang, "rt_spam_final", business=business, catalogue_url=url, minutes=spam_guard.mute_minutes())
    elif strike.reached:
        text = f"{_t(lang, 'rt_out_of_scope_repeat', business=business)}\n{_t(lang, 'rt_browse_more', catalogue_url=url)}"
    else:
        text = _t(lang, "rt_out_of_scope", business=business, catalogue_url=url)
    return await rc.reply(text, "out_of_scope", lang)


async def handle_clarify(rc, decision: RouterDecision | None):
    """Low confidence: 'Did you mean A or B?' (top 2 catalogue candidates) or 'what are you looking for?' — never echoes the text."""
    lang = template_language(decision, rc.user_text, rc.conv)
    if rc.next_slot and rc.stage in intent_router.ORDER_SLOT_STAGES:
        # Mid-order and the customer's reply made no sense: say so and ask the SAME question again —
        # "what are you looking for?" would read as if the order had been forgotten.
        return await _reask(rc, lang, "rt_clarify_slot", "clarify_slot")
    # Only guess when the customer's words really name the product ("i cant got it" must not suggest "Gota Patti ...").
    cands = [
        p for p in rc.ctx.candidate_products
        if intent_router.product_in_stock(p)
        and catalogue_service.name_match_score(p, rc.user_text) >= _CLARIFY_MIN_MATCH_SCORE
    ][:2]
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
    RouterAction.OUT_OF_SCOPE: handle_out_of_scope,
    RouterAction.CONFUSED: handle_confused,
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
    if not raw.get("want_photo"):
        raw.pop("want_photo", None)
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
        await _reset_off_topic(db, conv)     # button taps / exact SKUs / slot answers are on-topic by definition
        return _done(outcome)

    # A short, not-quite-exact answer to an open colour/size question ("pinkk", "gulabi", "medium", or a
    # colour the product doesn't come in, like "Gray"): settled here for ₹0 — the LLM would call it small talk.
    attempt = (
        intent_router.resolve_option_attempt(
            user_text, next_slot, (variant_info or {}).get(intent_router._OPTION_SLOT_KEYS.get(next_slot or "", ""), []),
        )
        if getattr(message, "type", "text") == "text" and pinned is not None and stored_stage in intent_router.ORDER_SLOT_STAGES
        else None
    )
    if attempt is not None:
        if attempt.status == "match":
            trace.fast_path = "slot_option_fuzzy"
            outcome.user_text = attempt.value      # the legacy slot machine takes the catalogue's spelling
            return _done(outcome)
        trace.fast_path = "slot_option_unavailable"
        rc = RouterCtx(
            db=db, conv=conv, client=client, message=message, user_text=user_text, sender_phone=sender_phone,
            wamid=wamid, stage=stored_stage, language=language, record_usage=record_usage, ctx=None,
            all_products=all_products, pinned_product=pinned, variant_info=variant_info, next_slot=next_slot,
            profile=None,
        )
        lang = (getattr(conv, "last_customer_language", None) or language or "english").lower()
        if template_language(None, user_text, conv) in ("hindi_devanagari", "gujarati_script"):
            lang = template_language(None, user_text, conv)
        sorry = _t(lang, "rt_variant_no", option=attempt.value, product=pinned.name, options_line="")
        # Count consecutive invalid answers to THIS question (same counter the slot machine uses; it resets
        # when the slot changes or an answer is accepted). The second miss adds a way out: catalogue / human.
        try:
            if getattr(conv, "slot_attempt_slot", None) != next_slot:
                conv.slot_attempt_count, conv.slot_attempt_slot = 0, next_slot
                await conversation_service.update_order_field(db, conv.id, "slot_attempt_slot", next_slot)
            conv.slot_attempt_count = (conv.slot_attempt_count or 0) + 1
            await conversation_service.update_order_field(db, conv.id, "slot_attempt_count", conv.slot_attempt_count)
        except Exception as exc:  # noqa: BLE001 — the counter is a nicety, never lose the reply
            logger.error("router invalid-option counter error conv=%s: %s", conv.id, exc)
        if (conv.slot_attempt_count or 0) >= _INVALID_OPTION_ESCAPE_AT:
            sorry = f"{sorry}\n{_t(lang, 'slot_invalid_escape', catalogue_url=_catalogue_url(client))}"
        outcome = await _reask(rc, lang, None, "slot_option_unavailable", prefix_text=sorry)
        outcome.trace = trace
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
    shop_profile = await shop_context_service.ensure_fresh(db, client, all_products)
    ctx = await intent_router.build_context(
        db, conv, client, user_text, sender_phone=sender_phone, stage=stored_stage, pinned_product=pinned,
        variant_info=variant_info, all_products=all_products, profile=profile, history_rows=history,
        next_slot=next_slot, shop_profile=shop_profile,
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
    if (
        pinned is not None and wants_photo(decision, user_text)
        and decision.action in (RouterAction.SMALLTALK, RouterAction.GENERAL_ANSWER, RouterAction.FAQ, RouterAction.GREETING)
    ):
        # "images" / "photo dikhao" with a product in context is never small talk: show that product's photo.
        decision.action = RouterAction.SHOW_PRODUCT
        decision.args.sku, decision.args.want_photo = pinned.sku, True
        decision.confidence = max(decision.confidence, 0.9)
        notes.append("photo_request->show_product")
    if (
        decision.action in _CONFUSION_OVERRIDES and order_recovery.is_confused(user_text)
    ) or (decision.action == RouterAction.CONFUSED):
        # "sorry i cant get it!" is never a cancel, and "what are you trying to say" is never small talk: the model's
        # reading is overruled by the deterministic confusion lexicon (and a CONFUSED verdict is trusted as such).
        if decision.action != RouterAction.CONFUSED:
            notes.append(f"{decision.action.value}->confused")
        decision.action = RouterAction.CONFUSED
        decision.confidence = max(decision.confidence, 0.9)
        decision.args.sku = None
    if (
        decision.action == RouterAction.SHOW_PRODUCT and not decision.args.filters.size and not decision.args.filters.color
        and is_more_request(user_text) and stored_stage not in intent_router.ORDER_ACTIVE_STAGES
    ):
        # "show me some more products like that" is a search for OTHER items, never the product card already shown.
        decision.action = RouterAction.SEARCH_CATALOG
        decision.args.sku = None
        notes.append("more_request->search_catalog")
    if decision.action == RouterAction.OUT_OF_SCOPE:
        # Never refuse a customer on a guess: a shaky call, or a message that shares a word with this shop's own
        # vocabulary (product / category / KB topic), becomes a clarifying question instead of the boundary reply.
        if decision.confidence < settings.router_out_of_scope_threshold:
            decision.confidence = min(decision.confidence, 0.3)
            notes.append("out_of_scope_low_conf->clarify")
        elif shop_context_service.matches(shop_profile, user_text):
            decision.confidence = min(decision.confidence, 0.3)
            notes.append("out_of_scope_shop_word->clarify")
        elif next_slot in intent_router.FREE_TEXT_SLOTS and decision.confidence < _OUT_OF_SCOPE_FREE_TEXT_MIN:
            # A name / address / phone answer is open: only a very sure verdict may override "it is the answer"
            # (low confidence there already means slot passthrough).
            decision.confidence = min(decision.confidence, 0.3)
            notes.append("out_of_scope_at_free_text_slot->passthrough")
    elif decision.action != RouterAction.SMALLTALK and decision.confidence >= settings.router_confidence_threshold:
        await _reset_off_topic(db, conv)     # a real on-topic request ends the off-topic streak (thanks/ok don't)
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
        if (
            decision.action == RouterAction.CANCEL_ORDER and decision.confidence >= settings.router_confidence_threshold
            and has_cancellable_order(rc)
            and not (
                order_recovery.has_explicit_cancel_keyword(user_text)
                and decision.confidence >= settings.router_cancel_min_confidence
            )
        ):
            # A cancel is destructive: without explicit cancel wording AND a very sure model, ask first.
            trace.action = "cancel_order->confirm"
            handled = await ask_cancel_confirmation(rc, decision)
        elif decision.confidence < settings.router_confidence_threshold:
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
