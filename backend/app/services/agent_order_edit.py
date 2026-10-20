"""
Agent v3 — change a just-placed order before it is paid: "Ohh I want 1 black and 1gray" right after confirming 2 × Black/XL.

Conv=60 (ORD-2026-0079): the order was confirmed (``pending_payment``), the cart was empty, the planner saw a bare quantity-per-colour list,
asked a clarifying question and the bot dumped an unrelated catalogue — twice. The customer plainly meant "change my order".

What happens now (no planner, ₹0):

1. The order is *editable* when it is ``pending_payment``, has no payment screenshot on file, and was created at most
   ``order_edit_window_minutes`` (30) ago.
2. The message is read by ``agent_multivariant`` against THAT order's product. Attributes the customer did not state (the size) are
   inherited from the existing line: "1 black and 1 gray" on Black/XL → 1× Black/XL, 1× Grey/XL. A colour that does not come in the
   inherited size asks for a size from the colour's own available sizes; a colour the product is not sold in is reported with the
   real options. Stock counts the order's own reservation as available again.
3. The proposal ("Update order #… to: 1× Black/XL, 1× Grey/XL — ₹1,758?") is stored in ``cart.state["order_edit"]`` and asks
   [Update order | Keep as is] (a typed yes / no works too — Instagram has no buttons).
4. **Update** edits the SAME order in place (decision: the number the customer already holds and may have quoted stays valid; a new
   number would orphan the old payment request): the order row is locked, line items replaced, the total recalculated, reservations
   swapped (the old ones released, new ones taken), an ``order_edited_by_customer`` audit row written, and the payment instructions are sent
   again with the new amount and a fresh QR. **Keep** leaves everything as it was.
5. A screenshot already on file, or an order past ``pending_payment``, is never edited: the customer is offered a NEW order for what they
   asked for instead.

Every word comes from the ``oedit_*`` templates (five languages) and the database — the model is not involved.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from app.config import get_settings
from app.schemas.agent_actions import AddLine, Variant
from app.services import agent_multivariant, indic_vocab
from app.services import command_words as cw
from app.services.agent_catalog import Catalogue, ProductView
from app.services.agent_renderer import Reply, text_for, TABLE_LANGUAGES
from app.services.language_templates import format_price

logger = logging.getLogger("app.services.agent_order_edit")

BTN_UPDATE = "v3_oedit_yes"
BTN_KEEP = "v3_oedit_no"
BTN_NEW = "v3_oedit_new"
BUTTON_IDS = frozenset({BTN_UPDATE, BTN_KEEP, BTN_NEW})
#: A proposal stays answerable (button or typed yes / no) this long.
PROPOSAL_TTL_SECONDS = 600
_ATTRS = ("color", "size", "material")


@dataclass
class Built:
    """The result of turning the customer's pairs into lines of the order: lines, or what is still needed, or why it cannot be done."""

    lines: list[dict[str, Any]] = field(default_factory=list)
    need: dict[str, Any] | None = None                  # {"pair": index, "attr": "size", "options": [...], "what": "Grey", "have": "XL"}
    problem: tuple[str, dict[str, Any]] | None = None    # (template key, format arguments)


def _label(line: dict[str, Any]) -> str:
    """'Black/XL' for a line (the product name when it has no variant)."""
    variant = "/".join(str(line[a]) for a in _ATTRS if line.get(a))
    return variant or str(line.get("name") or "")


def items_text(lines: list[dict[str, Any]]) -> str:
    """'1× Black/XL, 1× Grey/XL'."""
    return ", ".join(f"{int(ln['qty'])}× {_label(ln)}" for ln in lines)


def _order_lines(order) -> list[dict[str, Any]]:
    """The order's current lines as plain dicts (flat columns for a legacy order without line items)."""
    items = getattr(order, "line_items", None) or []
    if items:
        return [
            {"sku": li.product_sku, "name": li.product_name, "color": li.variant_color, "size": li.variant_size,
             "material": li.variant_material, "qty": int(li.quantity), "unit_price": float(li.unit_price)}
            for li in items
        ]
    return [{"sku": order.product_sku, "name": order.product_name, "color": order.variant_color, "size": order.variant_size,
             "material": getattr(order, "variant_material", None), "qty": int(order.quantity), "unit_price": float(order.unit_price)}]


def _aware(value: datetime | None) -> datetime | None:
    """Timezone-aware copy (naive = UTC)."""
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


async def has_live_proof(db, order_id: int) -> bool:
    """True when a payment screenshot is on file for the order that was not withdrawn or rejected (pending or approved)."""
    from app.models.payment_proof import PROOF_REJECTED, PROOF_WITHDRAWN, PaymentProof

    count = (await db.execute(
        select(func.count()).select_from(PaymentProof).where(
            PaymentProof.order_id == order_id, PaymentProof.status.notin_((PROOF_WITHDRAWN, PROOF_REJECTED)),
        )
    )).scalar_one()
    return int(count) > 0


def target_order(pending: list, *, now: datetime | None = None):
    """The order a "change it" message is about: the newest unpaid / under-review order created within the edit window (else None)."""
    now = now or datetime.now(timezone.utc)
    window = timedelta(minutes=get_settings().order_edit_window_minutes)
    for order in pending:                            # newest first
        created = _aware(getattr(order, "created_at", None))
        if created is not None and now - created <= window:
            return order
    return None


def _single_product_view(order, catalogue: Catalogue) -> ProductView | None:
    """The catalogue view of the order's product — only when every line is the same product (otherwise the planner decides)."""
    skus = {ln["sku"] for ln in _order_lines(order) if ln.get("sku")}
    if len(skus) != 1:
        return None
    return catalogue.get(next(iter(skus)))


def _own_units(order_lines: list[dict[str, Any]], fixed: dict[str, str | None]) -> int:
    """Units of the order's own lines that match `fixed` — its reservation is given back when it is edited, so they count as available."""
    total = 0
    for ln in order_lines:
        if all((ln.get(a) or "").lower() == (v or "").lower() for a, v in fixed.items() if v):
            total += int(ln["qty"])
    return total


def build_lines(order, view: ProductView, pairs: list[dict[str, Any]]) -> Built:
    """
    The proposed lines for `pairs` ({attr, value, qty, extra}): missing attributes are inherited from the order's own line, a
    combination the product does not have asks for the attribute from the real options, stock counts the order's own units.
    """
    current = _order_lines(order)
    base = current[0] if current else {}
    out = Built()
    for index, pair in enumerate(pairs):
        fixed: dict[str, str | None] = {a: base.get(a) for a in view.needed_attrs if a != pair["attr"] and base.get(a)}
        fixed.update(pair.get("extra") or {})
        fixed[pair["attr"]] = pair["value"]
        missing = [a for a in view.needed_attrs if not fixed.get(a)]
        if missing or view.variant_for(fixed) is None:
            attr = missing[0] if missing else next((a for a in view.needed_attrs if a != pair["attr"]), None)
            options = view.values(attr, {pair["attr"]: pair["value"]}) if attr else []
            if not attr or not options:
                out.problem = ("oedit_unavailable", {"what": pair["value"], "product": view.name})
                return out
            out.need = {"pair": index, "attr": attr, "options": options, "what": pair["value"], "have": fixed.get(attr) or base.get(attr) or ""}
            return out
        qty = int(pair.get("qty") or 1)
        available = view.available(fixed)
        if available is not None and qty > available + _own_units(current, fixed):
            out.problem = ("oedit_stock_short", {"n": max(0, available + _own_units(current, fixed)), "what": _label({**fixed})})
            return out
        out.lines.append({
            "sku": view.sku, "name": view.name, "color": fixed.get("color"), "size": fixed.get("size"), "material": fixed.get("material"),
            "qty": qty, "unit_price": view.unit_price(fixed),
        })
    return out


def total_of(lines: list[dict[str, Any]]) -> float:
    """Grand total of lines."""
    return sum(int(ln["qty"]) * float(ln["unit_price"]) for ln in lines)


def _pairs_state(parsed: agent_multivariant.Parse) -> list[dict[str, Any]]:
    """The parsed pairs as plain, JSON-able dicts (stored on the cart while the customer decides)."""
    return [{"attr": p.attr, "value": p.value, "qty": p.qty or 1, "extra": {}} for p in parsed.pairs]


def _reply(lang: str, text: str, *, buttons: list[tuple[str, str]] | None = None, images: list[tuple[str, str]] | None = None,
           fallback: str | None = None, is_whatsapp: bool = True, hint: str | None = None) -> Reply:
    """A ``Reply`` for an order-edit message (the typed yes/no hint replaces the buttons off WhatsApp)."""
    if buttons and not is_whatsapp:
        text, buttons = f"{text}\n\n{hint}" if hint else text, None
    return Reply(text=text, buttons=buttons if is_whatsapp else None, images=images, image_fallback_text=fallback, terminal=True,
                 localized=lang in TABLE_LANGUAGES)


def _proposal(lang: str, order, lines: list[dict[str, Any]], is_whatsapp: bool) -> Reply:
    """'Update order #… to: 1× Black/XL, 1× Grey/XL — ₹1,758?' with [Update order | Keep as is]."""
    new_total, old_total = total_of(lines), float(order.total_amount or 0)
    was = text_for(lang, "oedit_was", old=format_price(old_total)) if abs(new_total - old_total) > 0.5 else ""
    text = text_for(lang, "oedit_propose", number=order.order_number, items=items_text(lines), total=format_price(new_total), was=was)
    return _reply(
        lang, text, buttons=[(BTN_UPDATE, text_for(lang, "btn_oedit_update")), (BTN_KEEP, text_for(lang, "btn_oedit_keep"))],
        is_whatsapp=is_whatsapp, hint=text_for(lang, "oedit_yes_hint"),
    )


def _ask_attr(lang: str, need: dict[str, Any], is_whatsapp: bool) -> Reply:
    """'Grey isn't available in XL. Available: M, L. Which one would you like for Grey?' (typed answer)."""
    text = text_for(lang, "oedit_need_attr", what=need["what"], have=need["have"] or "—", options=", ".join(need["options"]))
    return _reply(lang, text, is_whatsapp=is_whatsapp)


def _blocked(lang: str, order, lines: list[dict[str, Any]], *, live_proof: bool, is_whatsapp: bool) -> Reply:
    """The order has a screenshot on file or is past pending_payment: offer a NEW order for what the customer asked for."""
    key = "oedit_blocked_proof" if live_proof or order.status == "payment_submitted" else "oedit_blocked_paid"
    return _reply(
        lang, text_for(lang, key, number=order.order_number, items=items_text(lines)),
        buttons=[(BTN_NEW, text_for(lang, "btn_new_order"))], is_whatsapp=is_whatsapp, hint=text_for(lang, "oedit_new_hint"),
    )


async def maybe_handle(
    *, db, client, conv, cart, catalogue: Catalogue, pending: list, text: str, button_id: str | None, lang: str, is_whatsapp: bool = True,
    open_slot: str | None = None,
):
    """
    The order-edit step of a turn: a ``Reply`` (answer now, no planner), a ``list[AddLine]`` (start a NEW order for what was asked), or
    None when this message is not about editing an order. Mutates ``cart.state["order_edit"]``; the caller saves the cart.
    """
    state = cart.state.get("order_edit")
    if state and time.time() - float(state.get("ts", 0)) > PROPOSAL_TTL_SECONDS:
        cart.state.pop("order_edit", None)
        state = None
    clean = (text or "").strip()

    # ── an answer to an open proposal (a tapped button, or a typed yes / no / the asked-for option) ─────
    if button_id in BUTTON_IDS and not state:
        return _reply(lang, text_for(lang, "oedit_expired"), is_whatsapp=is_whatsapp)
    if state and (button_id in BUTTON_IDS or (clean and open_slot is None)):
        answered = await _answer(db, client, conv, cart, catalogue, pending, state, button_id, clean, lang, is_whatsapp)
        if answered is not None:
            return answered

    # ── a new request: "1 black and 1 grey" while a fresh order waits ────────
    if cart.lines or not clean or open_slot in ("name", "address", "mobile"):
        return None
    order = target_order(pending)
    if order is None and _looks_like_option_list(clean):
        order = await _recent_paid_order(db, client.id, conv)          # a just-paid / just-confirmed order cannot be edited either: offer a new one
    if order is None or order.status not in ("pending_payment", "payment_submitted", "paid"):
        return None
    view = _single_product_view(order, catalogue)
    parsed = agent_multivariant.parse(clean, view) if view is not None else None
    if parsed is None:
        return None
    pairs = _pairs_state(parsed)
    live_proof = await has_live_proof(db, order.id)
    if parsed.missing:
        options = view.values(parsed.missing[0].attr)
        what = parsed.missing[0].text.title() if parsed.missing[0].attr == "color" else parsed.missing[0].text
        logger.info("ORDER_EDIT conv=%s order=%s unavailable=%s", conv.id, order.order_number, what)
        return _reply(lang, text_for(lang, "oedit_unavailable_options", what=what, product=view.name, options=", ".join(options)), is_whatsapp=is_whatsapp)
    if order.status != "pending_payment" or live_proof:
        cart.state["order_edit"] = {"order_id": order.id, "pairs": pairs, "ts": time.time(), "blocked": True}
        shown = [{"qty": p["qty"], **{p["attr"]: p["value"]}} for p in pairs]
        logger.info("ORDER_EDIT conv=%s order=%s blocked proof=%s status=%s", conv.id, order.order_number, live_proof, order.status)
        return _blocked(lang, order, shown, live_proof=live_proof, is_whatsapp=is_whatsapp)
    built = build_lines(order, view, pairs)
    if built.problem:
        key, args = built.problem
        return _reply(lang, text_for(lang, key, **args), is_whatsapp=is_whatsapp)
    if built.need:
        cart.state["order_edit"] = {"order_id": order.id, "pairs": pairs, "ts": time.time(), "need": built.need}
        return _ask_attr(lang, built.need, is_whatsapp)
    cart.state["order_edit"] = {"order_id": order.id, "pairs": pairs, "ts": time.time()}
    logger.info("ORDER_EDIT conv=%s order=%s proposed=%s total=%s", conv.id, order.order_number, items_text(built.lines), total_of(built.lines))
    return _proposal(lang, order, built.lines, is_whatsapp)


async def _answer(db, client, conv, cart, catalogue: Catalogue, pending: list, state: dict[str, Any], button_id: str | None, clean: str,
                  lang: str, is_whatsapp: bool):
    """Resolve a tap / typed answer against the open proposal; None when the message is something else (a new request is then read)."""
    order = next((o for o in pending if o.id == state.get("order_id")), None) or await _load_order(db, client.id, state.get("order_id"))
    view = _single_product_view(order, catalogue) if order is not None else None
    if order is None or view is None:
        cart.state.pop("order_edit", None)
        return _reply(lang, text_for(lang, "oedit_expired"), is_whatsapp=is_whatsapp) if button_id in BUTTON_IDS else None
    typed = not button_id
    if state.get("blocked"):
        # The order cannot be edited: "yes" / [New order] starts a NEW order for what was asked, "no" leaves things as they are.
        if button_id == BTN_NEW or (typed and cw.is_yes(clean)):
            cart.state.pop("order_edit", None)
            return [
                AddLine(type="add_line", sku=view.sku, variant=Variant(**{p["attr"]: p["value"]}), qty=p.get("qty")) for p in state.get("pairs", [])
            ]
        if button_id == BTN_KEEP or (typed and cw.is_no(clean)):
            cart.state.pop("order_edit", None)
            return _reply(lang, text_for(lang, "oedit_kept", number=order.order_number, items=items_text(_order_lines(order)),
                                         total=format_price(order.total_amount)), is_whatsapp=is_whatsapp)
        return None
    if button_id == BTN_UPDATE or (typed and not state.get("need") and cw.is_yes(clean)):
        return await _apply(db, client, conv, cart, catalogue, order, view, state, lang, is_whatsapp)
    if button_id == BTN_KEEP or (typed and not state.get("need") and cw.is_no(clean)):
        cart.state.pop("order_edit", None)
        return _reply(lang, text_for(lang, "oedit_kept", number=order.order_number, items=items_text(_order_lines(order)),
                                     total=format_price(order.total_amount)), is_whatsapp=is_whatsapp)
    if button_id == BTN_NEW:
        cart.state.pop("order_edit", None)
        return [
            AddLine(type="add_line", sku=view.sku, variant=Variant(**{p["attr"]: p["value"], **(p.get("extra") or {})}), qty=p.get("qty"))
            for p in state.get("pairs", [])
        ]
    if state.get("need") and clean:
        return await _answer_need(db, client, cart, order, view, state, clean, lang, is_whatsapp)
    return None


def _looks_like_option_list(text: str) -> bool:
    """Cheap pre-check (no database): at least two ``<qty> <option>`` segments — the only shape that can be a change to an order."""
    segments = agent_multivariant._segments(text)
    return len(segments) >= 2 and any(agent_multivariant._quantity(tok) is not None for seg in segments for tok in seg)


async def _recent_paid_order(db, client_id: int, conv):
    """The conversation's newest PAID order created within the edit window (None when there is none) — paid orders are not in the open-payment list."""
    from app.models.order import Order
    from app.services.order_state_machine import ORDER_PAID

    since = datetime.now(timezone.utc) - timedelta(minutes=get_settings().order_edit_window_minutes)
    return (await db.execute(
        select(Order).where(
            Order.client_id == client_id, Order.status == ORDER_PAID, Order.created_at >= since, Order.conversation_id == conv.id,
        ).order_by(Order.id.desc()).limit(1)
    )).scalar_one_or_none()


async def _load_order(db, client_id: int, order_id: int | None):
    """The order by id for this client (None when missing)."""
    if not order_id:
        return None
    from app.models.order import Order

    return (await db.execute(select(Order).where(Order.id == order_id, Order.client_id == client_id))).scalar_one_or_none()


async def _answer_need(db, client, cart, order, view: ProductView, state: dict[str, Any], text: str, lang: str, is_whatsapp: bool):
    """The customer answered "which size for Grey?": fill it in and propose again (an unknown answer repeats the question)."""
    need = state["need"]
    wanted = view.resolve(need["attr"], text)
    choice = next((o for o in need["options"] if wanted and o.lower() == wanted[0].lower()), None)
    if choice is None:
        plain = " ".join(indic_vocab.tokenize(text))
        choice = next((o for o in need["options"] if " ".join(indic_vocab.tokenize(o)) == plain), None)
    if choice is None:
        return _ask_attr(lang, need, is_whatsapp)
    pairs = state["pairs"]
    pairs[need["pair"]].setdefault("extra", {})[need["attr"]] = choice
    built = build_lines(order, view, pairs)
    if built.problem:
        cart.state.pop("order_edit", None)
        key, args = built.problem
        return _reply(lang, text_for(lang, key, **args), is_whatsapp=is_whatsapp)
    if built.need:
        state.update(need=built.need, ts=time.time())
        cart.state["order_edit"] = state
        return _ask_attr(lang, built.need, is_whatsapp)
    state.pop("need", None)
    state["ts"] = time.time()
    cart.state["order_edit"] = state
    return _proposal(lang, order, built.lines, is_whatsapp)


async def _apply(db, client, conv, cart, catalogue: Catalogue, order, view: ProductView, state: dict[str, Any], lang: str, is_whatsapp: bool) -> Reply:
    """Edit the order in place (see the module docstring) and answer with the new payment instructions."""
    from app.models.order import Order
    from app.models.order_audit_log import OrderAuditLog
    from app.models.order_line_item import OrderLineItem
    from app.services import payment_verification_service as pvs
    from app.services import stock_reservation_service

    try:
        locked = (await db.execute(
            select(Order).where(Order.id == order.id, Order.client_id == client.id).with_for_update().execution_options(populate_existing=True)
        )).scalar_one()
        if locked.status != "pending_payment" or await has_live_proof(db, locked.id):
            cart.state.pop("order_edit", None)
            await db.commit()                    # nothing was written: just release the row lock (a rollback would expire conv / client)
            return _blocked(lang, locked, [{"qty": p["qty"], **{p["attr"]: p["value"]}} for p in state["pairs"]], live_proof=True, is_whatsapp=is_whatsapp)
        built = build_lines(locked, view, state["pairs"])
        if built.problem or built.need or not built.lines:
            cart.state.pop("order_edit", None)
            await db.commit()
            key, args = built.problem or ("oedit_expired", {})
            return _reply(lang, text_for(lang, key, **args), is_whatsapp=is_whatsapp)
        before = _order_lines(locked)
        old_total = float(locked.total_amount or 0)
        for li in list(locked.line_items):
            await db.delete(li)
        await db.flush()
        for number, ln in enumerate(built.lines, start=1):
            db.add(OrderLineItem(
                order_id=locked.id, product_id=view.product.id, product_name=view.name, product_sku=view.sku, variant_color=ln["color"],
                variant_size=ln["size"], variant_material=ln["material"], quantity=ln["qty"], unit_price=ln["unit_price"],
                subtotal=ln["qty"] * ln["unit_price"], line_number=number,
            ))
        first = built.lines[0]
        locked.product_id, locked.product_name, locked.product_sku = view.product.id, view.name, view.sku
        locked.variant_color, locked.variant_size, locked.variant_material = first["color"], first["size"], first["material"]
        locked.quantity, locked.unit_price = first["qty"], first["unit_price"]
        locked.total_amount = total_of(built.lines)
        await db.flush()
        await stock_reservation_service.replace_lines(
            db, locked.id, [(view.product.id, ln["color"], ln["size"], ln["material"], ln["qty"]) for ln in built.lines],
        )
        db.add(OrderAuditLog(
            client_id=client.id, order_id=locked.id, actor_user_id=None, actor_name="customer", action="order_edited_by_customer",
            from_status=locked.status, to_status=locked.status,
            detail={"before": before, "after": built.lines, "total_before": old_total, "total_after": float(locked.total_amount)},
        ))
        await db.commit()
        await db.refresh(locked, attribute_names=["line_items"])
    except Exception as exc:  # noqa: BLE001 - the order stays exactly as it was
        logger.error("ORDER_EDIT apply failed conv=%s order=%s: %s", getattr(conv, "id", None), getattr(order, "order_number", None), exc)
        try:
            await db.rollback()
            await db.refresh(conv)             # the rollback expired the rows the rest of the turn reads (async lazy loads would raise)
            await db.refresh(client)
        except Exception:  # noqa: BLE001
            pass
        cart.state.pop("order_edit", None)
        return _reply(lang, text_for(lang, "oedit_failed"), is_whatsapp=is_whatsapp)
    cart.state.pop("order_edit", None)
    instruction = await pvs.build_payment_instruction(db, locked, client, conv)
    await db.commit()                                   # build_payment_instruction stamps conv.last_payment_instruction_at
    head = text_for(lang, "oedit_updated", number=locked.order_number, items=items_text(built.lines), total=format_price(locked.total_amount))
    reply = _reply(lang, f"{head}\n\n{text_for(lang, 'oedit_pay_new')}\n\n{instruction.text}", is_whatsapp=is_whatsapp)
    if instruction.qr_url:
        reply.images = [(instruction.qr_url, instruction.closing)]
        reply.image_fallback_text = instruction.closing
    reply.amounts.update({int(round(locked.total_amount)), int(round(old_total))})
    logger.info(
        "ORDER_EDIT conv=%s order=%s applied lines=%s total=%s→%s", getattr(conv, "id", None), locked.order_number, items_text(built.lines),
        old_total, locked.total_amount,
    )
    return reply
