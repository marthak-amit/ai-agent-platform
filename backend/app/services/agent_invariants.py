"""
Agent v3 — invariants asserted before anything is sent. A violation is logged (``INVARIANT …``, WARNING) and repaired with a
deterministic fallback; none of them ever raises into the customer's turn.

1. **No ungrounded values** — a colour / size / material / quantity / payment method / name / address that the plan wrote
   must come from the customer's message, from a button / number / option they tapped, or (line values) already be in the cart /
   have been chosen earlier in the conversation for the SAME product. Otherwise it is reverted and the engine asks instead
   (``enforce_grounding``).
2. **Ends with a question or CTA** — or is a terminal state (order placed, handed to a human) (``enforce_reply``).
3. **Pending order acknowledged once** — a new cart starting while an order is awaiting payment / verification gets one
   "Your order #X is being verified. Meanwhile …" (``acknowledge_pending_order``).
4. **Stage independent of pending orders** — an unpaid order never pins the conversation stage (``stage_to_mirror``); the turn trace shows both.
5. **No unverified amounts, no "not available" without options** (``enforce_reply``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.services import agent_executor as ex
from app.services import agent_text
from app.services import command_words as cw
from app.services.agent_cart import CartData, changes
from app.services.agent_catalog import Catalogue
from app.services.agent_renderer import Renderer, Reply

logger = logging.getLogger("app.services.agent_invariants")

TRUSTED_FAST_PATHS = frozenset({"button", "number", "option", "gate"})
_AMOUNT_RE = re.compile(r"₹\s?([\d,]+)")
_UNAVAILABLE_RE = re.compile(r"not available|isn't available|is not available|unavailable|sold out|doesn't come in|only \d+ of", re.IGNORECASE)
_CTA_START_RE = re.compile(r"^\s*(?:please|reply|send|tell|type|share|choose|tap|let me know|you can)\b", re.IGNORECASE)


@dataclass
class Violation:
    """One broken invariant and what was done about it."""

    code: str
    detail: str


# ── 1. grounding ─────────────────────────────────────────────────────────────

def _grounded(change, ctx: ex.ExecContext) -> bool:
    """Did the customer's message (or a tap) produce this value?"""
    if ctx.grounded_by in TRUSTED_FAST_PATHS:
        return True
    text, new, old = ctx.user_text or "", change.new, change.old
    if change.kind == "line" and change.sku and str(new).lower() in (ctx.known_values.get(change.sku.upper(), {}).get(change.field) or set()):
        return True       # already in the cart / chosen earlier for THIS product ("one more in pink, same size")
    if change.kind == "payment":
        return agent_text.value_in_text("payment", new, text)
    if change.kind == "customer":
        return agent_text.value_in_text(change.field, new, text)
    if change.field == "qty":
        nums = agent_text.numbers_in(text)
        return bool(nums) and (new in nums or sum(nums) == new or (old is not None and abs(new - old) in nums))
    if agent_text.value_in_text(change.field, new, text):
        return True
    # "yes" to a question that offered exactly this one option ("Shall I use Red?")
    options = (ctx.last_clarify or {}).get("options") or []
    return cw.is_yes(text) and len(options) == 1 and str(options[0]).strip().lower() == str(new).strip().lower()


def enforce_grounding(
    before: CartData, result: ex.ExecResult, ctx: ex.ExecContext, catalogue: Catalogue, client,
) -> list[Violation]:
    """
    Revert every value in ``result.cart`` the customer did not give. Returns the violations (empty = all grounded).

    After a revert the turn shows no summary and drops any clarify built before it; ``agent_executor.finish`` (run
    afterwards) asks for what is now missing.
    """
    violations: list[Violation] = []
    reverted_lines: set[str] = set()
    cart = result.cart
    for ch in changes(before, cart):
        if _grounded(ch, ctx):
            continue
        # A colour / size / material the plan wrote that neither the message nor a tapped option contains (add_line / update_line /
        # split_line alike) is the planner choosing for the customer — "option_not_from_user"; other values keep the general code.
        code = "option_not_from_user" if ch.kind == "line" and ch.field in ("color", "size", "material") else "ungrounded_value"
        violations.append(Violation(code, f"{ch.kind}.{ch.field}={ch.new!r} (message: {ctx.user_text[:60]!r})"))
        logger.warning("INVARIANT %s %s.%s=%r not in customer message %r — reverted", code, ch.kind, ch.field, ch.new, (ctx.user_text or "")[:80])
        if ch.kind == "line":
            line = cart.line(ch.line_id)
            if line is not None:
                line[ch.field] = ch.old
                reverted_lines.add(ch.line_id)
        elif ch.kind == "payment":
            cart.payment_method = ch.old
        elif ch.kind == "customer":
            if ch.old:
                cart.customer[ch.field] = ch.old
            else:
                cart.customer.pop(ch.field, None)
    # A split whose parts were reverted must not leave two EMPTY lines behind: the original line (with its quantity) comes back.
    for ap in [a for a in result.applied if a.kind == "split" and a.before]:
        part_ids = {p.get("line_id") for p in ap.parts}
        if part_ids & reverted_lines:
            index = next((i for i, ln in enumerate(cart.lines) if ln["line_id"] in part_ids), len(cart.lines))
            cart.lines = [ln for ln in cart.lines if ln["line_id"] not in part_ids]
            cart.lines.insert(min(index, len(cart.lines)), dict(ap.before))
            result.applied.remove(ap)
    if violations:
        result.applied = [
            a for a in result.applied
            if not ((a.line and a.line.get("line_id") in reverted_lines) or a.kind in ("customer", "payment"))
        ]
        result.effects.show_summary = False
        result.effects.clarify = None
    return violations


# ── 2 / 5. reply checks ──────────────────────────────────────────────────────

def ends_with_cta(reply: Reply) -> bool:
    """True when the reply asks something / invites a next step (buttons count), or is a terminal state."""
    if reply.terminal or reply.buttons or reply.list_rows:
        return True
    last = (reply.text or "").strip().split("\n\n")[-1]
    return "?" in last or "؟" in last or bool(_CTA_START_RE.match(last)) or is_template_question(last)


_QUESTION_KEYS = ("q_", "cta_")
_question_patterns: dict[int, re.Pattern] = {}


def is_template_question(paragraph: str) -> bool:
    """
    Is this paragraph one of the renderer's own question / invitation templates in ANY language ("Kripya apna poora address …",
    "કૃપા કરીને તમારું પૂરું …")? Those are written as requests, not with a "?", in every table.
    """
    from app.services import agent_renderer as r

    key = id(r._TABLES)
    pattern = _question_patterns.get(key)
    if pattern is None:
        alternatives = []
        for table in r._TABLES.values():
            for name, text in table.items():
                if name.startswith(_QUESTION_KEYS) or name.endswith("_cta"):
                    alternatives.append(re.sub(r"\\\{\w+\\\}", ".*", re.escape(text)))
        pattern = _question_patterns[key] = re.compile("^(?:" + "|".join(alternatives) + ")$", re.DOTALL)
    return bool(pattern.match((paragraph or "").strip()))


def unverified_amounts(reply: Reply) -> list[int]:
    """₹ amounts in the text that no template / database row produced this turn."""
    found = [int(m.replace(",", "")) for m in _AMOUNT_RE.findall(reply.text or "") if m.replace(",", "").isdigit()]
    return [a for a in found if a not in reply.amounts]


def enforce_reply(reply: Reply, *, renderer: Renderer, cart: CartData, catalogue: Catalogue, client) -> list[Violation]:
    """Fix a reply that breaks invariant 2 or 5 in place (deterministic fallbacks); returns what was wrong."""
    violations: list[Violation] = []
    bad = unverified_amounts(reply)
    if bad:
        violations.append(Violation("unverified_amount", str(bad)))
        logger.error("INVARIANT unverified_amount %s in reply — replaced by the deterministic fallback", bad)
        reply.text, reply.buttons, reply.list_rows, reply.images = "", None, None, None
        _fallback_question(reply, renderer, cart, catalogue, client)
    if not reply.terminal and _UNAVAILABLE_RE.search(reply.text or "") and not reply.lists_options:
        violations.append(Violation("unavailable_without_options", (reply.text or "")[:80]))
        logger.warning("INVARIANT unavailable_without_options — appending what IS available")
        like = catalogue.get(reply.unavailable_sku) if reply.unavailable_sku else None
        alts = renderer.alternatives({like.sku} if like else set(), like, same_category_only=True) if like is not None else []
        if alts:
            reply.append(renderer.t("alts_header") + "\n" + "\n".join(renderer.product_lines(alts)))
            renderer.product_list_widget(reply, alts)
            reply.append(renderer.t("alts_cta"))
        else:
            reply.append(renderer.t("cta_browse"))
        reply.lists_options = True
    if not ends_with_cta(reply):
        violations.append(Violation("no_cta", (reply.text or "")[-60:]))
        logger.warning("INVARIANT no_cta — reply did not end with a question: %r", (reply.text or "")[-60:])
        _fallback_question(reply, renderer, cart, catalogue, client)
    reply.amounts = renderer.amounts
    return violations


def _fallback_question(reply: Reply, renderer: Renderer, cart: CartData, catalogue: Catalogue, client) -> None:
    """Append the next open step as a question, or the generic browse prompt."""
    open_gaps = ex.gaps(cart, catalogue, client, is_whatsapp=renderer.is_whatsapp)
    parts: list[str] = [reply.text] if reply.text else []
    if open_gaps:
        g = open_gaps[0]
        renderer.clarify(ex.Clarify(slot=g.slot, options=g.options, line_id=g.line_id, sku=g.sku), reply, parts)
    else:
        parts.append(renderer.t("cta_browse"))
    reply.text = "\n\n".join(p for p in parts if p).strip()


# ── 3. pending order acknowledged once ───────────────────────────────────────

def acknowledge_pending_order(reply: Reply, before: CartData, after: CartData, pending_orders: list, renderer: Renderer) -> bool:
    """
    A cart that starts (0 → ≥1 lines) while an order is pending gets ONE acknowledgement per pending order:
    "Your order #X is being verified. Meanwhile …". Returns True when it was added (and remembered in ``after.state``).
    """
    if before.lines or not after.lines or not pending_orders or reply.terminal:
        return False
    acked = set(after.state.get("acked_orders", []))
    fresh = [o for o in pending_orders if o.order_number not in acked]
    if not fresh:
        return False
    order = fresh[0]
    key = "pending_review" if order.status == "payment_submitted" else "pending_pay"
    reply.text = f"{renderer.t(key, number=order.order_number)} {renderer.t('pending_meanwhile')}\n\n{reply.text}".strip()
    after.state["acked_orders"] = sorted(acked | {o.order_number for o in fresh})
    return True


# ── 4. stage ─────────────────────────────────────────────────────────────────

_MIRROR = {"collecting": "order_collection", "confirming": "awaiting_final_confirmation"}


def stage_to_mirror(
    cart: CartData, *, pending_orders: list, order_created: str | None, shown_something: bool, current: str | None,
    cart_stage: str | None = None,
) -> str | None:
    """
    The legacy ``Conversation.current_stage`` to write for this cart this turn, or None when it is already right.

    v3 stage → legacy: browsing → greeting / product_inquiry · collecting → order_collection · confirming →
    awaiting_final_confirmation · order placed → payment (UPI, awaiting money) / completed (COD, paid at once).

    The conversation stage is what the customer is doing NOW; an order that still waits for money is a separate fact
    (``pending_orders``, shown next to the stage in the turn trace). An old unpaid order therefore never pins the conversation to
    ``payment``: a customer who is browsing or filling a new cart is ``greeting`` / ``product_inquiry`` / ``order_collection``.
    """
    if order_created:
        return "payment" if order_created == "UPI" else "completed"
    stage = _MIRROR.get(cart_stage or cart.stage)
    if stage is None:
        if shown_something:
            stage = "product_inquiry"
        else:
            stage = current if current in ("greeting", "product_inquiry") else "greeting"
    return stage if stage != current else None
