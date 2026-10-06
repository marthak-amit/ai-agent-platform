"""
LLM intent router (ROUTER_V2) — the front door for messages that don't answer the open slot.

Principle: the LLM *understands* and picks an action; the engine *executes* it with database
data (app/services/router_actions.py). The model never states prices, stock, totals, order
status or dates — it returns one small JSON decision (app/schemas/router.py) and nothing else.

This module holds the pieces that don't touch the pipeline:
  * router_enabled()        — per-client flag (Client.router_v2_enabled, else ROUTER_V2_CLIENT_IDS)
  * fast_path_reason()      — the ₹0 cases that must never reach the LLM
  * build_context()         — compact, PII-free prompt context (~1.5–3k tokens)
  * call_router()           — ONE JSON-mode call on LLM_MODEL_CLASSIFIER (purpose="router" in llm_usage)
  * sanitize_decision()     — drops SKUs the model wasn't given, repairs impossible slot actions

The prompt is app/prompts/router_v2.md; tests/router_eval/run_eval.py loads the same file so
offline accuracy equals production behaviour.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import desc, or_, select

from app.config import get_settings
from app.schemas.router import RouterAction, RouterDecision
from app.services import catalogue_service, conversation_flow, llm_client, llm_health
from app.services.delivery_service import order_delivery_days, order_eta_window, to_ist_date

logger = logging.getLogger("app.services.intent_router")

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "router_v2.md"
_VERSION_RE = re.compile(r"<!--\s*version:\s*([\w.\-]+)\s*-->")

#: Stages where the shop has asked a slot question (the router gets NEXT SLOT only here).
ORDER_SLOT_STAGES = frozenset({"order_collection", "awaiting_final_confirmation"})
#: Every stage in which an order is being built or paid for.
ORDER_ACTIVE_STAGES = frozenset({"order_collection", "awaiting_final_confirmation", "payment", "awaiting_switch_confirm"})

#: Slots whose answer is free text — the router (not a fast path) decides whether a message is
#: the answer or something else ("I want to talk to someone" must not become the customer's name).
FREE_TEXT_SLOTS = frozenset({"customer_name", "delivery_address", "mobile_number", "cart_breakdown"})
_OPTION_SLOT_KEYS = {
    "color": "available_colors", "cart_item_color": "available_colors",
    "size": "available_sizes", "cart_item_size": "available_sizes",
    "material": "available_materials", "cart_item_material": "available_materials",
}
_QUANTITY_SLOTS = frozenset({"quantity", "cart_item_qty"})
_PAYMENT_WORDS = frozenset({"cod", "upi", "cash", "cash on delivery", "online", "bank", "bank transfer", "bank_transfer"})
# Bare closed-set replies the legacy gates (confirm / address-confirm / same-or-different) own.
_GATE_WORDS = (
    conversation_flow._CONFIRMATION_YES | conversation_flow._CONFIRMATION_NO
    | frozenset({"y", "n", "same", "different", "yes please", "no thanks", "haan ji", "ji haan", "nahi ji", "paid"})
)
_CATALOGUE_ORDER_RE = re.compile(r"SKU:\s*\S+", re.IGNORECASE)

# Redaction for text that goes into the prompt (history): phones, emails/UPI ids, links, pincodes.
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s-]?)?(?:\d[\s-]?){9,13}\d(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_PIN_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")


# ── flag ─────────────────────────────────────────────────────────────────────

def router_enabled(client) -> bool:
    """
    True when ROUTER_V2 is on for this client.

    Client.router_v2_enabled (True/False) wins; NULL falls back to the ROUTER_V2_CLIENT_IDS env
    list ("1" by default; "*" = every client; empty = none).
    """
    if client is None:
        return False
    override = getattr(client, "router_v2_enabled", None)
    if isinstance(override, bool):
        return override
    raw = (get_settings().router_v2_client_ids or "").strip()
    if raw == "*":
        return True
    ids = {int(tok) for tok in re.split(r"[,\s]+", raw) if tok.isdigit()}
    return getattr(client, "id", None) in ids


# ── prompt ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PromptParts:
    """The two halves of router_v2.md plus its version tag."""

    system: str
    user_template: str
    version: str


@lru_cache(maxsize=4)
def load_prompt(path: str = str(PROMPT_PATH)) -> PromptParts:
    """Split router_v2.md into SYSTEM / USER_TEMPLATE (cached; HTML comments are stripped)."""
    text = Path(path).read_text(encoding="utf-8")
    version_match = _VERSION_RE.search(text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    _, _, rest = text.partition("## SYSTEM")
    system, _, user = rest.partition("## USER_TEMPLATE")
    return PromptParts(system.strip(), user.strip(), version_match.group(1) if version_match else "unversioned")


def render_user_prompt(
    template: str, *, shop: str, state: str, orders: str, candidates: str, history: str, message: str,
) -> str:
    """Fill the user template's {{placeholders}} (plain replace — braces in the data are safe)."""
    return (
        template.replace("{{shop}}", shop).replace("{{state}}", state).replace("{{orders}}", orders)
        .replace("{{candidates}}", candidates).replace("{{history}}", history).replace("{{message}}", message)
    )


# ── small text helpers ───────────────────────────────────────────────────────

def redact(text: str, limit: int = 160) -> str:
    """Mask phones/emails/links/pincodes and cap the length — used on anything sent to the LLM or logs."""
    text = _URL_RE.sub("<link>", text or "")
    text = _EMAIL_RE.sub("<email>", text)
    text = _PHONE_RE.sub("<phone>", text)
    text = _PIN_RE.sub("<pin>", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def normalize_option(text: str) -> str:
    """Lower-case, strip punctuation/extra spaces: the form option comparisons use."""
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


def _active_variants(product) -> list:
    """Active variant rows of a product (empty when it has none)."""
    return [v for v in (getattr(product, "variants", None) or []) if getattr(v, "is_active", True) is not False]


def in_stock_options(product) -> dict[str, list[str]]:
    """Colours/sizes/materials that have at least one piece in stock (from the product's variant rows)."""
    colors, sizes, materials = [], [], []
    for v in _active_variants(product):
        if (v.stock or 0) <= 0:
            continue
        for bucket, value in ((colors, v.color), (sizes, v.size), (materials, getattr(v, "material", None))):
            if value and value not in bucket:
                bucket.append(value)
    return {"colors": colors, "sizes": sizes, "materials": materials}


def product_in_stock(product) -> bool:
    """True when the product (or any of its variants) has stock; untracked stock counts as in stock."""
    if getattr(product, "is_active", True) is False:
        return False
    if getattr(product, "has_variants", False) and _active_variants(product):
        return any((v.stock or 0) > 0 for v in _active_variants(product))
    stock = getattr(product, "stock", None)
    return stock is None or stock > 0


def format_candidate(product) -> str:
    """One compact prompt line for a catalogue product: name | sku | price | category | in-stock options."""
    opts = in_stock_options(product)
    line = (
        f"- {product.name} | sku={product.sku or 'n/a'} | ₹{int(product.price or 0):,} | "
        f"{product.category or 'uncategorised'}"
    )
    if opts["colors"]:
        line += f" | colors in stock: {', '.join(opts['colors'])}"
    if opts["sizes"]:
        line += f" | sizes in stock: {', '.join(opts['sizes'])}"
    if not product_in_stock(product):
        line += " | OUT OF STOCK"
    return line


def pick_candidates(all_products: list, user_text: str, pinned_product=None, k: int = 5) -> list:
    """
    Top-k catalogue candidates for a message (the existing fuzzy search), SKUs typed in the
    message first, the pinned product kept in reach. Only these may appear as `sku` in the decision.
    """
    by_sku = {(p.sku or "").upper(): p for p in all_products if p.sku}
    picked: list = []

    def _add(product) -> None:
        if product is not None and all(product is not q for q in picked):
            picked.append(product)

    for sku in catalogue_service.extract_skus_from_text(user_text or ""):
        _add(by_sku.get(sku.upper()))
    for _, product in catalogue_service.search_products_with_scores(all_products, user_text or "", top_k=k):
        _add(product)
    picked = picked[:k]
    if pinned_product is not None and all(pinned_product is not q for q in picked):
        picked = picked[: max(k - 1, 0)] + [pinned_product]
    return picked


# ── orders ───────────────────────────────────────────────────────────────────

async def fetch_recent_orders(db, client, conv, sender_phone: str, limit: int = 2) -> list:
    """The customer's newest orders in this shop (by phone or conversation) — only ever their own."""
    from app.models.order import Order

    stmt = (
        select(Order)
        .where(
            Order.client_id == client.id,
            or_(Order.customer_phone == sender_phone, Order.conversation_id == conv.id),
        )
        .order_by(desc(Order.created_at), desc(Order.id))
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def order_product_days(db, order) -> list[int | None]:
    """Per-line Product.delivery_days for an order (None where unset), for the ETA window."""
    from app.models.product import Product

    ids = [li.product_id for li in (order.line_items or []) if li.product_id] or (
        [order.product_id] if order.product_id else []
    )
    if not ids:
        return []
    rows = (await db.execute(select(Product.id, Product.delivery_days).where(Product.id.in_(ids)))).all()
    days = {pid: d for pid, d in rows}
    return [days.get(pid) for pid in ids]


def _order_items_short(order) -> str:
    """'2× Cotton Lehenga (Pink, M); 1× Silk Stole' from the order's line items."""
    rows = list(order.line_items or []) or [order]
    parts = []
    for row in rows:
        variant = ", ".join(v for v in (row.variant_color, row.variant_size, row.variant_material) if v)
        parts.append(f"{row.quantity or 1}× {row.product_name}" + (f" ({variant})" if variant else ""))
    return "; ".join(parts)


async def format_orders(db, client, orders: list) -> str:
    """Prompt block for the latest orders: id, status, items, total, dates, engine-computed ETA window."""
    if not orders:
        return "(none)"
    lines = []
    for order in orders:
        eta = order_eta_window(order, client, await order_product_days(db, order))
        if eta["state"] == "window":
            eta_txt = f"{eta['from']:%d %b}–{eta['to']:%d %b}"
        else:
            eta_txt = {"none": "n/a", "awaiting": "starts after payment is confirmed", "overdue": "window passed"}[eta["state"]]
        placed, paid = to_ist_date(order.created_at), to_ist_date(order.paid_at)
        lines.append(
            f"- {order.order_number} | status: {order.status} | items: {_order_items_short(order)} | "
            f"total ₹{int(order.total_amount or 0):,} | payment: {order.payment_method or 'n/a'} | "
            f"placed {placed:%d %b}" + (f" | paid {paid:%d %b}" if paid else "") + f" | ETA: {eta_txt}"
        )
    return "\n".join(lines)


# ── context ──────────────────────────────────────────────────────────────────

@dataclass
class RouterContext:
    """Everything the router prompt (and the sanitiser) needs for one turn."""

    shop: str
    state: str
    orders: str
    candidates: str
    history: str
    stage: str = "greeting"
    next_slot: str | None = None
    pinned_sku: str | None = None
    candidate_products: list = field(default_factory=list)
    allowed_skus: set = field(default_factory=set)
    order_rows: list = field(default_factory=list)


def _state_block(conv, stage: str, pinned_product, variant_info: dict, next_slot: str | None) -> str:
    """CONVERSATION STATE lines. Customer name/address/phone are reported as 'set', never their values."""
    lines = [f"stage: {stage}"]
    if pinned_product is not None:
        opts = in_stock_options(pinned_product)
        line = (
            f"pinned_product: {pinned_product.name} | sku={pinned_product.sku} | "
            f"₹{int(pinned_product.price or 0):,}"
        )
        if opts["colors"]:
            line += f" | colors in stock: {', '.join(opts['colors'])}"
        if opts["sizes"]:
            line += f" | sizes in stock: {', '.join(opts['sizes'])}"
        lines.append(line)
    else:
        lines.append("pinned_product: none")
    filled = []
    for label, attr in (
        ("color", "selected_color"), ("size", "selected_size"), ("material", "selected_material"),
        ("quantity", "pending_order_quantity"), ("payment_method", "payment_method"),
    ):
        value = getattr(conv, attr, None)
        if value:
            filled.append(f"{label}={value}")
    for label, attr in (("name", "customer_name"), ("address", "delivery_address"), ("phone", "mobile_number")):
        if getattr(conv, attr, None):
            filled.append(f"{label}=set")
    lines.append(f"filled_slots: {', '.join(filled) if filled else 'none'}")
    if next_slot:
        line = f"next_slot: {next_slot}"
        key = _OPTION_SLOT_KEYS.get(next_slot)
        if key and variant_info.get(key):
            line += f" | allowed options: {', '.join(variant_info[key])}"
        elif next_slot == "payment_method":
            line += " | allowed options: COD, UPI"
        lines.append(line)
    else:
        lines.append("next_slot: none (no question is open)")
    if getattr(conv, "summary_shown", False):
        lines.append("order_summary_shown: yes (waiting for the customer to confirm)")
    menu = getattr(conv, "pending_choice_skus", None)
    if menu:
        lines.append(f"open_choice_menu_skus: {menu}")
    return "\n".join(lines)


def _shop_block(client, conv, profile) -> str:
    """SHOP line: name, language preference, delivery days, payment modes (flags only — no ids/accounts)."""
    lo, hi = order_delivery_days(client)
    language = (
        getattr(conv, "last_customer_language", None) or getattr(profile, "preferred_language", None) or "english"
    )
    modes = []
    if getattr(client, "accepts_cod", False):
        modes.append("COD")
    if getattr(client, "accepts_upi", True) and getattr(client, "upi_id", None):
        modes.append("UPI")
    return (
        f"name: {getattr(client, 'business_name', None) or 'the shop'} | customer language: {language} | "
        f"delivery: {lo}–{hi} business days | payment: {', '.join(modes) or 'not configured'}"
    )


def _history_block(history_rows: list, limit: int = 8) -> str:
    """Last `limit` messages, oldest first, redacted ('customer:' / 'shop:')."""
    rows = [m for m in history_rows if (getattr(m, "content", None) or "").strip()][-limit:]
    if not rows:
        return "(no earlier messages)"
    return "\n".join(
        f"{'customer' if m.role == 'user' else 'shop'}: {redact(m.content)}" for m in rows
    )


async def build_context(
    db, conv, client, user_text: str, *, sender_phone: str, stage: str, pinned_product, variant_info: dict,
    all_products: list, profile, history_rows: list, next_slot: str | None,
) -> RouterContext:
    """Assemble the compact router context for this turn (state, orders, candidates, history)."""
    candidates = pick_candidates(all_products, user_text, pinned_product)
    orders = await fetch_recent_orders(db, client, conv, sender_phone)
    allowed = {(p.sku or "").upper() for p in candidates if p.sku}
    if pinned_product is not None and pinned_product.sku:
        allowed.add(pinned_product.sku.upper())
    return RouterContext(
        shop=_shop_block(client, conv, profile),
        state=_state_block(conv, stage, pinned_product, variant_info or {}, next_slot),
        orders=await format_orders(db, client, orders),
        candidates="\n".join(format_candidate(p) for p in candidates) or "(no close catalogue matches for this message)",
        history=_history_block(history_rows),
        stage=stage, next_slot=next_slot,
        pinned_sku=getattr(pinned_product, "sku", None),
        candidate_products=candidates, allowed_skus=allowed, order_rows=orders,
    )


# ── fast paths (₹0, before the router) ───────────────────────────────────────

async def fast_path_reason(
    conv, message, user_text: str, stage: str, *, pinned_product, variant_info: dict, all_products: list,
    next_slot: str | None,
) -> str | None:
    """
    Name of the ₹0 fast path this message belongs to, or None when it must go to the router.

    Only these are fast paths: a button/list tap or non-text message; a catalogue "Order" paste;
    the exact SKU / exact product name; a number/option picked from an open "which one?" menu;
    a bare yes/no while a prompt is open; and an exact option the OPEN slot question expects (colour/size/material/quantity/payment
    word/yes-no/same-different/phone number). "Image while pending_payment" is handled earlier by
    payment_inbound.pre_process. Free-text slots (name, address) are NOT fast paths.
    """
    from app.services import order_pipeline as op

    mtype = getattr(message, "type", "text")
    if mtype == "interactive":
        return "button"
    if mtype not in ("text", "audio") or not (user_text or "").strip():
        return "non_text"
    text = user_text.strip()
    if _CATALOGUE_ORDER_RE.search(text):
        return "catalogue_order"
    for product in all_products:
        if op._is_exact_sku_message(text, product):
            return "exact_sku"
    if op._find_exact_name_match(all_products, text) is not None:
        return "exact_name"

    norm = normalize_option(text)
    is_int = bool(re.fullmatch(r"\d{1,4}", norm))
    menu_raw = getattr(conv, "pending_choice_skus", None)
    if menu_raw and is_int:
        return "menu_pick"
    if is_int and stage in ORDER_ACTIVE_STAGES:
        return "number_reply"
    # A bare yes/no/same/different answers whatever prompt is open: an order step, the "Would you like
    # to order?" card (a product is pinned), an interrupted-SKU confirm, or a "which one?" menu.
    open_prompt = stage in ORDER_ACTIVE_STAGES or bool(
        getattr(conv, "pending_product_sku", None) or getattr(conv, "interrupted_sku", None) or menu_raw
    )
    if open_prompt and norm in {normalize_option(w) for w in _GATE_WORDS}:
        return "gate_word"

    if stage in ORDER_SLOT_STAGES and next_slot:
        vi = variant_info or {}
        key = _OPTION_SLOT_KEYS.get(next_slot)
        if key and norm in {normalize_option(o) for o in vi.get(key, [])}:
            return "slot_option"
        if next_slot in _QUANTITY_SLOTS and is_int:
            return "slot_quantity"
        if next_slot == "payment_method" and norm in _PAYMENT_WORDS:
            return "slot_payment"
        if next_slot == "mobile_number" and re.fullmatch(r"\+?\d[\d\s-]{8,13}", text):
            return "slot_phone"
    return None


# ── LLM call + validation ────────────────────────────────────────────────────

@dataclass
class RouterCall:
    """Outcome of one router LLM call: a validated decision, or None plus why not."""

    decision: RouterDecision | None
    error: str | None = None


async def call_router(
    ctx: RouterContext, user_text: str, *, client_id: int | None, conversation_id: int | None,
) -> RouterCall:
    """
    ONE JSON-mode call on LLM_MODEL_CLASSIFIER, recorded in llm_usage as purpose="router".

    Output is validated against RouterDecision; invalid output is retried once (chat_json), then
    reported as a failure. Never raises: the caller falls back to the keyword detectors.
    """
    settings = get_settings()
    prompt = load_prompt()
    user = render_user_prompt(
        prompt.user_template, shop=ctx.shop, state=ctx.state, orders=ctx.orders,
        candidates=ctx.candidates, history=ctx.history, message=user_text.strip(),
    )
    holder: dict[str, RouterDecision] = {}

    def _validate(data: dict) -> bool:
        """Accept only dicts that parse into a RouterDecision (keeps the parsed object)."""
        try:
            holder["decision"] = RouterDecision.model_validate(data)
            return True
        except ValidationError as exc:
            logger.warning("router: invalid decision (%s): %s", exc.error_count(), str(data)[:160])
            return False

    try:
        parsed = await llm_client.chat_json(
            settings.llm_model_classifier,
            [{"role": "system", "content": prompt.system}, {"role": "user", "content": user}],
            max_tokens=settings.router_max_tokens, purpose="router",
            client_id=client_id, conversation_id=conversation_id, validate=_validate, retries=1,
        )
    except llm_health.LLMUnavailableError as exc:
        return RouterCall(None, f"llm_unavailable:{exc}")
    except Exception as exc:  # noqa: BLE001 — any provider failure means "use the fallback"
        llm_health.record_failure("router", exc)
        return RouterCall(None, f"llm_error:{type(exc).__name__}")
    if parsed is None or "decision" not in holder:
        llm_health.record_failure("router_invalid_json", "no valid router JSON after retry")
        return RouterCall(None, "invalid_json")
    return RouterCall(holder["decision"])


_SLOT_ALIASES = {
    "colour": "color", "color": "color", "size": "size", "material": "material", "fabric": "material",
    "quantity": "quantity", "qty": "quantity", "name": "name", "customer_name": "name",
    "address": "address", "delivery_address": "address", "phone": "phone", "mobile": "phone",
    "mobile_number": "phone", "payment_method": "payment_method", "payment": "payment_method",
    "confirmation": "confirmation", "confirm": "confirmation", "variant_mode": "variant_mode",
}


def canonical_slot(slot: str | None) -> str | None:
    """Fold a slot spelling into color|size|material|quantity|name|address|phone|payment_method|confirmation|variant_mode."""
    return _SLOT_ALIASES.get((slot or "").strip().lower())


def sanitize_decision(decision: RouterDecision, ctx: RouterContext) -> tuple[RouterDecision, list[str]]:
    """
    Enforce the engine's side of the contract; returns (decision, notes about what was repaired).

    * a `sku` that isn't one of the candidates / the pinned product is dropped; show_product /
      start_order left without a sku become search_catalog (when a query exists) or low-confidence;
    * answer_slot with no open slot becomes change_slot (when an order is in progress);
    * slot actions without a recognised slot or value (except "change my name/address" with no new value yet),
      and unknown slots, become low-confidence
      (the engine then asks a clarifying question instead of guessing).
    """
    notes: list[str] = []
    d = decision.model_copy(deep=True)
    a = d.args
    low = min(d.confidence, 0.3)

    if a.sku and a.sku.upper() not in ctx.allowed_skus:
        notes.append(f"dropped_sku={a.sku}")
        a.sku = None
    elif a.sku:
        a.sku = next(
            (p.sku for p in ctx.candidate_products if (p.sku or "").upper() == a.sku.upper()), None
        ) or ctx.pinned_sku or a.sku

    if d.action in (RouterAction.SHOW_PRODUCT, RouterAction.START_ORDER) and not a.sku:
        if ctx.pinned_sku and d.action == RouterAction.START_ORDER and not notes:
            a.sku = ctx.pinned_sku
        elif a.query or not a.filters.is_empty():
            d.action = RouterAction.SEARCH_CATALOG
            notes.append("sku_missing->search_catalog")
        else:
            d.confidence = low
            notes.append("sku_missing->clarify")

    if d.action in (RouterAction.ANSWER_SLOT, RouterAction.CHANGE_SLOT):
        a.slot = canonical_slot(a.slot)
        if d.action == RouterAction.ANSWER_SLOT and not ctx.next_slot:
            if ctx.stage in ORDER_ACTIVE_STAGES and a.slot:
                d.action = RouterAction.CHANGE_SLOT
                notes.append("answer_slot_without_open_slot->change_slot")
            else:
                d.confidence = low
                notes.append("answer_slot_without_open_slot->clarify")
        # "address badalna hai" (no new value yet) is valid for name/address: the engine clears the slot and re-asks.
        clearable = d.action == RouterAction.CHANGE_SLOT and a.slot in ("name", "address")
        if a.slot is None or (a.value in (None, "") and a.slot != "confirmation" and not clearable):
            d.confidence = low
            notes.append("slot_args_missing->clarify")
    return d, notes
