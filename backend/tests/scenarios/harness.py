"""
Agent v3 test harness: a seeded shop, structured capture of everything the bot sends, a scripted planner, and a turn runner
that drives the REAL webhook → pipeline → Postgres (replay DB).

Only the LLM is replaced: ``agent_planner.call_model`` returns the plan a scenario scripts for each customer message
(``RUN_LIVE_LLM=1`` leaves the real planner in place and ignores the scripts — that is how planner quality is measured).
Everything else — fast paths, executor, renderer, invariants, cart persistence, order creation — is production code.
"""

from __future__ import annotations

import ast
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from tests.replay.conftest import META_APP_SECRET, WA_PHONE_NUMBER_ID  # noqa: F401
from tests.replay.helpers import _sign

SCENARIO_DIR = Path(__file__).resolve().parent
_HUMAN_DELAY = 5       # seconds between our reply and the customer's next message (the sender-side timestamp we stamp on it)
PHONE_BASE = "9195550"


# ── scripted planner ─────────────────────────────────────────────────────────

class ScriptedPlanner:
    """Stands in for ``agent_planner.call_model``: returns the next scripted plan; an unscripted call fails the test."""

    def __init__(self) -> None:
        """Empty queue, no calls yet."""
        self.queue: list[dict | Exception] = []
        self.calls: list[list[dict]] = []

    def script(self, plans: list[dict | Exception] | None) -> None:
        """Plans for the next customer message: the first plan, then one per repair round."""
        self.queue = list(plans or [])

    async def __call__(self, messages, *, client_id=None, conversation_id=None, model=None):
        """Same contract as call_model: a JSON-ish dict (or raises). A plan's "__usage__" feeds the token accounting."""
        from app.config import get_settings
        from app.services import llm_usage_service

        self.calls.append(messages)
        if not self.queue:
            raise AssertionError("the planner was called but the scenario scripted no plan for this turn")
        nxt = self.queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        usage = nxt.pop("__usage__", None) if isinstance(nxt, dict) else None
        if usage:      # what llm_client.llm_call would report for this provider response: one ledger row
            await llm_usage_service.record_usage(
                purpose="planner", model=get_settings().llm_model_planner, prompt_tokens=usage.get("input", 0),
                cached_tokens=usage.get("cached", 0), completion_tokens=usage.get("output", 0),
                client_id=client_id, conversation_id=conversation_id,
            )
        return nxt


class ProviderError(RuntimeError):
    """A scripted provider failure carrying an HTTP status (429 → the planner's backoff-and-retry path)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Remember the status the way the openai SDK's errors expose it."""
        super().__init__(message)
        self.status_code = status_code


# ── capture ──────────────────────────────────────────────────────────────────

@dataclass
class Out:
    """One outbound WhatsApp message as the customer would see it."""

    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)       # (full id, title)
    rows: list[tuple[str, str]] = field(default_factory=list)          # (full id, title)
    image: tuple[str, str | None] | None = None

    def action_ids(self) -> list[str]:
        """Button / row ids without the ~conv~nonce suffix."""
        return [i.split("~")[0] for i, _ in self.buttons + self.rows]


def capture_structured(monkeypatch) -> list[Out]:
    """Record every outbound send (text, buttons, list rows, images) in order."""
    sent: list[Out] = []

    async def _text(to_phone_number, message_text, phone_number_id=None, access_token=None):
        sent.append(Out(message_text))
        return {"messages": [{"id": f"wamid.out.{len(sent)}"}]}

    async def _buttons(to_phone_number, body_text, buttons, phone_number_id=None, access_token=None):
        sent.append(Out(body_text, buttons=[(b["id"], b["title"]) for b in buttons]))
        return True

    async def _list(to_phone_number, header_text, body_text, button_text, sections, phone_number_id=None, access_token=None):
        rows = [(r["id"], r["title"]) for s in sections for r in s["rows"]]
        sent.append(Out(body_text, rows=rows))
        return True

    async def _image(to_phone_number, image_url, caption=None, phone_number_id=None, access_token=None):
        sent.append(Out(caption or "", image=(image_url, caption)))
        return {"messages": [{"id": f"wamid.outimg.{len(sent)}"}]}

    monkeypatch.setattr("app.services.whatsapp_service._raw_send_text_message", _text)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_button_message", _buttons)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_list_message", _list)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_image_message", _image)
    return sent


def wa_list_reply_payload(sender: str, row_id: str, title: str, *, phone_number_id: str, wamid: str | None = None, ts: int | None = None):
    """Webhook body for a WhatsApp list_reply (the helpers only ship a button_reply builder)."""
    wamid = wamid or f"wamid.lst.{sender}.{time.time_ns()}"
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "ENTRY_ID", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "0", "phone_number_id": phone_number_id},
            "contacts": [{"profile": {"name": "Test User"}, "wa_id": sender}],
            "messages": [{"from": sender, "id": wamid, "timestamp": str(ts or int(time.time())), "type": "interactive",
                          "interactive": {"type": "list_reply", "list_reply": {"id": row_id, "title": title}}}],
        }}]}],
    }
    body = json.dumps(payload).encode()
    return body, {"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"}


# ── seeded shop ──────────────────────────────────────────────────────────────

SHOPS = {
    # name: (accepts_cod, accepts_upi, upi_id)
    "default": (True, True, "shop@upi"),
    "upi_only": (False, True, "shop@upi"),
    "cod_only": (True, False, None),
}


async def seed_shop(session, *, n: str = "01", shop: str = "default", agent_v3: bool = True):
    """
    A client with agent v3 on and this catalogue:

      KU10001 Cotton Kurti ₹499       Red 5 · Green 5 · Blue 2 · Pink 0 (sold out)           (colour only)
      LH10042 Cotton Lehenga ₹1,299   Pink/M 5 · Pink/XXL 0 · Blue/M 5 · Blue/XXL 3          (colour × size)
      TB30001 Handmade Tote Bag ₹250  no variants, stock 10
      SR20001 Banarasi Silk Saree ₹2,499   Maroon 3 · Gold 2                                  (colour only)
      SC40001 Silk Scarf ₹150         no variants, stock 0 (sold out)
      WL60001 Leather Wallet ₹550     Black / Brown, ONE size ("One Size")                   (size has a single option)
      TM70001 Travel Mug ₹600         White only                                              (every attribute has a single option)
      ZC80001 Zari Choli Set ₹1,899   no variants, stock 4 (category "Lehenga Choli")
      ZB…     9 × "Zari Block Print Saree N" ₹899 (Cream only) and ZS80001 "Zari Silk Saree" ₹1,499 (Black / Cream): sarees that sort AFTER
              the first 8 name matches, so a colour filter that only looks at the top of the list never finds the black one
      Photos: Cotton Kurti (+ Red / Green / Blue variants), Banarasi Silk Saree, Handmade Tote Bag.

    Returns (client, phone, phone_number_id).
    """
    from app.models.client import Client
    from app.models.product import Product
    from app.models.product_variant import ProductVariant

    accepts_cod, accepts_upi, upi = SHOPS[shop]
    phone, pnid = f"{PHONE_BASE}{n}", f"881{n}"
    client = Client(
        business_name="Riya Boutique", email=f"v3_{n}@test.com", phone=phone[:-1] + "0", whatsapp_phone_number_id=pnid,
        accepts_cod=accepts_cod, accepts_upi=accepts_upi, upi_id=upi, hashed_password="x",
        delivery_days_min=3, delivery_days_max=7, agent_v3_enabled=agent_v3, router_v2_enabled=False,
        catalogue_slug=f"riya-{n}",
    )
    session.add(client)
    await session.flush()

    def prod(sku, name, price, category, stock, has_variants=False):
        """Add a product row."""
        p = Product(client_id=client.id, name=name, sku=sku, price=price, stock=stock, is_active=True,
                    has_variants=has_variants, category=category)
        session.add(p)
        return p

    kurti = prod("KU10001", "Cotton Kurti", 499.0, "Kurti", 12, True)
    lehenga = prod("LH10042", "Cotton Lehenga", 1299.0, "Lehenga", 13, True)
    tote = prod("TB30001", "Handmade Tote Bag", 250.0, "Bags", 10)
    saree = prod("SR20001", "Banarasi Silk Saree", 2499.0, "Saree", 5, True)
    prod("SC40001", "Silk Scarf", 150.0, "Accessories", 0)
    wallet = prod("WL60001", "Leather Wallet", 550.0, "Accessories", 6, True)
    mug = prod("TM70001", "Travel Mug", 600.0, "Home", 4, True)
    prod("ZC80001", "Zari Choli Set", 1899.0, "Lehenga Choli", 4)
    block_sarees = [prod(f"ZB{i:04d}0", f"Zari Block Print Saree {i}", 899.0, "Saree", 5, True) for i in range(1, 10)]
    silk = prod("ZS80001", "Zari Silk Saree", 1499.0, "Saree", 6, True)
    await session.flush()
    for sp in block_sarees:
        session.add(ProductVariant(product_id=sp.id, client_id=client.id, color="Cream", stock=5, is_active=True, price=899.0))
    for color, stock in (("Black", 3), ("Cream", 3)):
        session.add(ProductVariant(product_id=silk.id, client_id=client.id, color=color, stock=stock, is_active=True, price=1499.0))
    for p_, url in ((kurti, "https://cdn.test/ku10001.jpg"), (saree, "https://cdn.test/sr20001.jpg")):
        p_.image_url = url
    tote.image_url = "https://cdn.test/tb30001.jpg"
    for color, size, stock in (("Black", "One Size", 3), ("Brown", "One Size", 3)):
        session.add(ProductVariant(product_id=wallet.id, client_id=client.id, color=color, size=size, stock=stock, is_active=True, price=550.0))
    session.add(ProductVariant(product_id=mug.id, client_id=client.id, color="White", stock=4, is_active=True, price=600.0))
    for color, stock in (("Red", 5), ("Green", 5), ("Blue", 2), ("Pink", 0)):
        img = f"https://cdn.test/ku10001-{color.lower()}.jpg" if color != "Pink" else None
        session.add(ProductVariant(product_id=kurti.id, client_id=client.id, color=color, stock=stock, is_active=True, price=499.0, image_url=img))
    for color, size, stock in (("Pink", "M", 5), ("Pink", "XXL", 0), ("Blue", "M", 5), ("Blue", "XXL", 3)):
        session.add(ProductVariant(product_id=lehenga.id, client_id=client.id, color=color, size=size, stock=stock, is_active=True, price=1299.0))
    for color, stock in (("Maroon", 3), ("Gold", 2)):
        session.add(ProductVariant(product_id=saree.id, client_id=client.id, color=color, stock=stock, is_active=True, price=2499.0))
    await session.commit()
    await session.refresh(client)
    return client, phone, pnid


# ── conversation access ──────────────────────────────────────────────────────

async def conv_of(session, client_id: int, phone: str):
    """The (fresh) Conversation row of this customer, or None."""
    from app.models.conversation import Conversation

    r = await session.execute(
        select(Conversation).where(Conversation.client_id == client_id, Conversation.phone_number == phone)
        .execution_options(populate_existing=True))
    return r.scalar_one_or_none()


async def active_cart(session, conv_id: int):
    """The conversation's active ConversationCart (fresh), or None."""
    from app.models.conversation_cart import ConversationCart

    r = await session.execute(
        select(ConversationCart).where(ConversationCart.conversation_id == conv_id, ConversationCart.status == "active")
        .execution_options(populate_existing=True))
    return r.scalar_one_or_none()


async def orders_of(session, client_id: int) -> list:
    """All orders of a client, oldest first (fresh)."""
    from app.models.order import Order

    r = await session.execute(
        select(Order).where(Order.client_id == client_id).order_by(Order.id).execution_options(populate_existing=True))
    return list(r.scalars().all())


# ── a customer driving the bot ───────────────────────────────────────────────

class Customer:
    """One WhatsApp customer talking to a seeded shop through the real webhook."""

    def __init__(self, http, session, sent: list[Out], client, phone: str, pnid: str) -> None:
        """Bind the HTTP client, DB session, capture list and identity."""
        self.http, self.session, self.sent, self.client, self.phone, self.pnid = http, session, sent, client, phone, pnid

    async def say(self, text: str, ts: int | None = None) -> list[Out]:
        """Send a typed message (optionally with the sender-side Meta timestamp `ts`); return what the bot sent in reply."""
        from tests.replay.helpers import wa_text_payload

        before = len(self.sent)
        body, headers = wa_text_payload(self.phone, text, phone_number_id=self.pnid, ts=ts or int(time.time()) + _HUMAN_DELAY)
        resp = await self.http.post("/webhook", content=body, headers=headers)
        assert resp.status_code == 200, resp.text
        return self.sent[before:]

    async def image(self) -> list[Out]:
        """Send a photo (not a payment proof unless an order is awaiting one)."""
        from tests.replay.helpers import send_image

        before = len(self.sent)
        resp = await send_image(self.http, self.phone, phone_number_id=self.pnid)
        assert resp.status_code == 200, resp.text
        return self.sent[before:]

    async def tap(self, action: str, ts: int | None = None) -> list[Out]:
        """Tap the most recent button / list row whose id (without ~conv~nonce) is `action`."""
        found = None
        m_pos = re.fullmatch(r"v3_(opt|rm)_(\d+)", action)
        if m_pos:       # "the N-th option of the last question": option ids are structured (v3_opt:<line>:<slot>:<value>, v3_rm:<line>), the position is what scenarios mean
            n = int(m_pos.group(2))
            prefixes = ("v3_opt:", "v3_var:", "v3_opt_") if m_pos.group(1) == "opt" else ("v3_rm:",)
            for out in reversed(self.sent):
                options = [(rid, t) for rid, t in out.buttons + out.rows if rid.split("~")[0].startswith(prefixes)]
                if options and n <= len(options):
                    action = options[n - 1][0].split("~")[0]
                    break
        for out in reversed(self.sent):
            for rid, title in out.buttons + out.rows:
                if rid.split("~")[0] == action:
                    found = (rid, title, rid in [r for r, _ in out.rows])
                    break
            if found:
                break
        assert found, f"no button / row {action!r} was offered (last: {[o.action_ids() for o in self.sent[-3:]]})"
        rid, title, is_row = found
        before = len(self.sent)
        if is_row:
            body, headers = wa_list_reply_payload(self.phone, rid, title, phone_number_id=self.pnid, ts=ts or int(time.time()) + _HUMAN_DELAY)
            resp = await self.http.post("/webhook", content=body, headers=headers)
        else:
            from tests.replay.helpers import wa_button_payload

            body, headers = wa_button_payload(self.phone, rid, title, phone_number_id=self.pnid, ts=ts or int(time.time()) + _HUMAN_DELAY)
            resp = await self.http.post("/webhook", content=body, headers=headers)
        assert resp.status_code == 200, resp.text
        return self.sent[before:]

    async def cart(self):
        """The active cart row (None before the first turn)."""
        conv = await conv_of(self.session, self.client.id, self.phone)
        return await active_cart(self.session, conv.id) if conv else None

    async def conv(self):
        """The conversation row."""
        return await conv_of(self.session, self.client.id, self.phone)


def joined(outs: list[Out]) -> str:
    """All reply texts of a turn, joined."""
    return "\n".join(o.text for o in outs)


_AGENT_ACTIONS_RE = re.compile(r"AGENT conv=\S+ fast=(\S+) planner=(\S+) actions=(\[.*?\]) issues=(\[.*?\]) repairs=(\d+) violations=(\[.*?\])")


def parse_agent_logs(records) -> list[dict]:
    """The ``AGENT …`` trace lines of a turn as dicts: fast, planner, actions, issues, repairs, violations."""
    out = []
    for rec in records:
        m = _AGENT_ACTIONS_RE.search(rec.getMessage())
        if m:
            out.append({
                "fast": None if m.group(1) == "None" else m.group(1), "planner": m.group(2) == "True",
                "actions": ast.literal_eval(m.group(3)), "issues": ast.literal_eval(m.group(4)),
                "repairs": int(m.group(5)), "violations": ast.literal_eval(m.group(6)), "raw": rec.getMessage(),
            })
    return out
