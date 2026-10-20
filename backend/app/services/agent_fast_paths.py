"""
Agent v3 — ₹0 fast paths: taps, numbers, exact options and gate words become actions for the SAME executor, no LLM.

Order of precedence: button id → exact SKU → the question that is open (``state.last_clarify``) → the summary on screen →
cancel wording → side questions / greeting. Anything else returns ``None`` and the planner decides.

Free-text answers (name, address, phone) are never fast paths: only the planner can tell "my address is …" from
"I want to talk to someone". ``grounded_by`` tells the invariants why a value may be written ("button", "number",
"option", "gate"); ``side_topic`` answers write nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, unquote, urlencode

from app.schemas.agent_actions import (
    Action, AddLine, AnswerQuestion, AskClarify, BrowseCatalog, Cancel, ConfirmOrder, HandoffHuman, LineFields, RemoveLine,
    SearchCatalog, SendImages, SetPayment, ShowProduct, SplitLine, SplitPart, UpdateLine, Variant, QUESTION_TOPICS,
)
from app.services import agent_catalogue_words, agent_multivariant, agent_removal, agent_social, agent_text, side_questions
from app.services import command_words as cw
from app.services import indic_vocab, order_recovery
from app.services.agent_cart import CartData
from app.services.agent_catalog import Catalogue

_SLOT_TO_SIDE = {
    "color": "color", "size": "size", "material": "material", "quantity": "quantity", "name": "customer_name",
    "address": "delivery_address", "mobile": "mobile_number", "payment": "payment_method",
}
_PAYMENT_WORDS = {"cod": "COD", "upi": "UPI"}
_ATTR_SLOTS = ("color", "size", "material")
BARE_GATE_MAX_TOKENS = 3
IMAGE_WORDS = frozenset({
    "image", "images", "photo", "photos", "pic", "pics", "picture", "pictures", "foto", "fotos", "dekhao", "dikhao", "dikhado",
    "dekhado", "batao",
})
_IMAGE_MAX_TOKENS = 4


# ── structured button / row ids ─────────────────────────────────────────────
# A tap is resolved from the id ALONE (line + slot + value), never from "which list was on screen" or from the (truncated) title:
#   v3_opt:<line_id>:<color|size|material>:<value>     the customer picked a value for a line of the cart
#   v3_var:<line_id|->:<sku>:<color=Pink&size=M>       one combination of an in-stock list (new or existing line)
#   v3_rm:<line_id>                                    "remove this one" (which-one question)
# WhatsApp allows 256 characters for a button id (200 for a row) including our ~conv~nonce suffix; longer ids fall back to the
# positional v3_opt_N form.
MAX_STRUCTURED_ID = 150
_OPT_RE = re.compile(r"^v3_opt:([A-Za-z0-9]+):(color|size|material):(.+)$")
_VAR_RE = re.compile(r"^v3_var:([A-Za-z0-9\-]+):([^:]+):(.+)$")
_RM_RE = re.compile(r"^v3_rm:([A-Za-z0-9]+)$")
_QTY_TAKE_RE = re.compile(r"^v3_qtytake:([A-Za-z0-9]+):(\d{1,5})$")          # "Take {n}" (only {n} in stock)
_QTY_OK_RE = re.compile(r"^v3_qtyok:([A-Za-z0-9]+):(\d{1,5})$")              # "Yes, {n}" (an unusually large quantity)
_QTY_NO_RE = re.compile(r"^v3_qtyno:([A-Za-z0-9]+)$")
_SIMILAR_RE = re.compile(r"^v3_similar:(.*)$")                                 # "See similar" (same category as the product)


def option_id(line_id: str, slot: str, value: str) -> str | None:
    """Button / row id for "this value of this slot on this line", or None when it would be too long."""
    out = f"v3_opt:{line_id}:{slot}:{quote(value, safe='')}"
    return out if len(out) <= MAX_STRUCTURED_ID else None


def variant_id(line_id: str | None, sku: str, combo: dict[str, str]) -> str | None:
    """Button / row id for one in-stock combination (colour / size / material) of a product."""
    out = f"v3_var:{line_id or '-'}:{quote(sku, safe='')}:{urlencode({k: v for k, v in combo.items() if v})}"
    return out if len(out) <= MAX_STRUCTURED_ID else None


def remove_id(line_id: str) -> str:
    """Button / row id for "remove this cart line"."""
    return f"v3_rm:{line_id}"


def from_structured_button(button_id: str, cart: CartData, catalogue: Catalogue) -> FastPath | None:
    """
    Actions for a structured id (see above); None when the id is not one of these forms or its content no longer fits the shop.

    A tap on a line that has since left the cart is a harmless no-op (the open question is asked again), not a planner call.
    """
    raw = (button_id or "").strip()
    m = _OPT_RE.match(raw)
    if m:
        line_id, slot, value = m.group(1), m.group(2), unquote(m.group(3))
        line = cart.line(line_id)
        if line is None:
            return FastPath("stale_option", [], "button")
        view = catalogue.get(line["sku"])
        hits = view.resolve(slot, value) if view else []
        if len(hits) != 1:
            return None
        return FastPath("option", [UpdateLine(type="update_line", line_id=line["line_id"], fields=LineFields(**{slot: hits[0]}))], "button")
    m = _VAR_RE.match(raw)
    if m:
        line_id, sku, payload = m.group(1), unquote(m.group(2)), m.group(3)
        combo = {k: v for k, v in parse_qsl(payload) if k in _ATTR_SLOTS and v}
        view = catalogue.get(sku)
        if view is None or not combo:
            return None
        resolved = {}
        for attr, value in combo.items():
            hits = view.resolve(attr, value)
            if len(hits) != 1:
                return None
            resolved[attr] = hits[0]
        if line_id != "-":
            line = cart.line(line_id)
            if line is None:
                return FastPath("stale_option", [], "button")
            return FastPath("option", [UpdateLine(type="update_line", line_id=line["line_id"], fields=LineFields(**resolved))], "button")
        return FastPath("option", [AddLine(type="add_line", sku=view.sku, variant=Variant(**resolved))], "button")
    m = _QTY_TAKE_RE.match(raw)
    if m:
        line = cart.line(m.group(1))
        if line is None:
            return FastPath("stale_option", [], "button")
        return FastPath("qty_take", [UpdateLine(type="update_line", line_id=line["line_id"], fields=LineFields(qty=int(m.group(2))))], "button")
    m = _QTY_OK_RE.match(raw)
    if m:
        line = cart.line(m.group(1))
        if line is None:
            return FastPath("stale_option", [], "button")
        return FastPath("qty_ok", [UpdateLine(type="update_line", line_id=line["line_id"], fields=LineFields(qty=int(m.group(2))))], "button", qty_confirmed=True)
    m = _QTY_NO_RE.match(raw)
    if m:
        line = cart.line(m.group(1))
        if line is None:
            return FastPath("stale_option", [], "button")
        return FastPath("qty_no", [AskClarify(type="ask_clarify", slot="quantity")], "button")
    m = _SIMILAR_RE.match(raw)
    if m:
        view = catalogue.get(unquote(m.group(1)))
        return FastPath("similar", [BrowseCatalog(type="browse_catalog", category=(view.product.category if view else None) or None)], "button")
    m = _RM_RE.match(raw)
    if m:
        line = cart.line(m.group(1))
        if line is None:
            return FastPath("stale_option", [], "button")
        return FastPath("btn_remove", [RemoveLine(type="remove_line", line_id=line["line_id"])], "button")
    return None


def is_bare_gate(text: str, catalogue: Catalogue) -> bool:
    """
    A gate word on its own: at most 3 words and none of them names something the shop sells.

    "yes" / "cancel" / "ok confirm" are gate words; "I don't want this, I want shoes" and "cancel kurti" are sentences with
    their own content — they go to the planner, which may return several actions.
    """
    return len(indic_vocab.tokenize(text)) <= BARE_GATE_MAX_TOKENS and not catalogue.mentions(text)


def _variant_focus(cart: CartData, clarify: dict, catalogue: Catalogue):
    """The product a "2 red 1 blue" message is about: the line being worked on, else the product in focus, else the cart's only product."""
    line = cart.line(cart.state.get("focus_line_id"))
    sku = (line or {}).get("sku") or cart.state.get("focus_sku") or clarify.get("sku") or (cart.skus[0] if len(cart.skus) == 1 else None)
    return catalogue.get(sku)


def multi_variant(text: str, cart: CartData, clarify: dict, catalogue: Catalogue) -> FastPath | None:
    """
    "2 purple and one red and one green" → one ``add_line`` per understood pair (an existing line of that option gets its quantity set,
    an empty line is filled), and the options the product is not sold in are reported — never a reason to drop the valid pairs.
    """
    if clarify.get("slot") in ("name", "address", "mobile"):
        return None
    view = _variant_focus(cart, clarify, catalogue)
    parsed = agent_multivariant.parse(text, view)
    if parsed is None:
        return None
    focus_line = cart.line(cart.state.get("focus_line_id"))
    if focus_line is not None and focus_line["sku"] != view.sku:
        focus_line = None
    carried = {a: focus_line[a] for a in _ATTR_SLOTS if focus_line and focus_line.get(a)}
    attrs = {pair.attr for pair in parsed.pairs}
    # "3 kurtis" were asked for and the breakdown is for that line: split it, so a breakdown that does not add up ("2 red 2 green" for 3)
    # is answered with "that adds up to 4 but the order is for 3" instead of silently changing the quantity.
    if (
        focus_line is not None and focus_line.get("qty") and not parsed.missing and parsed.pairs and all(p.qty for p in parsed.pairs)
        and len(attrs) == 1 and not focus_line.get(next(iter(attrs)))
    ):
        parts = [
            SplitPart(variant=Variant(**{p.attr: p.value}), qty=p.qty) for p in parsed.pairs
        ]
        return FastPath("multi_variant", [SplitLine(type="split_line", line_id=focus_line["line_id"], parts=parts)], "option")
    actions: list[Action] = []
    for pair in parsed.pairs:
        variant = {**{a: v for a, v in carried.items() if a != pair.attr}, pair.attr: pair.value}
        actions.append(AddLine(type="add_line", sku=view.sku, variant=Variant(**variant), qty=pair.qty))
    path = FastPath("multi_variant", actions, "option")
    for miss in parsed.missing:
        path.unavailable.append({"sku": view.sku, "attr": miss.attr, "value": miss.text.title() if miss.attr == "color" else miss.text, "qty": miss.qty})
    return path


def _focus_sku(cart: CartData, clarify: dict) -> str | None:
    """The product the conversation is about: the last one shown / added, else the one on offer."""
    return cart.state.get("focus_sku") or (clarify.get("sku") if clarify.get("slot") == "order_offer" else None)


@dataclass
class FastPath:
    """A resolved ₹0 path: the actions to run and why the values they write are trustworthy."""

    name: str
    actions: list[Action] = field(default_factory=list)
    grounded_by: str | None = None
    reply_intent: str = "none"
    cancel_confirmed: bool = False
    confirm_ok: bool = False
    unavailable: list[dict] = field(default_factory=list)      # options the customer asked for that the product is not sold in
    not_in_cart: str | None = None                             # "remove chair": the thing named is not in the cart
    forced_issue: dict | None = None                           # an answer that cannot be applied (quantity < 1): {code, line_id, sku}
    qty_confirmed: bool = False                                # the customer confirmed an unusually large quantity


def _plain(text: str) -> str:
    """Words of the message, normalised (any script) — the form options are compared in."""
    return " ".join(indic_vocab.tokenize(text))


def _pick_option(options: list[str], text: str) -> str | None:
    """The option the customer named (exact, case-insensitive) — or its 1-based number — else None."""
    plain = _plain(text)
    for opt in options:
        if _plain(opt) == plain:
            return opt
    n = agent_text.is_bare_number(text)
    if n is not None and 1 <= n <= len(options):
        return options[n - 1]
    if text.strip().endswith("…") and plain:         # a title we shortened ourselves ("Pocket Linen Kurti…"): the one option it starts
        starts = [opt for opt in options if _plain(opt).startswith(plain)]
        if len(starts) == 1:
            return starts[0]
    return None


def _apply_option(clarify: dict, option: str, how: str) -> FastPath | None:
    """The action for "the customer picked `option`" at the open question."""
    slot, line_id = clarify.get("slot"), clarify.get("line_id")
    if slot in _ATTR_SLOTS and line_id:
        return FastPath("option", [UpdateLine(type="update_line", line_id=line_id, fields=LineFields(**{slot: option}))], grounded_by=how)
    if slot == "variant":
        combos, options = clarify.get("combos") or [], clarify.get("options") or []
        combo = combos[options.index(option)] if option in options and options.index(option) < len(combos) else None
        if combo and line_id:
            return FastPath("option", [UpdateLine(type="update_line", line_id=line_id, fields=LineFields(**combo))], grounded_by=how)
        if combo and clarify.get("sku"):
            return FastPath("option", [AddLine(type="add_line", sku=clarify["sku"], variant=Variant(**combo))], grounded_by=how)
        return None
    if slot == "payment" and option in _PAYMENT_WORDS.values():
        return FastPath("option", [SetPayment(type="set_payment", method=option)], grounded_by=how)
    if slot == "product":
        return FastPath("option", [ShowProduct(type="show_product", sku=option)], grounded_by=how)
    return None


def from_button(button_id: str, cart: CartData, catalogue: Catalogue, clarify: dict | None = None) -> FastPath | None:
    """Actions for one of OUR button / list-row ids (``v3_…``); None for an id that is not ours."""
    bid = (button_id or "").lower()
    if not bid.startswith("v3_"):
        return None
    structured = from_structured_button(button_id, cart, catalogue)
    if structured is not None:
        return structured
    clarify = clarify if clarify is not None else cart.state.get("last_clarify") or {}
    if bid == "v3_confirm":
        return FastPath("btn_confirm", [ConfirmOrder(type="confirm_order")], "button", confirm_ok=True)
    if bid == "v3_cancel":
        return FastPath("btn_cancel", [Cancel(type="cancel")], "button")
    if bid == "v3_continue":
        return FastPath("btn_continue", [], "button")
    if bid == "v3_fresh":
        return FastPath("btn_fresh", [Cancel(type="cancel")], "button", cancel_confirmed=True)
    if bid == "v3_cancel_yes":
        return FastPath("btn_cancel_yes", [Cancel(type="cancel")], "button", cancel_confirmed=True)
    if bid == "v3_cancel_no":
        return FastPath("btn_cancel_no", [], "button")
    if bid == "v3_remove_menu":
        return FastPath("btn_remove_menu", [AskClarify(type="ask_clarify", slot="remove")], "button")
    if bid == "v3_change":
        return FastPath("btn_change", [AskClarify(type="ask_clarify", slot="change")], "button")
    if bid in ("v3_pay_cod", "v3_pay_upi"):
        return FastPath("btn_pay", [SetPayment(type="set_payment", method="COD" if bid.endswith("cod") else "UPI")], "button")
    if bid == "v3_browse":
        return FastPath("btn_browse", [BrowseCatalog(type="browse_catalog")], "button")
    m = re.fullmatch(r"v3_opt_(\d+)", bid)
    if m:
        options = clarify.get("options") or []
        idx = int(m.group(1))
        return _apply_option(clarify, options[idx - 1], "button") if 1 <= idx <= len(options) else None
    for prefix, build in (
        ("v3_sku_", lambda v: ShowProduct(type="show_product", sku=v.sku)),
        ("v3_order_", lambda v: AddLine(type="add_line", sku=v.sku)),
        ("v3_upsell_", lambda v: AddLine(type="add_line", sku=v.sku)),
    ):
        if bid.startswith(prefix):
            view = catalogue.get(bid[len(prefix):])
            return FastPath(f"btn_{prefix.strip('v3_').strip('_')}", [build(view)], "button") if view else None
    return None


def resolve(
    *, text: str, button_id: str | None, cart: CartData, catalogue: Catalogue, client, is_whatsapp: bool = True,
    has_pending_order: bool = False, restorable: tuple[list[dict], str | None] = ([], None), clarify: dict | None = None,
) -> FastPath | None:
    """
    The fast path for this message, or None (→ planner).

    `clarify` = the open question as the customer saw it when they typed (``CartData.clarify_as_of``); default: the newest one.
    """
    clarify = clarify if clarify is not None else (cart.state.get("last_clarify") or {})
    if button_id:
        fp = from_button(button_id, cart, catalogue, clarify)
        if fp is not None:
            return fp
    text = (text or "").strip()
    if not text:
        return None
    slot = clarify.get("slot")

    # An exact SKU (and nothing else) shows that product; so does its exact name ("A-Line Cotton Kurti").
    view = catalogue.get(text)
    if view is not None:
        return FastPath("sku", [ShowProduct(type="show_product", sku=view.sku)], "option")
    named = catalogue.get_by_name(text)
    if named is not None:
        return FastPath("name", [ShowProduct(type="show_product", sku=named.sku)], "option")

    # "I don't want the kurti": the whole sentence is a removal of something in the cart — ₹0, ahead of every option / decline reading.
    removal = agent_removal.detect(text, cart, catalogue)
    if removal is not None and removal.absent:
        # "remove chair" — but a name / address being typed ("Drop Point Surat") is the answer, not a removal: while one is open only a
        # sentence that BEGINS with a removal verb counts.
        if slot not in ("name", "address", "mobile") or agent_removal.starts_with_removal_verb(text):
            return FastPath("remove_absent", [], "gate", not_in_cart=removal.absent)
    elif removal is not None:
        if removal.line_ids:
            return FastPath("remove", [RemoveLine(type="remove_line", line_id=lid) for lid in removal.line_ids], "gate")
        return FastPath("remove_which", [AskClarify(type="ask_clarify", slot="remove", options=removal.ambiguous)], "gate")

    # "2 purple and one red and one green": every understood pair is applied, what the product does not come in is reported.
    multi = multi_variant(text, cart, clarify, catalogue)
    if multi is not None:
        return multi

    # The question that is open.
    if slot == "remove":
        opts = clarify.get("options") or []
        n = agent_text.is_bare_number(text)
        if n is not None and 1 <= n <= len(opts):
            return FastPath("remove_pick", [RemoveLine(type="remove_line", line_id=opts[n - 1])], "number")
        named = agent_removal.detect(f"remove {text}", cart, catalogue)         # "the kurti" answers "which one should I remove?"
        if named is not None and named.line_ids:
            return FastPath("remove_pick", [RemoveLine(type="remove_line", line_id=lid) for lid in named.line_ids], "option")
    if slot == "variant":
        picked = _pick_option(clarify.get("options") or [], text)
        if picked is not None:
            return _apply_option(clarify, picked, "number" if agent_text.is_bare_number(text) else "option")
    if slot in _ATTR_SLOTS and clarify.get("line_id"):
        options = clarify.get("options") or []
        picked = _pick_option(options, text)
        if picked is None and options and len(indic_vocab.tokenize(text)) <= 2 and not any(ch.isdigit() for ch in text):
            line = cart.line(clarify["line_id"])
            product = catalogue.get(line["sku"]) if line else None
            hits = product.resolve(slot, text) if product else []
            picked = hits[0] if len(hits) == 1 and any(_plain(hits[0]) == _plain(o) for o in options) else None
        if picked is not None:
            return _apply_option(clarify, picked, "option" if not agent_text.is_bare_number(text) else "number")
    bare = is_bare_gate(text, catalogue)
    if slot == "qty_confirm" and clarify.get("line_id") and bare:
        # "Did you mean 150?" — yes takes it, no asks again, a different number is the new quantity (handled just below as a quantity answer).
        if cw.is_yes(text):
            return FastPath("qty_ok", [UpdateLine(type="update_line", line_id=clarify["line_id"], fields=LineFields(qty=int(clarify.get("qty") or 1)))], "gate", qty_confirmed=True)
        if cw.matches_whole("no", text):
            return FastPath("qty_no", [AskClarify(type="ask_clarify", slot="quantity")], "gate")
    if slot in ("quantity", "qty_confirm") and clarify.get("line_id"):
        n = agent_text.quantity_answer(text)
        negative = bool(re.fullmatch(r"\s*[-−–]\s*\d+\s*", text))
        if negative or (n is not None and n < 1):
            line = cart.line(clarify["line_id"])
            return FastPath("qty_min", [], "number", forced_issue={"code": "QTY_MIN", "line_id": clarify["line_id"], "sku": (line or {}).get("sku")})
        if n is not None and 1 <= n <= 10_000:
            return FastPath("quantity", [UpdateLine(type="update_line", line_id=clarify["line_id"], fields=LineFields(qty=n))], "number")
    if slot == "payment":
        word = _plain(text)
        method = _PAYMENT_WORDS.get(word)
        if method is None:
            n = agent_text.is_bare_number(text)
            opts = clarify.get("options") or []
            method = opts[n - 1] if n is not None and 1 <= n <= len(opts) else None
        if method:
            return FastPath("payment", [SetPayment(type="set_payment", method=method)], "option")
    if slot == "product":
        n = agent_text.is_bare_number(text)
        opts = clarify.get("options") or []
        if n is not None and 1 <= n <= len(opts):
            return FastPath("product_number", [ShowProduct(type="show_product", sku=opts[n - 1])], "number")
    bare = is_bare_gate(text, catalogue)
    if slot == "order_offer" and clarify.get("sku") and bare:
        if cw.is_yes(text):
            return FastPath("offer_yes", [AddLine(type="add_line", sku=clarify["sku"])], "gate")
        if cw.matches_whole("no", text):
            return FastPath("offer_no", [BrowseCatalog(type="browse_catalog")], "gate")
    if slot == "cancel" and bare:
        if order_recovery.has_explicit_cancel_keyword(text) or cw.is_yes(text):
            return FastPath("cancel_yes", [Cancel(type="cancel")], "gate", cancel_confirmed=True)
        if cw.is_no(text):
            return FastPath("cancel_no", [], "gate")

    # "continue" / "yes" / "I want to continue this" right after a cancel: put the cancelled lines back.
    restorable_lines, restorable_payment = restorable
    if restorable_lines and not cart.lines and order_recovery.is_resume_request(text):
        actions: list[Action] = [
            AddLine(type="add_line", sku=ln["sku"], variant=Variant(color=ln.get("color"), size=ln.get("size"), material=ln.get("material")), qty=ln.get("qty"))
            for ln in restorable_lines
        ]
        if restorable_payment:
            actions.append(SetPayment(type="set_payment", method=restorable_payment))
        return FastPath("restore_cart", actions, "gate")

    # The summary on screen.
    if cart.summary_on_screen and bare and agent_text.looks_like_confirmation(text):
        return FastPath("confirm", [ConfirmOrder(type="confirm_order")], "gate", confirm_ok=True)
    if cart.summary_on_screen and bare and cw.matches_whole("no", text):
        return FastPath("summary_no", [AskClarify(type="ask_clarify", slot="change")], "gate")

    # Cancel wording (explicit only — the executor asks when it is not).
    if bare and order_recovery.has_explicit_cancel_keyword(text) and (cart.lines or has_pending_order):
        return FastPath("cancel", [Cancel(type="cancel")], "gate")

    # "images / photo / pic / dikhao" about the product in focus (a message naming another product goes to the planner).
    focus = _focus_sku(cart, clarify)
    tokens = indic_vocab.tokenize(text)
    if focus and catalogue.get(focus) is not None and len(tokens) <= _IMAGE_MAX_TOKENS and set(tokens) & IMAGE_WORDS and not catalogue.mentions(text):
        return FastPath("images", [SendImages(type="send_images", sku=focus)], "side_topic")

    # Reactions ("wah", 👍), declines ("no thanks", "bas") and "what sells best" — social one-liners, never a search. A name / address /
    # phone is being typed: those words may be the answer, so the planner decides.
    free_text_open = slot in ("name", "address", "mobile")
    if not free_text_open:
        if agent_social.is_decline(text, bare_no_ok=slot in (None, "", "product", "order_offer")):
            return FastPath("decline", [], "side_topic", reply_intent="decline")
        if agent_social.is_reaction(text):
            return FastPath("reaction", [], "side_topic", reply_intent="reaction")
        if agent_social.is_bestseller_request(text):
            return FastPath("bestseller", [BrowseCatalog(type="browse_catalog", sort="bestseller")], "side_topic")

    # Side questions and social messages (the shop's own facts; free-text slots never reach this).
    topic = side_questions.detect_topic(text, next_slot=_SLOT_TO_SIDE.get(slot or ""))
    if topic == "human":
        return FastPath("human", [HandoffHuman(type="handoff_human", reason="customer asked for a person")], "side_topic")
    if topic == "change_product":
        return FastPath("change_product", [BrowseCatalog(type="browse_catalog")], "side_topic")
    if topic == "thanks":
        return FastPath("thanks", [], "side_topic", reply_intent="thanks")
    if topic == "bye":
        return FastPath("bye", [], "side_topic", reply_intent="goodbye")
    if topic in QUESTION_TOPICS:
        return FastPath("side_topic", [AnswerQuestion(type="answer_question", topic=topic)], "side_topic")
    if cw.is_greeting_message(text):
        return FastPath("greeting", [], "side_topic", reply_intent="greeting")

    # "Saree" / "yellow saree" / "dikha bhai red kurti": only the shop's own words and chatter → browse the category / search them, no planner.
    if not free_text_open:
        lookup = agent_catalogue_words.parse(text, catalogue)
        if lookup is not None:
            if lookup.kind == "product":
                return FastPath("product_name", [ShowProduct(type="show_product", sku=lookup.value)], "side_topic")
            if lookup.kind == "category":
                return FastPath("category", [BrowseCatalog(type="browse_catalog", category=lookup.value)], "side_topic")
            return FastPath("catalogue_search", [SearchCatalog(type="search_catalog", query=lookup.value)], "side_topic")
    return None
