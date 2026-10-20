"""
Agent v3 — the executor: validates every planner action against the database, applies the valid ones to the cart and
turns the rest into structured issues. **The planner proposes, the executor decides.**

Contract
--------
* ``Executor.run(cart, actions)`` mutates the (already copied) ``cart`` action by action. An action that fails validation
  changes nothing and yields an ``ActionIssue`` (code + message + the REAL options that would have worked).
* ``execute_with_repair`` drives the loop: plan → run → hand the *fixable* issues (planner mistakes: unknown SKU / line,
  invalid variant, quantity that does not add up, malformed action) back to the planner, at most
  ``agent_max_repairs`` times. Issues the planner cannot fix (sold out, quantity above stock, incomplete cart, invalid
  address …) are answered deterministically right away. Whatever is still unresolved ends in an ``ask_clarify`` built
  from real catalogue options (never an invented one); after ``agent_clarify_fail_limit`` unresolved clarifies on the same
  slot the conversation is handed to a human instead of asking a third time.
* Terminal actions are guarded: ``confirm_order`` only for the exact cart version whose summary is on screen, only after an
  explicit yes, only if nothing else in the plan changed the cart; ``cancel`` only with explicit wording (else it asks).

The executor returns *data* (``ExecResult``: applied changes, issues, ``Effects``); words are the renderer's job.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.config import get_settings
from app.schemas.agent_actions import (
    Action, ActionIssue, AddLine, AnswerQuestion, AskClarify, BrowseCatalog, Cancel, ConfirmOrder, HandoffHuman,
    PlannerOutput, RemoveLine, RequestConfirmation, SearchCatalog, SendImages, SetCustomer, SetPayment, ShowProduct,
    SplitLine, UpdateLine,
)
from app.services import agent_text, conversation_flow, order_recovery
from app.services.agent_cart import CartData
from app.services.agent_catalog import Catalogue, ProductView

logger = logging.getLogger("app.services.agent_executor")

# Planner mistakes — worth another planner round with the error attached.
FIXABLE_CODES = frozenset({
    "INVALID_ACTION", "INVALID_PLAN", "UNKNOWN_SKU", "UNKNOWN_LINE", "INVALID_VARIANT", "INVALID_VARIANT_ATTR", "QTY_MISMATCH",
})
LINE_ATTRS = ("color", "size", "material")
_MOBILE_RE = re.compile(r"^[6-9]\d{9}$")


# ── result types ─────────────────────────────────────────────────────────────

@dataclass
class ExecContext:
    """What the executor needs to know about the turn besides the cart."""

    user_text: str
    language: str
    sender_phone: str
    wamid: str | None = None
    button_id: str | None = None
    grounded_by: str | None = None       # 'button' | 'number' | 'option' | 'gate' | 'side_topic' — set by the fast paths
    cancel_confirmed: bool = False       # the customer answered Yes to "Cancel this order?"
    confirm_ok: bool = False             # the customer explicitly confirmed (button / yes word / confirm phrase)
    pending_orders: list = field(default_factory=list)   # orders awaiting payment / verification
    is_whatsapp: bool = True
    last_clarify: dict | None = None     # the question the customer is answering ({slot, options, line_id, sku})
    known_values: dict = field(default_factory=dict)     # {sku: {field: {values}}} already in the cart / chosen earlier
    unavailable_options: list = field(default_factory=list)   # [{sku, attr, value, qty}] the customer asked for that the product is not sold in
    not_in_cart: str | None = None       # "remove chair": the thing the customer named is not in the cart
    qty_confirmed: bool = False          # the customer confirmed an unusually large quantity ("Yes, 150") — the sanity check is skipped
    forced_issue: dict | None = None     # a fast path found a problem with the answer (quantity < 1): {code, line_id, sku, **details}


@dataclass
class Gap:
    """The next thing the order is waiting for."""

    slot: str                            # color | size | material | quantity | name | address | mobile | payment
    line_id: str | None = None
    sku: str | None = None
    options: list[str] = field(default_factory=list)


@dataclass
class Clarify:
    """One question for the customer, with options that are all real."""

    slot: str
    options: list[str] = field(default_factory=list)
    question: str | None = None          # planner wording (already sanitised) — optional
    line_id: str | None = None
    sku: str | None = None
    reason: str | None = None            # issue code that caused it (INVALID_VARIANT, QTY_EXCEEDS_STOCK, …)
    details: dict[str, Any] = field(default_factory=dict)
    from_planner: bool = False


@dataclass
class Applied:
    """One change that really happened (the renderer turns these into acknowledgement lines)."""

    kind: str                            # added | updated | removed | split | merged | customer | payment | dropped
    line: dict[str, Any] | None = None
    before: dict[str, Any] | None = None
    parts: list[dict[str, Any]] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)


@dataclass
class OrderResult:
    """A created order and how the customer pays."""

    order: Any
    method: str
    instruction: Any | None = None       # payment_verification_service.PaymentInstruction for UPI


@dataclass
class Effects:
    """What the turn produced besides cart changes — input for the renderer."""

    search: dict[str, Any] | None = None       # {"query", "skus"}
    browse: dict[str, Any] | None = None       # {"category", "skus", "categories"}
    product: str | None = None                 # SKU whose card to show
    images_for: str | None = None              # SKU whose photos to send (send_images)
    answers: list[str] = field(default_factory=list)
    clarify: Clarify | None = None
    show_summary: bool = False
    order: OrderResult | None = None
    cancel: str | None = None                  # asked | cart | order:<no> | blocked:<no> | nothing
    handoff: str | None = None                 # reason
    unavailable: list[str] = field(default_factory=list)   # names of cart lines dropped because the product vanished
    dynamic: bool = False                      # an answer is the seller's own free text (knowledge base): not covered by the template tables
    unavailable_options: list[dict[str, Any]] = field(default_factory=list)   # options asked for that the product does not come in
    not_in_cart: str | None = None             # a removal of something that is not in the cart: said so, nothing changed


@dataclass
class ExecResult:
    """Everything one turn's execution produced."""

    cart: CartData
    applied: list[Applied] = field(default_factory=list)
    issues: list[ActionIssue] = field(default_factory=list)
    effects: Effects = field(default_factory=Effects)
    mutated: bool = False
    repairs: int = 0
    plan_actions: list[str] = field(default_factory=list)     # action types, for the turn trace
    planner_failed: bool = False                               # the first planner call returned nothing usable
    fixes: list[str] = field(default_factory=list)             # planner output the engine corrected / dropped itself (logged as "fixes=")

    def issue_codes(self) -> list[str]:
        """Codes of the issues still standing, for logs."""
        return [i.code for i in self.issues]


@dataclass
class RepairRequest:
    """What the planner is told when it is asked to fix its plan."""

    cart: CartData
    applied: list[str]
    issues: list[ActionIssue]


@dataclass
class PlanOutcome:
    """The planner's answer for one round: the parsed plan plus per-action parse issues."""

    output: PlannerOutput
    parse_issues: list[ActionIssue] = field(default_factory=list)


PlanFn = Callable[["RepairRequest | None"], Awaitable["PlanOutcome | None"]]


# ── cart analysis (shared with the renderer) ─────────────────────────────────

def payment_options(client) -> list[str]:
    """Payment methods this shop really offers and can serve (UPI needs a UPI id)."""
    options: list[str] = []
    if getattr(client, "accepts_cod", False):
        options.append("COD")
    if getattr(client, "accepts_upi", True) and getattr(client, "upi_id", None):
        options.append("UPI")
    return options


def effective_payment(cart: CartData, client) -> str | None:
    """The customer's chosen method, or the shop's only method when it offers just one (a policy, not a guess)."""
    options = payment_options(client)
    if cart.payment_method in options:
        return cart.payment_method
    return options[0] if len(options) == 1 else None


def line_fixed(view: ProductView, line: dict[str, Any]) -> dict[str, str | None]:
    """The attributes of `line` that the product actually defines, as a `fixed` dict for catalogue queries."""
    return {a: line.get(a) for a in view.needed_attrs if line.get(a)}


def committed_elsewhere(cart: CartData, sku: str, fixed: dict[str, str | None], exclude_line_id: str | None) -> int:
    """Units of the same product already in OTHER lines that compete for the same stock as `fixed`."""
    total = 0
    for ln in cart.lines_for(sku):
        if ln["line_id"] == exclude_line_id or not ln.get("qty"):
            continue
        theirs = {a: ln.get(a) for a in LINE_ATTRS if ln.get(a)}
        if all(_eq(theirs[a], fixed[a]) for a in theirs if a in fixed and fixed[a]):
            total += int(ln["qty"])
    return total


def _eq(a: str | None, b: str | None) -> bool:
    """Case-insensitive equality."""
    return (a or "").strip().lower() == (b or "").strip().lower()


def focus_first(cart: CartData) -> list[dict[str, Any]]:
    """
    The cart's lines with the one the customer is working on FIRST (``state.focus_line_id`` = the line last added / changed), the
    rest in their original order — so the next question is about the item just added, and older unfinished lines wait their turn.
    """
    focus = cart.state.get("focus_line_id")
    return sorted(cart.lines, key=lambda ln: ln["line_id"] != focus)       # stable: only the focus line moves


def gaps(cart: CartData, catalogue: Catalogue, client, *, is_whatsapp: bool = True) -> list[Gap]:
    """
    Everything the order still needs, in the order to ask: each line's missing attribute / quantity, then the
    customer's name, address (and contact number off WhatsApp), then the payment method.
    """
    out: list[Gap] = []
    for ln in focus_first(cart):
        view = catalogue.get(ln["sku"])
        if view is None:
            continue
        fixed = line_fixed(view, ln)
        missing = next((a for a in view.needed_attrs if not ln.get(a)), None)
        if missing:
            out.append(Gap(missing, ln["line_id"], ln["sku"], view.values(missing, fixed)))
        elif not ln.get("qty"):
            out.append(Gap("quantity", ln["line_id"], ln["sku"]))
    if cart.lines:
        if not cart.customer.get("name"):
            out.append(Gap("name"))
        if not cart.customer.get("address"):
            out.append(Gap("address"))
        if not is_whatsapp and not cart.customer.get("mobile"):
            out.append(Gap("mobile"))
        options = payment_options(client)
        if options and effective_payment(cart, client) is None:      # no method configured at all: confirmation explains it
            out.append(Gap("payment", options=options))
    return out


def stock_problem(cart: CartData, catalogue: Catalogue) -> tuple[dict[str, Any], str, dict[str, Any]] | None:
    """First line whose chosen variant cannot be served in full: (line, code, details) — checked again at confirmation."""
    for ln in cart.lines:
        view = catalogue.get(ln["sku"])
        if view is None or not ln.get("qty"):
            continue
        fixed = line_fixed(view, ln)
        avail = view.available(fixed)
        if avail is None:
            continue
        left = avail - committed_elsewhere(cart, ln["sku"], fixed, ln["line_id"])
        if left <= 0:
            return ln, "OUT_OF_STOCK_VARIANT", {"available": 0}
        if ln["qty"] > left:
            return ln, "QTY_EXCEEDS_STOCK", {"available": left}
    return None


async def units_sold_by_sku(db, client_id: int) -> dict[str, int]:
    """Units sold per SKU (upper-case) from real orders: cancelled / failed orders and simulator test orders do not count."""
    from sqlalchemy import func, select

    from app.models.order import Order
    from app.models.order_line_item import OrderLineItem

    rows = await db.execute(
        select(OrderLineItem.product_sku, func.coalesce(func.sum(OrderLineItem.quantity), 0))
        .join(Order, Order.id == OrderLineItem.order_id)
        .where(Order.client_id == client_id, Order.is_sandbox.is_(False), Order.status.notin_(("cancelled", "failed", "expired")),
               OrderLineItem.product_sku.isnot(None))
        .group_by(OrderLineItem.product_sku)
    )
    return {str(sku).upper(): int(n) for sku, n in rows.all()}


# ── the executor ─────────────────────────────────────────────────────────────

class Executor:
    """Validates and applies one plan's actions to a working copy of the cart."""

    def __init__(self, db, client, conv, catalogue: Catalogue, ctx: ExecContext, result: ExecResult, origin: CartData) -> None:
        """Bind the turn's collaborators; `result` accumulates across repair rounds, `origin` is the cart as the turn began."""
        self.db, self.client, self.conv, self.catalogue, self.ctx, self.result = db, client, conv, catalogue, ctx, result
        self.cart = result.cart
        self.summary_was_on_screen = origin.summary_on_screen     # what the customer SAW, before this turn changed anything

    # ── dispatch ─────────────────────────────────────────────────────────────
    async def run(self, actions: list[Action]) -> list[ActionIssue]:
        """Execute `actions` in order; return this round's issues (the caller decides which stand)."""
        issues: list[ActionIssue] = []
        failed = False
        for i, action in enumerate(actions):
            self.result.plan_actions.append(action.type)
            if failed and action.type in ("confirm_order", "request_confirmation", "cancel"):
                issues.append(self._issue(i, action, "SKIPPED", "An earlier action in this plan failed, so this one was not run."))
                continue
            handler = getattr(self, f"_do_{action.type}")
            issue = await handler(i, action)
            if issue is not None:
                issues.append(issue)
                failed = True
        return issues

    @staticmethod
    def _issue(i: int, action: Action | str, code: str, message: str, options: list[str] | None = None, **details) -> ActionIssue:
        """Build an ActionIssue."""
        kind = action if isinstance(action, str) else action.type
        return ActionIssue(index=i, action_type=kind, code=code, message=message, valid_options=list(options or []), details=details)

    def _touch(self) -> None:
        """Mark the cart changed; a summary shown earlier no longer describes it."""
        self.result.mutated = True
        self.cart.invalidate_summary()

    # ── read-only actions ────────────────────────────────────────────────────
    async def _do_search_catalog(self, i: int, a: SearchCatalog) -> ActionIssue | None:
        """
        Search the catalogue. Colour / size / material words in the query ("red kurti", "xl lehenga") filter on in-stock variants;
        with no exact match the effect carries the closest real alternative. Zero hits is a normal outcome (no issue).
        """
        query = " ".join((a.query or "").split())
        found = self.catalogue.search_filtered(query, top_k=5) if query else {"views": [], "labels": {}, "filters": {}, "text": "", "closest": None}
        closest = found["closest"]
        empty_category = self._sold_out_category(query) if query else None
        self.result.effects.search = {
            "query": query, "skus": [] if empty_category else [v.sku for v in found["views"]], "filters": found["filters"],
            "text": found["text"], "labels": found["labels"], "category_empty": empty_category,
            "closest": {"attr": closest["attr"], "values": closest["values"], "skus": [v.sku for v in closest["views"]]} if closest and not empty_category else None,
        }
        corrected = self.catalogue.normalize_query(query).corrections if query else {}
        if corrected:
            logger.info("AGENT query corrected conv=%s %s", getattr(self.conv, "id", None), corrected)
        for v in found["views"] + (closest["views"] if closest else []):
            self.cart.remember("shown_skus", v.sku)
        return None

    def _sold_out_category(self, text: str) -> str | None:
        """The category `text` names when the shop HAS it but nothing in it is in stock (so the reply says so, honestly), else None."""
        for name in self.catalogue.find_categories(text):
            in_cat = self.catalogue.in_category(name)
            if in_cat and not any(v.in_stock() for v in in_cat):
                return name
        return None

    async def _do_browse_catalog(self, i: int, a: BrowseCatalog) -> ActionIssue | None:
        """
        Show a category (or the shop in general).

        A category word is resolved spelling-tolerantly ("lahemgha" → Lehenga, "suit" → every suit category). A category that exists
        but is sold out, and a word that is no category and no product at all, are answered as exactly that — never by silently
        listing the first products of the shop.
        """
        category = " ".join((a.category or "").split())
        views: list[ProductView] = []
        empty_category = unknown_category = None
        if category:
            in_cat = self.catalogue.in_category(category)
            views = [v for v in in_cat if v.in_stock()]
            if in_cat and not views:
                empty_category = in_cat[0].product.category
            elif not in_cat:
                views = [v for v in self.catalogue.search_filtered(category, top_k=5)["views"] if v.in_stock()]
                unknown_category = None if views else category
                if views:
                    category = ""
        else:
            views = [v for v in self.catalogue.views if v.in_stock()]
        sort = None
        if a.sort == "bestseller":
            sold = await units_sold_by_sku(self.db, self.client.id)
            ranked = [v for v in views if sold.get((v.sku or "").upper(), 0) > 0]
            if ranked:                  # a shop without sales never claims to have best sellers: it just shows what it has
                ranked.sort(key=lambda v: -sold[v.sku.upper()])      # stable: ties keep catalogue order
                views, sort = ranked, "bestseller"
        views = views[:5]
        self.result.effects.browse = {
            "category": category if category and views else None, "skus": [v.sku for v in views],
            "categories": self.catalogue.categories(), "sort": sort,
            "category_empty": empty_category, "unknown_category": unknown_category,
        }
        for v in views:
            self.cart.remember("shown_skus", v.sku)
        return None

    async def _do_show_product(self, i: int, a: ShowProduct) -> ActionIssue | None:
        """Show one product's card."""
        view = self.catalogue.get(a.sku)
        if view is None:
            return self._unknown_sku(i, a, a.sku)
        self.result.effects.product = view.sku
        self.cart.state["focus_sku"] = view.sku
        self.cart.remember("viewed_skus", view.sku)
        self.cart.remember("shown_skus", view.sku)
        return None

    async def _do_send_images(self, i: int, a: SendImages) -> ActionIssue | None:
        """Mark a product's photos to be sent (the orchestrator resolves and watermarks the URLs)."""
        view = self.catalogue.get(a.sku)
        if view is None:
            return self._unknown_sku(i, a, a.sku)
        self.result.effects.images_for = view.sku
        self.cart.state["focus_sku"] = view.sku
        self.cart.remember("viewed_skus", view.sku)
        return None

    def _unknown_sku(self, i: int, a: Action, sku: str) -> ActionIssue:
        """UNKNOWN_SKU with real products the planner can choose from."""
        real = [f"{v.sku} ({v.name})" for v in self.catalogue.views if v.sku][:12]
        return self._issue(i, a, "UNKNOWN_SKU", f"No product with SKU {sku!r}. Use only SKUs from CATALOGUE.", real)

    # ── cart actions ─────────────────────────────────────────────────────────
    def _resolve_given(self, i: int, a: Action, view: ProductView, given: dict[str, str]) -> tuple[dict[str, str], ActionIssue | None]:
        """Map the planner's attribute values to catalogue spellings, or explain what is valid."""
        resolved: dict[str, str] = {}
        for attr, text in given.items():
            if attr not in view.needed_attrs:
                return {}, self._issue(
                    i, a, "INVALID_VARIANT_ATTR", f"{view.name} has no {attr} options.", view.needed_attrs, attr=attr,
                    product=view.name,
                )
            candidates = view.resolve(attr, text)
            if not candidates:
                return {}, self._issue(
                    i, a, "INVALID_VARIANT", f"{view.name} does not come in {attr} {text!r}.",
                    view.values(attr), attr=attr, value=text, product=view.name, sku=view.sku,
                    all_values=view.values(attr, in_stock_only=False),
                )
            if len(candidates) > 1:
                return {}, self._issue(
                    i, a, "AMBIGUOUS_VARIANT", f"{text!r} matches several {attr} values.", candidates, attr=attr, value=text,
                    product=view.name, sku=view.sku,
                )
            resolved[attr] = candidates[0]
        return resolved, None

    def _check_stock(
        self, i: int, a: Action, view: ProductView, fixed: dict[str, str | None], qty: int | None, exclude_line_id: str | None,
    ) -> ActionIssue | None:
        """The (partial) variant must have stock, and `qty` must fit after the cart's other lines."""
        avail = view.available(fixed)
        if avail is None:
            return None
        left = avail - committed_elsewhere(self.cart, view.sku, fixed, exclude_line_id)
        if left <= 0:
            return self._out_of_stock_issue(i, a, view, fixed)
        if qty is not None and qty > left:
            return self._issue(
                i, a, "QTY_EXCEEDS_STOCK", f"Only {left} available for {view.name}.", [], available=left,
                product=view.name, sku=view.sku, requested=qty, line_id=exclude_line_id, **self._fixed_details(fixed),
            )
        return None

    @staticmethod
    def _fixed_details(fixed: dict[str, str | None]) -> dict[str, Any]:
        """Variant attributes for an issue's details."""
        return {f"variant_{k}": v for k, v in fixed.items() if v}

    def _out_of_stock_issue(self, i: int, a: Action, view: ProductView, fixed: dict[str, str | None]) -> ActionIssue:
        """
        OUT_OF_STOCK(_VARIANT) listing what IS available.

        One attribute fixed (a colour): the other in-stock values of it. Several fixed (colour AND size): the in-stock
        combinations that share one of the requested values ("Pink / M", "Blue / XXL") — the customer can pick one of those
        as it is. Nothing in stock at all: OUT_OF_STOCK (the renderer then suggests other products).
        """
        if not view.in_stock():
            return self._issue(i, a, "OUT_OF_STOCK", f"{view.name} is sold out.", [], product=view.name, sku=view.sku)
        given = {k: v for k, v in fixed.items() if v}
        label = " / ".join(given.values())
        if len(given) >= 2:
            combos: list[dict[str, str]] = []
            for v in view.variants:
                if v.available is not None and v.available <= 0:
                    continue
                shared = sum(1 for k, val in given.items() if v.attr(k) and _eq(v.attr(k), val))
                if shared >= 1 and not all(_eq(v.attr(k), val) for k, val in given.items()):
                    combos.append({a_: v.attr(a_) for a_ in view.needed_attrs if v.attr(a_)})
            if combos:
                combos = combos[:6]
                return self._issue(
                    i, a, "OUT_OF_STOCK_VARIANT", f"{view.name} in {label} is sold out.",
                    [" / ".join(c.values()) for c in combos], attr="variant", value=label, product=view.name, sku=view.sku,
                    combos=combos,
                )
        for attr in LINE_ATTRS:
            if not given.get(attr):
                continue
            others = {k: v for k, v in given.items() if k != attr}
            alternatives = [v for v in view.values(attr, others) if not _eq(v, given[attr])]
            if alternatives:
                return self._issue(
                    i, a, "OUT_OF_STOCK_VARIANT", f"{view.name} in {given[attr]} is sold out.", alternatives,
                    attr=attr, value=given[attr], product=view.name, sku=view.sku, **self._fixed_details(others),
                )
        return self._issue(i, a, "OUT_OF_STOCK", f"{view.name} is sold out in that combination.", [], product=view.name, sku=view.sku)

    async def _do_add_line(self, i: int, a: AddLine) -> ActionIssue | None:
        """Put a product (and whatever variant / quantity the customer gave) in the cart."""
        view = self.catalogue.get(a.sku)
        if view is None:
            return self._unknown_sku(i, a, a.sku)
        if not view.in_stock():
            return self._out_of_stock_issue(i, a, view, {})
        given, issue = self._resolve_given(i, a, view, a.variant.given())
        if issue:
            return issue
        target = self._mergeable_line(view.sku, given)
        fixed = {**(line_fixed(view, target) if target else {}), **given}
        stock_issue = self._check_stock(i, a, view, fixed, a.qty, target["line_id"] if target else None)
        if stock_issue:
            return stock_issue
        big_qty = a.qty if (a.qty is not None and a.qty > get_settings().agent_qty_sane_max and not self.ctx.qty_confirmed) else None
        if big_qty is not None:
            a = a.model_copy(update={"qty": None})         # the line is created / extended WITHOUT the unusual quantity; the customer confirms it next
        self.cart.remember("viewed_skus", view.sku)
        self.cart.state["focus_sku"] = view.sku
        if target is not None:
            self.cart.state["focus_line_id"] = target["line_id"]
            # Re-adding a variant that is already in the cart SETS its quantity (idempotent — a repeated plan must not
            # double it); to add pieces the planner uses update_line.
            before = dict(target)
            target.update(given)
            if a.qty is not None:
                target["qty"] = a.qty
            if target != before:
                self._touch()
                self.result.applied.append(Applied("updated", dict(target), before, fields=[k for k in target if target[k] != before.get(k)]))
            if big_qty is not None:
                return self._issue(i, a, "QTY_CONFIRM", f"{big_qty} is a lot — confirm.", [], line_id=target["line_id"], sku=view.sku, requested=big_qty, product=view.name)
            return None
        ln = self.cart.add_line(view.sku, qty=a.qty, **given)
        self.cart.state["focus_line_id"] = ln["line_id"]
        self._touch()
        self.result.applied.append(Applied("added", dict(ln)))
        if big_qty is not None:
            return self._issue(i, a, "QTY_CONFIRM", f"{big_qty} is a lot — confirm.", [], line_id=ln["line_id"], sku=view.sku, requested=big_qty, product=view.name)
        return None

    def _mergeable_line(self, sku: str, given: dict[str, str]) -> dict[str, Any] | None:
        """An existing line of `sku` that the new values extend without conflict (empty lines first), else None."""
        best = None
        for ln in self.cart.lines_for(sku):
            if any(ln.get(a) and not _eq(ln[a], v) for a, v in given.items()):
                continue
            incomplete = any(not ln.get(a) for a in LINE_ATTRS) or not ln.get("qty")
            identical = all(_eq(ln.get(a), v) for a, v in given.items())
            if incomplete or identical:
                if best is None or (incomplete and not any(ln.get(a) for a in LINE_ATTRS)):
                    best = ln
        return best

    async def _do_update_line(self, i: int, a: UpdateLine) -> ActionIssue | None:
        """Change colour / size / material / quantity of one line."""
        ln = self.cart.line(a.line_id)
        if ln is None:
            return self._unknown_line(i, a, a.line_id)
        view = self.catalogue.get(ln["sku"])
        if view is None:
            return self._unknown_sku(i, a, ln["sku"])
        wanted = a.fields.given()
        qty = wanted.pop("qty", None)
        given, issue = self._resolve_given(i, a, view, {k: v for k, v in wanted.items() if k in LINE_ATTRS})
        if issue:
            return issue
        fixed = {**line_fixed(view, ln), **given}
        new_qty = qty if qty is not None else ln.get("qty")
        stock_issue = self._check_stock(i, a, view, fixed, new_qty, ln["line_id"])
        if stock_issue:
            return stock_issue
        if qty is not None and qty > get_settings().agent_qty_sane_max and not self.ctx.qty_confirmed:
            return self._issue(i, a, "QTY_CONFIRM", f"{qty} is a lot — confirm.", [], line_id=ln["line_id"], sku=view.sku, requested=qty, product=view.name)
        before = dict(ln)
        ln.update(given)
        if qty is not None:
            ln["qty"] = qty
        self.cart.state["focus_line_id"] = ln["line_id"]
        if ln != before:
            self._touch()
            self.result.applied.append(Applied("updated", dict(ln), before, fields=[k for k in ln if ln[k] != before.get(k)]))
        return None

    def _unknown_line(self, i: int, a: Action, line_id: str) -> ActionIssue:
        """UNKNOWN_LINE listing the real line ids."""
        return self._issue(i, a, "UNKNOWN_LINE", f"No cart line {line_id!r}.", [ln["line_id"] for ln in self.cart.lines])

    async def _do_remove_line(self, i: int, a: RemoveLine) -> ActionIssue | None:
        """
        Drop one line (``line_id``) or every line of a product (``sku``); the product counts as abandoned for upsell purposes.

        A ``sku`` that is not in the cart, or a ``line_id`` that does not exist, is UNKNOWN_LINE listing the real line ids.
        """
        if a.line_id:
            ln = self.cart.line(a.line_id)
            targets = [ln] if ln is not None else []
        else:
            targets = self.cart.lines_for(a.sku or "")
        if not targets:
            return self._unknown_line(i, a, a.line_id or a.sku or "")
        for ln in targets:
            self.cart.lines.remove(ln)
            self.cart.remember("removed_skus", ln["sku"])
            if self.cart.state.get("focus_line_id") == ln["line_id"]:
                self.cart.state.pop("focus_line_id", None)
            self.result.applied.append(Applied("removed", dict(ln)))
        self._touch()
        return None

    async def _do_split_line(self, i: int, a: SplitLine) -> ActionIssue | None:
        """Replace one line by several (e.g. 3 → 2 Red + 1 Green); the parts must add up to the line's quantity."""
        ln = self.cart.line(a.line_id)
        if ln is None:
            return self._unknown_line(i, a, a.line_id)
        view = self.catalogue.get(ln["sku"])
        if view is None:
            return self._unknown_sku(i, a, ln["sku"])
        total = sum(p.qty for p in a.parts)
        if ln.get("qty") and total != ln["qty"]:
            return self._issue(
                i, a, "QTY_MISMATCH", f"The parts add up to {total} but the line has quantity {ln['qty']}.", [],
                expected=ln["qty"], got=total, product=view.name, sku=view.sku,
            )
        merged: list[dict[str, Any]] = []
        probe = self.cart.copy()
        probe.lines = [x for x in probe.lines if x["line_id"] != ln["line_id"]]
        for part in a.parts:
            given, issue = self._resolve_given(i, a, view, part.variant.given())
            if issue:
                return issue
            same = next((m for m in merged if all(_eq(m.get(k), given.get(k)) for k in LINE_ATTRS)), None)
            if same is not None:
                same["qty"] += part.qty
            else:
                merged.append({**{k: None for k in LINE_ATTRS}, **given, "qty": part.qty})
        for part in merged:
            stock_issue = self._check_stock_on(probe, i, a, view, {k: part[k] for k in LINE_ATTRS if part.get(k)}, part["qty"])
            if stock_issue:
                return stock_issue
            probe.lines.append({"line_id": "probe", "sku": view.sku, **part})
        index = self.cart.lines.index(ln)
        self.cart.lines.remove(ln)
        new_lines = []
        for offset, part in enumerate(merged):
            new_line = {"line_id": _new_id(), "sku": view.sku, **part}
            self.cart.lines.insert(index + offset, new_line)
            new_lines.append(dict(new_line))
        if new_lines:
            self.cart.state["focus_line_id"] = new_lines[0]["line_id"]
        self._touch()
        self.result.applied.append(Applied("split", before=dict(ln), parts=new_lines))
        return None

    def _check_stock_on(self, cart: CartData, i: int, a: Action, view: ProductView, fixed: dict, qty: int) -> ActionIssue | None:
        """`_check_stock` against a scratch cart (used while a split is being assembled)."""
        saved, self.cart = self.cart, cart
        try:
            return self._check_stock(i, a, view, fixed, qty, None)
        finally:
            self.cart = saved

    # ── customer / payment ───────────────────────────────────────────────────
    async def _do_set_customer(self, i: int, a: SetCustomer) -> ActionIssue | None:
        """Store name / address / mobile exactly as given, after the same checks the legacy flow applies."""
        changed: list[str] = []
        if a.name:
            name = " ".join(a.name.split())
            if len(name) < 2 or len(name) > 60 or sum(ch.isalpha() for ch in name) < 2 or "?" in name:
                return self._issue(i, a, "INVALID_NAME", "That doesn't look like a name.", [])
            if name != self.cart.customer.get("name"):
                self.cart.customer["name"] = name
                changed.append("name")
        if a.address:
            address = " ".join(a.address.split())
            if not conversation_flow.is_valid_address(address):
                return self._issue(
                    i, a, "INVALID_ADDRESS", "The address looks incomplete.", [],
                    reason=conversation_flow.classify_address_rejection(address),
                )
            if address != self.cart.customer.get("address"):
                self.cart.customer["address"] = address
                changed.append("address")
        if a.mobile:
            digits = re.sub(r"\D", "", a.mobile)[-10:]
            if not _MOBILE_RE.match(digits):
                return self._issue(i, a, "INVALID_MOBILE", "That doesn't look like a 10-digit mobile number.", [])
            if digits != self.cart.customer.get("mobile"):
                self.cart.customer["mobile"] = digits
                changed.append("mobile")
        if changed:
            self._touch()
            self.result.applied.append(Applied("customer", fields=changed))
        return None

    async def _do_set_payment(self, i: int, a: SetPayment) -> ActionIssue | None:
        """Record COD / UPI if the shop offers it."""
        options = payment_options(self.client)
        if a.method not in options:
            return self._issue(i, a, "PAYMENT_NOT_ACCEPTED", f"{a.method} is not available here.", options, method=a.method)
        if self.cart.payment_method != a.method:
            self.cart.payment_method = a.method
            self._touch()
            self.result.applied.append(Applied("payment", fields=["payment_method"]))
        return None

    # ── conversation actions ─────────────────────────────────────────────────
    async def _do_ask_clarify(self, i: int, a: AskClarify) -> ActionIssue | None:
        """Ask a question — with the options replaced by REAL ones whenever the slot is something the catalogue knows."""
        slot = a.slot or "other"
        question = agent_text.sanitize_question(a.question)
        open_gaps = gaps(self.cart, self.catalogue, self.client, is_whatsapp=self.ctx.is_whatsapp)
        gap = next((g for g in open_gaps if g.slot == slot), None)
        dropped = self._pointless_clarify(slot, gap)
        if dropped:
            # The planner asked for something the engine already knows (set, or the shop's only option). Dropping it lets the
            # deterministic next-step logic (autofill → next open gap → summary) decide; logged as a repair.
            self.result.fixes.append(dropped)
            logger.info("AGENT repair conv=%s dropped ask_clarify: %s", getattr(self.conv, "id", None), dropped)
            return None
        options: list[str] = []
        details: dict[str, Any] = {}
        line_id = sku = None
        if gap is not None:
            options, line_id, sku = list(gap.options), gap.line_id, gap.sku
        elif slot in ("color", "size", "material"):
            focus = self.catalogue.get(self.cart.state.get("focus_sku") or (self.cart.lines[-1]["sku"] if self.cart.lines else None))
            if focus is not None:
                options = focus.values(slot)
                sku = focus.sku
        elif slot == "payment":
            options = payment_options(self.client)
        elif slot == "product":
            real = {v.sku.upper() for v in self.catalogue.views}
            options = [o for o in a.options if o.upper() in real][:8]
        elif slot == "split":
            line = next((ln for ln in self.cart.lines if (ln.get("qty") or 0) >= 2), self.cart.lines[-1] if self.cart.lines else None)
            if line is not None:
                line_id, sku = line["line_id"], line["sku"]
                details["expected"] = line.get("qty")
                view = self.catalogue.get(sku)
                if view is not None:        # the options the CUSTOMER named — the only values a question may ever quote back
                    named = [v for a in view.needed_attrs for v in view.values(a) if agent_text.value_in_text(a, v, self.ctx.user_text)]
                    if named:
                        details["named"] = named
        elif slot == "cancel":
            options = []
        elif slot == "remove":
            wanted = [o for o in a.options if self.cart.line(o) is not None]
            options = wanted or [ln["line_id"] for ln in self.cart.lines]
        if slot in ("color", "size", "material", "payment"):
            keep = [o for o in a.options if any(_eq(o, r) for r in options)]
            options = keep or options            # an invented option is dropped; the real list takes its place
        self.result.effects.clarify = Clarify(
            slot=slot, options=options[:10], question=question, line_id=line_id, sku=sku, from_planner=True, details=details,
        )
        return None

    def _pointless_clarify(self, slot: str, gap: Gap | None) -> str | None:
        """
        Why a planner ``ask_clarify`` must not be shown, or None when it may be.

        * the slot has exactly ONE available value (``gap.options`` / the product's only value) → the engine selects it
          (``autofill_single_options``), there is nothing to choose;
        * the slot is already set on every line, the customer's message does not talk about it, and this is not a repair round (a
          repair round is the planner re-asking about a value the customer DID name) → a stray question.
        A cart with no lines keeps the planner's question (it may be a product-level "which colour does it come in?"), and so does
        "I want to change the colour" (the message mentions the slot).
        """
        if slot not in ("color", "size", "material", "quantity"):
            return None
        if gap is not None:
            return f"slot={slot} has the single option {gap.options[0]!r} (auto-selected)" if slot != "quantity" and len(gap.options) == 1 else None
        if not self.cart.lines or self.result.repairs > 0:
            return None
        if slot != "quantity":
            focus = self.catalogue.get(self.cart.state.get("focus_sku") or self.cart.lines[-1]["sku"])
            values = focus.values(slot) if focus is not None else []
            if len(values) == 1:
                return f"slot={slot} has the single option {values[0]!r} and it is already set"
        if agent_text.mentions_slot(self.ctx.user_text, slot):
            return None
        return f"slot={slot} already set on every line and the customer did not mention it"

    async def _do_request_confirmation(self, i: int, a: RequestConfirmation) -> ActionIssue | None:
        """Show the summary — only when nothing is missing."""
        missing = gaps(self.cart, self.catalogue, self.client, is_whatsapp=self.ctx.is_whatsapp)
        if not self.cart.lines:
            return self._issue(i, a, "INCOMPLETE_CART", "The cart is empty.", [], missing=["items"])
        if missing:
            return self._issue(i, a, "INCOMPLETE_CART", "The order is not complete yet.", [], missing=[g.slot for g in missing])
        problem = stock_problem(self.cart, self.catalogue)
        if problem is not None:
            ln, code, details = problem
            return self._stock_issue_for_line(i, a, ln, code, details)
        self.result.effects.show_summary = True
        return None

    def _stock_issue_for_line(self, i: int, a: Action, ln: dict, code: str, details: dict) -> ActionIssue:
        """OUT_OF_STOCK_VARIANT / QTY_EXCEEDS_STOCK for a line that no longer fits."""
        view = self.catalogue.get(ln["sku"])
        fixed = line_fixed(view, ln)
        if code == "OUT_OF_STOCK_VARIANT":
            issue = self._out_of_stock_issue(i, a, view, fixed)
        else:
            issue = self._issue(
                i, a, "QTY_EXCEEDS_STOCK", f"Only {details['available']} available for {view.name}.", [],
                available=details["available"], product=view.name, sku=view.sku, requested=ln["qty"], **self._fixed_details(fixed),
            )
        issue.details["line_id"] = ln["line_id"]
        return issue

    async def _do_confirm_order(self, i: int, a: ConfirmOrder) -> ActionIssue | None:
        """Place the order — only for the exact cart version whose summary is on screen, after an explicit yes."""
        if not self.ctx.confirm_ok:
            return self._issue(i, a, "NOT_CONFIRMABLE", "The customer has not explicitly confirmed.", [], why="no_explicit_yes")
        if not self.summary_was_on_screen or self.result.mutated:
            return self._issue(i, a, "NOT_CONFIRMABLE", "The summary on screen is not for this cart.", [], why="stale_summary")
        missing = gaps(self.cart, self.catalogue, self.client, is_whatsapp=self.ctx.is_whatsapp)
        if missing:
            return self._issue(i, a, "INCOMPLETE_CART", "The order is not complete yet.", [], missing=[g.slot for g in missing])
        problem = stock_problem(self.cart, self.catalogue)
        if problem is not None:
            return self._stock_issue_for_line(i, a, *problem)
        method = effective_payment(self.cart, self.client)
        if method is None or (method == "UPI" and not getattr(self.client, "upi_id", None)):
            return self._issue(i, a, "UPI_NOT_CONFIGURED", "No payment method can be served.", [])
        order = await self._create_order(method)
        self.result.effects.order = order
        self.result.mutated = True
        return None

    async def _create_order(self, method: str) -> OrderResult:
        """Write the order (+ reservation for UPI / paid status for COD) and build the payment instruction."""
        from app.services import customer_service, order_service, payment_verification_service as pvs

        items = []
        for ln in self.cart.lines:
            view = self.catalogue.get(ln["sku"])
            fixed = line_fixed(view, ln)
            items.append({
                "product_id": view.product.id, "product_name": view.name, "product_sku": view.sku,
                "variant_color": ln.get("color"), "variant_size": ln.get("size"), "variant_material": ln.get("material"),
                "quantity": int(ln["qty"]), "unit_price": view.unit_price(fixed),
            })
        customer = self.cart.customer
        order = await order_service.create_cart_order(
            db=self.db, client_id=self.client.id, conversation_id=self.conv.id,
            customer_name=customer["name"], customer_phone=self.ctx.sender_phone, delivery_address=customer["address"],
            mobile_number=customer.get("mobile"), line_items=items, payment_method=method, idempotency_key=self.ctx.wamid,
        )
        try:
            await customer_service.record_order(
                self.db, client_id=self.client.id, phone=self.ctx.sender_phone, order_total=order.total_amount,
                customer_name=customer["name"], delivery_address=customer["address"], payment_method=order.payment_method,
            )
            await self.db.commit()
        except Exception as exc:  # noqa: BLE001 — profile stats must never block the order
            logger.warning("customer stats update failed conv=%s: %s", self.conv.id, exc)
        instruction = None
        if method == "UPI":
            instruction = await pvs.build_payment_instruction(self.db, order, self.client, self.conv)
        else:
            try:
                await order_service.mark_order_paid(self.db, order, self.client)
            except Exception as exc:  # noqa: BLE001
                logger.warning("mark_order_paid (COD) failed conv=%s: %s", self.conv.id, exc)
        logger.info("AGENT order created conv=%s order=%s method=%s lines=%d", self.conv.id, order.order_number, method, len(items))
        return OrderResult(order=order, method=method, instruction=instruction)

    async def _do_cancel(self, i: int, a: Cancel) -> ActionIssue | None:
        """Drop the cart (or the unpaid order) — explicit wording only, otherwise ask first."""
        from app.services import payment_verification_service as pvs

        explicit = (
            self.ctx.cancel_confirmed or self.ctx.button_id == "v3_cancel"
            or order_recovery.has_explicit_cancel_keyword(self.ctx.user_text)
        )
        effects = self.result.effects
        unpaid = [o for o in self.ctx.pending_orders if o.status == "pending_payment"]
        under_review = [o for o in self.ctx.pending_orders if o.status == "payment_submitted"]
        if self.cart.lines:
            if not explicit:
                effects.clarify = Clarify(slot="cancel", options=["Yes, cancel", "No, keep it"], from_planner=False)
                effects.cancel = "asked"
                return None
            effects.cancel = "cart"
            self.result.mutated = True
            return None
        if unpaid:
            if not explicit:
                effects.clarify = Clarify(slot="cancel", options=["Yes, cancel", "No, keep it"], reason="ORDER", from_planner=False)
                effects.cancel = "asked"
                return None
            await pvs.cancel_by_customer(self.db, unpaid[0])
            effects.cancel = f"order:{unpaid[0].order_number}"
            return None
        if under_review:
            effects.cancel = f"blocked:{under_review[0].order_number}"
            return None
        effects.cancel = "nothing"
        return None

    async def _do_handoff_human(self, i: int, a: HandoffHuman) -> ActionIssue | None:
        """Pause the bot and flag the conversation for a person."""
        await pause_for_handoff(self.db, self.client, self.conv, a.reason or "customer asked")
        self.result.effects.handoff = a.reason or "customer asked"
        return None

    async def _do_answer_question(self, i: int, a: AnswerQuestion) -> ActionIssue | None:
        """Answer from the shop's own facts (side_questions) — never from the model."""
        from app.services import order_pipeline as op, side_questions

        focus = self.catalogue.get(self.cart.state.get("focus_sku") or (self.cart.lines[0]["sku"] if self.cart.lines else None))
        pinned = focus.product if focus is not None else None
        if a.topic == "order_status":
            text, _found = await op._build_order_status_reply(self.db, self.conv, self.client, self.ctx.sender_phone, self.ctx.user_text, self.ctx.language)
        else:
            info: dict = {}
            text = await side_questions.build_answer(self.db, self.client, self.conv, pinned, a.topic, self.ctx.language, self.ctx.user_text, info=info)
            if info.get("seller_text"):
                self.result.effects.dynamic = True
        if text:
            self.result.effects.answers.append(text)
        return None


async def pause_for_handoff(db, client, conv, reason: str) -> None:
    """Pause the bot for this conversation (source ``escalation``) and count the escalation. Never raises."""
    from datetime import datetime, timezone

    from app.services import conversation_control

    try:
        conv.escalation_count = (conv.escalation_count or 0) + 1
        conv.last_escalation_at = datetime.now(timezone.utc)
        await conversation_control.pause_bot(db, client, conv, source="escalation", note=f"Agent v3 handoff: {reason}"[:200])
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("agent handoff pause failed conv=%s: %s", conv.id, exc)


def _new_id() -> str:
    """A fresh line id (same format as ``CartData.add_line``)."""
    from app.services.agent_cart import new_line_id

    return new_line_id()


# ── pruning / fallbacks ──────────────────────────────────────────────────────

def prune_unavailable(cart: CartData, catalogue: Catalogue, result: ExecResult) -> None:
    """Drop cart lines whose product was deleted / deactivated since they were added; remember their names for the reply."""
    for ln in list(cart.lines):
        if catalogue.get(ln["sku"]) is None:
            cart.lines.remove(ln)
            cart.remember("removed_skus", ln["sku"])
            result.effects.unavailable.append(ln["sku"])
            result.applied.append(Applied("dropped", dict(ln)))
            result.mutated = True
            cart.invalidate_summary()


def clarify_for_issue(issue: ActionIssue, cart: CartData, catalogue: Catalogue, client, *, is_whatsapp: bool = True) -> Clarify:
    """
    The deterministic clarify for an unresolved issue — every option comes from the catalogue / shop settings.

    Never returns an empty explanation: "not available" always travels with what IS available (``options``), or with the
    ``alternatives`` flag so the renderer lists similar in-stock products.
    """
    d = issue.details
    code = issue.code
    if code == "OUT_OF_STOCK_VARIANT" and d.get("attr") == "variant":
        return Clarify(slot="variant", options=list(issue.valid_options), reason=code, details=dict(d), sku=d.get("sku"), line_id=d.get("line_id"))
    if code in ("INVALID_VARIANT", "AMBIGUOUS_VARIANT", "OUT_OF_STOCK_VARIANT") and d.get("attr"):
        return Clarify(slot=d["attr"], options=list(issue.valid_options), reason=code, details=dict(d), sku=d.get("sku"), line_id=d.get("line_id"))
    if code == "INVALID_VARIANT_ATTR":
        gap = next(iter(gaps(cart, catalogue, client, is_whatsapp=is_whatsapp)), None)
        return Clarify(slot=gap.slot if gap else "other", options=gap.options if gap else [], reason=code, details=dict(d))
    if code == "QTY_EXCEEDS_STOCK":
        return Clarify(slot="quantity", reason=code, details=dict(d), sku=d.get("sku"), line_id=d.get("line_id"))
    if code in ("QTY_MIN", "QTY_CONFIRM"):
        return Clarify(slot="qty_confirm" if code == "QTY_CONFIRM" else "quantity", reason=code, details=dict(d), sku=d.get("sku"), line_id=d.get("line_id"))
    if code == "QTY_MISMATCH":
        return Clarify(slot="split", reason=code, details=dict(d), sku=d.get("sku"))
    if code in ("OUT_OF_STOCK", "UNKNOWN_SKU"):
        return Clarify(slot="product", options=[], reason=code, details=dict(d), sku=d.get("sku"))
    if code == "INVALID_NAME":
        return Clarify(slot="name", reason=code)
    if code == "INVALID_ADDRESS":
        return Clarify(slot="address", reason=code, details=dict(d))
    if code == "INVALID_MOBILE":
        return Clarify(slot="mobile", reason=code)
    if code in ("PAYMENT_NOT_ACCEPTED", "UPI_NOT_CONFIGURED"):
        return Clarify(slot="payment", options=payment_options(client), reason=code, details=dict(d))
    missing = gaps(cart, catalogue, client, is_whatsapp=is_whatsapp)
    if missing:
        g = missing[0]
        return Clarify(slot=g.slot, options=g.options, line_id=g.line_id, sku=g.sku, reason=code)
    return Clarify(slot="other", reason=code)


_ATTEMPT_ACTIONS = frozenset({"add_line", "update_line", "remove_line", "split_line", "set_customer", "set_payment", "ask_clarify"})


def register_clarify(cart: CartData, clarify: Clarify, *, count: bool = True) -> str | None:
    """
    Count unresolved clarifies: asking the same slot (of the same line) again means the last answer did not resolve it.

    Returns a handoff reason once the slot has failed ``agent_clarify_fail_limit`` times, else None. Updates
    ``cart.state['clarify_fails']`` / ``last_clarify_key``.
    """
    key = f"{clarify.slot}:{clarify.line_id or clarify.sku or ''}"
    if not count:               # a side question / greeting / thanks: not an attempt to fill the slot, nothing changes
        return None
    fails = dict(cart.state.get("clarify_fails", {}))
    if cart.state.get("last_clarify_key") == key and clarify.slot not in ("cancel", "other", "change"):
        fails[key] = int(fails.get(key, 0)) + 1
    else:
        fails = {}
    cart.state["clarify_fails"] = fails
    cart.state["last_clarify_key"] = key
    if fails.get(key, 0) >= get_settings().agent_clarify_fail_limit:
        return f"could not resolve {clarify.slot} after {fails[key]} clarifications"
    return None


def clear_clarify_memory(cart: CartData) -> None:
    """A turn without a clarify: the customer moved on, so the failure counters restart."""
    cart.state.pop("clarify_fails", None)
    cart.state.pop("last_clarify_key", None)


# ── the plan → execute → repair loop ─────────────────────────────────────────

def _applied_summary(result: ExecResult) -> list[str]:
    """One line per change already made this turn, for the repair prompt."""
    out: list[str] = []
    for ap in result.applied:
        if ap.line:
            out.append(f"{ap.kind}: {ap.line.get('sku')} {ap.line.get('color') or ''} {ap.line.get('size') or ''} qty={ap.line.get('qty')} (line_id={ap.line.get('line_id')})".replace("  ", " "))
        else:
            out.append(f"{ap.kind}: {', '.join(ap.fields) or ''}".strip(": "))
    return out


FREE_TEXT_SLOTS = frozenset({"name", "address", "mobile"})


def keyword_search_outcome(catalogue: Catalogue, text: str) -> PlanOutcome | None:
    """A plan that searches the catalogue for the customer's own words, when they name something the shop sells."""
    if text and catalogue.keyword_hit(text):
        return PlanOutcome(output=PlannerOutput(actions=[SearchCatalog(type="search_catalog", query=" ".join(text.split())[:120])]))
    return None


async def execute_with_repair(
    plan_fn: PlanFn, *, db, client, conv, catalogue: Catalogue, cart: CartData, ctx: ExecContext,
    first_plan: PlanOutcome | None = None,
) -> ExecResult:
    """
    Run one turn: plan (or take `first_plan` from a fast path) → execute → repair. The caller then runs the grounding
    invariant, the single-option autofill and ``finish`` (clarify / summary decisions).

    `plan_fn(None)` asks the planner for the first plan; `plan_fn(RepairRequest)` asks it to fix its mistakes.
    A planner that returns None (error / 429 after its retry / invalid JSON) is never a dead end: a message naming something
    the shop sells becomes a catalogue search, anything else leaves the order's current step to be re-asked
    (``result.planner_failed`` lets the renderer say so briefly). An EMPTY plan for a message that names a catalogue product
    while no free-text slot is open gets the same search — the planner missed an obvious "I want <product>".
    """
    settings = get_settings()
    result = ExecResult(cart=cart.copy())
    prune_unavailable(result.cart, catalogue, result)
    outcome = first_plan if first_plan is not None else await plan_fn(None)
    open_slot = (ctx.last_clarify or {}).get("slot")
    if outcome is None:
        from app.services import agent_fallback

        bare = agent_fallback.resolve(ctx.user_text, ctx.last_clarify)
        if bare is not None:
            # A yes / no / number the planner could not read is answered against the open question — no apology (result.planner_failed stays False).
            output, grounded = bare
            ctx.grounded_by = grounded or ctx.grounded_by
            outcome = PlanOutcome(output=output)
            result.fixes.append("planner_down_bare_reply")
            logger.info("AGENT planner down conv=%s bare reply %r → %s", getattr(conv, "id", None), (ctx.user_text or "")[:20], [a.type for a in output.actions] or "re-ask the open question")
        else:
            result.planner_failed = True
            outcome = keyword_search_outcome(catalogue, ctx.user_text) or PlanOutcome(output=PlannerOutput(actions=[]))
            logger.info("AGENT planner fallback conv=%s → %s", getattr(conv, "id", None), [a.type for a in outcome.output.actions] or "re-show the current step")
    elif (
        first_plan is None and not outcome.output.actions and not outcome.parse_issues
        and outcome.output.reply_intent in ("none", "smalltalk") and open_slot not in FREE_TEXT_SLOTS
    ):
        guard = keyword_search_outcome(catalogue, ctx.user_text)
        if guard is not None:
            logger.info("AGENT empty plan for a catalogue word conv=%s — searching for %r", getattr(conv, "id", None), ctx.user_text[:60])
            outcome = PlanOutcome(output=PlannerOutput(actions=guard.output.actions, reply_intent="none"))
    result.cart.state["reply_intent"] = outcome.output.reply_intent
    soft: list[ActionIssue] = []            # issues the planner cannot fix — answered deterministically
    fixable: list[ActionIssue] = []
    repairs = 0
    executor = Executor(db, client, conv, catalogue, ctx, result, cart)
    while outcome is not None:
        round_issues = list(outcome.parse_issues) + await executor.run(outcome.output.actions)
        soft.extend(x for x in round_issues if x.code not in FIXABLE_CODES)
        fixable = [x for x in round_issues if x.code in FIXABLE_CODES]
        if not fixable or repairs >= settings.agent_max_repairs:
            break
        repairs += 1
        result.repairs = repairs
        outcome = await plan_fn(RepairRequest(cart=result.cart, applied=_applied_summary(result), issues=fixable))
    if ctx.forced_issue:
        forced = dict(ctx.forced_issue)
        code = forced.pop("code")
        soft.append(ActionIssue(index=0, action_type="update_line", code=code, message=code, valid_options=[], details=forced))
    result.issues = soft + fixable           # whatever is still standing (fixable ones only if the repairs ran out)
    return result


def autofill_single_options(result: ExecResult, catalogue: Catalogue) -> bool:
    """
    Select an attribute automatically when exactly ONE value is available for it (given the rest of the line): the customer
    has no choice to make, so this is not a silent override. Runs AFTER the grounding invariant (the value is not the
    customer's words) and is announced in the reply (``Applied("autofilled")``). Returns True when anything changed.
    """
    changed = False
    for ln in result.cart.lines:
        view = catalogue.get(ln["sku"])
        if view is None:
            continue
        fixed = line_fixed(view, ln)
        for attr in view.needed_attrs:
            if ln.get(attr):
                continue
            values = view.values(attr, fixed)
            if len(values) == 1:
                ln[attr] = fixed[attr] = values[0]
                result.applied.append(Applied("autofilled", dict(ln), fields=[attr]))
                changed = True
    if changed:
        result.mutated = True
        result.cart.invalidate_summary()
    return changed


def finish(result: ExecResult, catalogue: Catalogue, client, conv, ctx: ExecContext, before: CartData) -> None:
    """After the last round: turn standing issues into a real-options clarify, count clarifies, auto re-summarise."""
    cart, effects = result.cart, result.effects
    unresolved = [x for x in result.issues if x.code not in ("SKIPPED",)]
    if unresolved and effects.order is None and effects.cancel is None:
        first = unresolved[0]
        if first.code == "NOT_CONFIRMABLE" and not gaps(cart, catalogue, client, is_whatsapp=ctx.is_whatsapp) and stock_problem(cart, catalogue) is None:
            effects.show_summary = True       # "yes" that cannot be applied → show the summary again, never guess
        else:
            effects.clarify = clarify_for_issue(first, cart, catalogue, client, is_whatsapp=ctx.is_whatsapp)
    # A change made while the summary was on screen invalidates it: show it again (or ask what is now missing).
    if before.stage == "confirming" and result.mutated and effects.order is None and effects.cancel is None and effects.clarify is None:
        effects.show_summary = not gaps(cart, catalogue, client, is_whatsapp=ctx.is_whatsapp) and stock_problem(cart, catalogue) is None
    # The order is waiting for something and this turn shows nothing else: that is a question to ask (and count) — except after a
    # bare greeting: the renderer decides how a returning customer is welcomed (recap + buttons, or the open question) itself.
    greeting_only = cart.state.get("reply_intent") == "greeting" and not result.plan_actions
    if (
        effects.clarify is None and not effects.show_summary and cart.lines and effects.order is None and not greeting_only
        and effects.cancel is None and effects.handoff is None and effects.search is None and effects.browse is None
        and effects.product is None and effects.images_for is None and not effects.answers and not effects.not_in_cart
    ):
        open_gaps = gaps(cart, catalogue, client, is_whatsapp=ctx.is_whatsapp)
        if open_gaps:
            g = open_gaps[0]
            effects.clarify = Clarify(slot=g.slot, options=g.options, line_id=g.line_id, sku=g.sku)
    # Nothing left to ask and nothing else to show: the order is complete — show the summary (as the legacy flow does).
    if (
        result.mutated and cart.lines and effects.clarify is None and not effects.show_summary
        and effects.order is None and effects.cancel is None and effects.handoff is None
        and effects.search is None and effects.browse is None and effects.product is None and effects.images_for is None
        and not gaps(cart, catalogue, client, is_whatsapp=ctx.is_whatsapp) and stock_problem(cart, catalogue) is None
    ):
        effects.show_summary = True
    # A quantity below 1 / an "are you sure?" about a big quantity is the customer correcting a number, not a clarify that failed: it does not
    # count toward the hand-over-to-a-human limit.
    attempted = bool(set(result.plan_actions) & _ATTEMPT_ACTIONS) or any(i.code not in ("QTY_MIN", "QTY_CONFIRM") for i in result.issues)
    if effects.clarify is not None:
        handoff = register_clarify(cart, effects.clarify, count=attempted)
        if handoff:
            logger.warning("AGENT clarify limit conv=%s slot=%s — handing over to a human", conv.id, effects.clarify.slot)
            effects.clarify = None
            effects.handoff = handoff
    elif attempted:
        clear_clarify_memory(cart)
