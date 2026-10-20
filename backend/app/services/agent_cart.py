"""
Agent v3 — the cart: an in-memory ``CartData`` working copy and its persistence in ``conversation_cart``.

Rules worth knowing:

* One ACTIVE cart per conversation; ``get_active`` opens it on first use and retires one that went stale.
* The executor mutates a **copy** (``CartData.copy()``); ``save`` writes it only when the whole plan validated, using the
  row's ``version`` as an optimistic lock (``CartConflict`` if another turn got there first).
* A field the customer has not given is ``None``. Nothing in this module ever fills one in.
"""

from __future__ import annotations

import copy
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update

from app.models.conversation_cart import CART_ACTIVE, CART_CANCELLED, CART_CONFIRMED, CART_EXPIRED, ConversationCart

logger = logging.getLogger("app.services.agent_cart")

LINE_FIELDS: tuple[str, ...] = ("color", "size", "material", "qty")
STAGES = ("browsing", "collecting", "confirming")


class CartConflict(Exception):
    """The cart row changed under this turn (another message committed first); the turn should be re-run."""


def new_line_id() -> str:
    """A short, unique, planner-friendly line id."""
    return uuid.uuid4().hex[:6]


SHOWN_LISTS_KEEP = 6               # replies remembered for "what was on the customer's screen when they typed"
DELIVERY_GRACE_SECONDS = 2.0       # stored → on the customer's phone (reply pause + WhatsApp send + delivery)


@dataclass
class CartData:
    """A cart as plain data (lines, customer, payment, planner memory) — cheap to copy, diff and serialise."""

    id: int
    conversation_id: int
    client_id: int
    lines: list[dict[str, Any]] = field(default_factory=list)
    customer: dict[str, Any] = field(default_factory=dict)
    payment_method: str | None = None
    status: str = CART_ACTIVE
    version: int = 1
    state: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None

    def copy(self) -> "CartData":
        """Deep copy — the executor mutates this, the original stays the rollback point."""
        return copy.deepcopy(self)

    # ── lines ────────────────────────────────────────────────────────────────
    def line(self, line_id: str | None) -> dict[str, Any] | None:
        """The line with this id (case-insensitive), or None."""
        wanted = (line_id or "").strip().lower()
        return next((ln for ln in self.lines if ln["line_id"].lower() == wanted), None)

    def lines_for(self, sku: str) -> list[dict[str, Any]]:
        """All lines of one product."""
        return [ln for ln in self.lines if ln["sku"].upper() == sku.upper()]

    def add_line(self, sku: str, *, color=None, size=None, material=None, qty=None) -> dict[str, Any]:
        """Append a new line and return it."""
        ln = {"line_id": new_line_id(), "sku": sku.upper(), "color": color, "size": size, "material": material, "qty": qty}
        self.lines.append(ln)
        return ln

    @property
    def skus(self) -> list[str]:
        """Distinct SKUs in the cart, in order of first appearance."""
        return list(dict.fromkeys(ln["sku"] for ln in self.lines))

    # ── planner memory (state) ───────────────────────────────────────────────
    @property
    def stage(self) -> str:
        """browsing (no lines) / collecting / confirming (summary on screen)."""
        stage = self.state.get("stage")
        if stage in STAGES:
            return stage
        return "collecting" if self.lines else "browsing"

    def set_stage(self, stage: str) -> None:
        """Move the cart to `stage`."""
        assert stage in STAGES, stage
        self.state["stage"] = stage

    def remember(self, key: str, sku: str | None, *, cap: int = 30) -> None:
        """Append a SKU to a de-duplicated memory list (viewed / offered / removed / shown)."""
        if not sku:
            return
        items = [s for s in self.state.get(key, []) if s != sku.upper()]
        items.append(sku.upper())
        self.state[key] = items[-cap:]

    def record_shown(self, clarify: dict[str, Any] | None, sent_at: float, *, keep: int = SHOWN_LISTS_KEEP) -> None:
        """Remember the open question (a list / menu / option set, or None = nothing open) each reply left, with when it went out."""
        history = [h for h in self.state.get("shown_lists", []) if isinstance(h, dict)]
        history.append({"clarify": clarify or None, "sent_at": round(sent_at, 3)})
        self.state["shown_lists"] = history[-keep:]

    def clarify_as_of(self, inbound_ts: float | None, *, grace: float = DELIVERY_GRACE_SECONDS) -> dict[str, Any] | None:
        """
        The open question the customer could actually SEE when they sent a message stamped `inbound_ts`.

        A number / option refers to the latest list DELIVERED before the message was typed, not the newest one we have computed
        (the customer may have sent "1" while the next reply was still in flight). A reply counts as delivered `grace` seconds
        after we stored it (reply pause + send + WhatsApp delivery). Without a timestamp, or with nothing older to fall back on,
        it is the newest question (``state['last_clarify']``).
        """
        history = [h for h in self.state.get("shown_lists", []) if isinstance(h, dict) and "sent_at" in h]
        if inbound_ts is None or not history:
            return self.state.get("last_clarify") or None
        seen = [h for h in history if float(h["sent_at"]) + grace <= inbound_ts]
        if not seen:
            return None       # typed before any of the remembered replies reached the customer: nothing of ours was open
        return seen[-1].get("clarify") or None

    def memory(self, key: str) -> list[str]:
        """A memory list (empty when unset)."""
        return list(self.state.get(key, []))

    @property
    def summary_on_screen(self) -> bool:
        """True when the summary shown to the customer is for exactly this version of the cart."""
        return self.stage == "confirming" and self.state.get("summary_version") == self.version

    def invalidate_summary(self) -> None:
        """A change after the summary was shown: it no longer describes the cart."""
        self.state.pop("summary_version", None)

    def known_values(self) -> dict[str, dict[str, set[str]]]:
        """
        What the customer has already settled per product: ``{sku: {field: {values}}}`` from the lines in this cart and from
        ``state.chosen`` (earlier carts of the conversation). A value that is already known is not "invented" when a plan
        repeats it ("one more in pink, same size").
        """
        known: dict[str, dict[str, set[str]]] = {}
        for sku, fields in (self.state.get("chosen") or {}).items():
            for f, values in fields.items():
                known.setdefault(sku.upper(), {}).setdefault(f, set()).update(str(v).lower() for v in values)
        for ln in self.lines:
            for f in LINE_FIELDS:
                if ln.get(f) is not None:
                    known.setdefault(ln["sku"].upper(), {}).setdefault(f, set()).add(str(ln[f]).lower())
        return known

    def remember_chosen(self) -> None:
        """Record every value now on a line into ``state.chosen`` (kept across carts; at most 40 products)."""
        chosen: dict[str, dict[str, list]] = {k: {f: list(v) for f, v in fs.items()} for k, fs in (self.state.get("chosen") or {}).items()}
        for ln in self.lines:
            for f in LINE_FIELDS:
                if ln.get(f) is not None and ln[f] not in chosen.setdefault(ln["sku"].upper(), {}).setdefault(f, []):
                    chosen[ln["sku"].upper()][f].append(ln[f])
        self.state["chosen"] = dict(list(chosen.items())[-40:])

    def to_prompt(self) -> dict[str, Any]:
        """The cart as the planner sees it (no prices — those come from the catalogue rows)."""
        return {
            "lines": [
                {k: ln.get(k) for k in ("line_id", "sku", "color", "size", "material", "qty")} for ln in self.lines
            ],
            "customer": {
                "name": bool(self.customer.get("name")), "address": bool(self.customer.get("address")),
            },
            "payment_method": self.payment_method,
            "summary_on_screen": self.summary_on_screen,
        }


def _to_data(row: ConversationCart) -> CartData:
    """ORM row → working copy."""
    return CartData(
        id=row.id, conversation_id=row.conversation_id, client_id=row.client_id,
        lines=copy.deepcopy(row.lines or []), customer=copy.deepcopy(row.customer or {}),
        payment_method=row.payment_method, status=row.status, version=row.version or 1,
        state=copy.deepcopy(row.state or {}), updated_at=row.updated_at or row.created_at,
    )


def idle_hours(data: "CartData") -> float | None:
    """Hours since the cart was last touched by a real turn (None when unknown)."""
    last = _aware(data.updated_at)
    return None if last is None else max(0.0, (datetime.now(timezone.utc) - last).total_seconds() / 3600.0)


def _aware(dt: datetime | None) -> datetime | None:
    """Treat a naive timestamp as UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _active_row(db, conversation_id: int) -> ConversationCart | None:
    """The conversation's active cart row, if any."""
    res = await db.execute(
        select(ConversationCart).where(
            ConversationCart.conversation_id == conversation_id, ConversationCart.status == CART_ACTIVE,
        )
    )
    return res.scalar_one_or_none()


async def _open_row(
    db, conversation_id: int, client_id: int, *, customer: dict[str, Any] | None = None, state: dict[str, Any] | None = None,
) -> ConversationCart:
    """Create and flush a fresh active cart (carrying the customer's name / address / mobile when given)."""
    row = ConversationCart(
        conversation_id=conversation_id, client_id=client_id, lines=[], customer=dict(customer or {}),
        status=CART_ACTIVE, version=1, state={"stage": "browsing", **(state or {})},
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def get_active(
    db, conv, client_id: int, *, stale_after: timedelta | None = None, mobile: str | None = None,
) -> CartData:
    """
    The conversation's active cart, created if missing.

    A cart with lines that has been idle longer than `stale_after` is retired (``expired``) and a fresh one opened:
    a customer coming back days later must not find yesterday's half-built order silently resumed.
    `mobile` (the WhatsApp sender) pre-fills the delivery contact number, as the legacy flow does.
    """
    row = await _active_row(db, conv.id)
    now = datetime.now(timezone.utc)
    if row is not None and stale_after is not None and row.lines:
        last = _aware(row.updated_at or row.created_at)
        if last is not None and now - last > stale_after:
            logger.info("cart expired conv=%s idle=%s lines=%d", conv.id, now - last, len(row.lines))
            row.status = CART_EXPIRED
            await db.commit()
            row = None
    if row is None:
        customer, carried = await _carried_from_last(db, conv)
        row = await _open_row(db, conv.id, client_id, customer=customer, state=carried)
    data = _to_data(row)
    if mobile and not data.customer.get("mobile"):
        data.customer["mobile"] = mobile
    return data


# Planner memory that outlives one cart: what the customer was offered / looked at / was told, orders already acknowledged,
# the open question (a list they may still answer by number) and the running token / cost total of the conversation.
CARRIED_STATE_KEYS = ("offered_upsell", "removed_skus", "viewed_skus", "acked_orders", "last_clarify", "shown_lists", "chosen", "lang_track")


async def _carried_from_last(db, conv) -> tuple[dict[str, Any], dict[str, Any]]:
    """(customer, carried state) from the latest non-active cart; the customer falls back to the legacy columns."""
    res = await db.execute(
        select(ConversationCart.customer, ConversationCart.state)
        .where(ConversationCart.conversation_id == conv.id, ConversationCart.status != CART_ACTIVE)
        .order_by(ConversationCart.id.desc()).limit(1)
    )
    row = res.first()
    prev, prev_state = (row[0] or {}, row[1] or {}) if row else ({}, {})
    customer = {k: v for k, v in {
        "name": prev.get("name") or getattr(conv, "customer_name", None),
        "address": prev.get("address") or getattr(conv, "delivery_address", None),
        "mobile": prev.get("mobile") or getattr(conv, "mobile_number", None),
    }.items() if v}
    return customer, {k: prev_state[k] for k in CARRIED_STATE_KEYS if k in prev_state}


async def save(db, data: CartData, *, bump: bool = True, touch: bool = True) -> CartData:
    """
    Persist the working copy with an optimistic version check.

    Raises CartConflict when the row's version is no longer the one this copy was loaded at. ``touch=False`` keeps ``updated_at``
    as it was — a bare greeting must not make an old cart look fresh (the next hello would then resume it instead of recapping it).
    """
    new_version = data.version + 1 if bump else data.version
    values = dict(lines=data.lines, customer=data.customer, payment_method=data.payment_method, state=data.state, version=new_version)
    # The column has an onupdate default, so "keep as it was" must be said explicitly.
    values["updated_at"] = datetime.now(timezone.utc) if touch or data.updated_at is None else data.updated_at
    res = await db.execute(
        update(ConversationCart)
        .where(ConversationCart.id == data.id, ConversationCart.version == data.version, ConversationCart.status == CART_ACTIVE)
        .values(**values)
    )
    if res.rowcount != 1:
        await db.rollback()
        raise CartConflict(f"cart {data.id} changed under this turn")
    await db.commit()
    data.version = new_version
    return data


async def close(
    db, data: CartData, status: str, *, order_id: int | None = None, conv=None,
) -> CartData:
    """
    Retire the active cart as confirmed / cancelled and open the next, empty one (the customer's name / address /
    mobile carry over; lines, payment and planner memory do not — except what must outlive a cart, see below).
    """
    assert status in (CART_CONFIRMED, CART_CANCELLED, CART_EXPIRED), status
    res = await db.execute(
        update(ConversationCart)
        .where(ConversationCart.id == data.id, ConversationCart.status == CART_ACTIVE)
        .values(
            status=status, order_id=order_id, lines=data.lines, customer=data.customer,
            payment_method=data.payment_method, state=data.state, version=data.version + 1,
            updated_at=datetime.now(timezone.utc),
        )
    )
    if res.rowcount != 1:
        await db.rollback()
        raise CartConflict(f"cart {data.id} was already closed")
    await db.commit()
    carried_state = {k: data.state[k] for k in CARRIED_STATE_KEYS if k in data.state}
    row = await _open_row(db, data.conversation_id, data.client_id, customer={
        k: v for k, v in data.customer.items() if k in ("name", "address", "mobile") and v
    }, state=carried_state)
    return _to_data(row)


# ── diff (what did this plan change?) ────────────────────────────────────────

@dataclass(frozen=True)
class Change:
    """One value that is new or different after a plan ran. ``kind`` is 'line' (color/size/material/qty), 'payment' or 'customer'."""

    kind: str
    field: str
    old: Any
    new: Any
    line_id: str | None = None
    sku: str | None = None


def changes(before: CartData, after: CartData) -> list[Change]:
    """Every value the plan wrote: new lines' set fields, edited fields of existing lines, payment, name / address."""
    out: list[Change] = []
    old_lines = {ln["line_id"]: ln for ln in before.lines}
    for ln in after.lines:
        prev = old_lines.get(ln["line_id"])
        for f in LINE_FIELDS:
            if ln.get(f) is None:
                continue
            if prev is None or prev.get(f) != ln.get(f):
                out.append(Change("line", f, None if prev is None else prev.get(f), ln.get(f), ln["line_id"], ln.get("sku")))
    if after.payment_method and after.payment_method != before.payment_method:
        out.append(Change("payment", "payment_method", before.payment_method, after.payment_method))
    for f in ("name", "address"):
        if after.customer.get(f) and after.customer.get(f) != before.customer.get(f):
            out.append(Change("customer", f, before.customer.get(f), after.customer.get(f)))
    return out


async def recent_cancelled(db, conv, ttl: timedelta) -> tuple[list[dict[str, Any]], str | None]:
    """(lines, payment method) of the conversation's latest CANCELLED cart if cancelled within `ttl` — "continue" restores them."""
    res = await db.execute(
        select(ConversationCart.lines, ConversationCart.updated_at, ConversationCart.payment_method)
        .where(ConversationCart.conversation_id == conv.id, ConversationCart.status == CART_CANCELLED)
        .order_by(ConversationCart.id.desc()).limit(1)
    )
    row = res.first()
    if row is None or not row[0]:
        return [], None
    when = _aware(row[1])
    if when is None or datetime.now(timezone.utc) - when > ttl:
        return [], None
    return [dict(ln) for ln in row[0]], row[2]
