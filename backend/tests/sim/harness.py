"""
Simulator harness: throwaway Postgres, the real FastAPI app, captured WhatsApp sends, per-turn checks.

Everything except the outside world is real: signed webhook → pipeline → Postgres. Outside world =
Groq (tests/sim/fake_llm.py), Meta send APIs (captured), media download/storage (stubbed).
Run via tests/sim/run_sim.py.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import hmac
import itertools
import json
import logging
import os
import re
import subprocess
import sys
import time
import unittest.mock as mock
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
SIM_DB = os.environ.get("SIM_DB", "sim_5k")
ADMIN_URL = "postgresql://bytes-amit@localhost/ai_agent_dev"
ASYNC_URL = f"postgresql+asyncpg://bytes-amit@localhost/{SIM_DB}"
APP_SECRET = "replay-test-secret"
PNIDS = {"router": "555000001", "legacy": "555000002", "ig": "555000003"}
IG_ACCOUNT = "ig-acc-sim-1"

CURRENT_TURN: contextvars.ContextVar = contextvars.ContextVar("sim_turn", default=None)
VOICE_TRANSCRIPTS: dict[str, str] = {}      # phone -> what the (fake) speech-to-text hears in the next voice note


# ── database ──────────────────────────────────────────────────────────────────

def create_database() -> None:
    """Drop/create the sim DB and run alembic upgrade head on it."""
    eng = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{SIM_DB}" WITH (FORCE)'))
        conn.execute(sa.text(f'CREATE DATABASE "{SIM_DB}"'))
    eng.dispose()
    res = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=str(BACKEND_DIR),
        env={**os.environ, "DATABASE_URL": ASYNC_URL}, capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RuntimeError("alembic failed:\n" + res.stdout + res.stderr)


# ── captured outbound ─────────────────────────────────────────────────────────

@dataclass
class Sent:
    """One outbound WhatsApp message as the customer would see it."""

    kind: str                       # text | buttons | list | image
    text: str
    buttons: list = field(default_factory=list)   # [(id, title)]
    rows: list = field(default_factory=list)      # [(id, title, description)]
    extra: str = ""


class Outbox:
    """Per-recipient captured sends."""

    def __init__(self) -> None:
        """Empty outbox."""
        self.by_phone: dict[str, list[Sent]] = defaultdict(list)
        self.counter = itertools.count(1)

    def install(self, mp) -> None:
        """Patch the raw Meta send functions (text / buttons / list / image / typing / document)."""
        outbox = self

        async def _text(to_phone_number, message_text, phone_number_id=None, access_token=None):
            to = to_phone_number
            outbox.by_phone[to].append(Sent("text", message_text))
            return {"messages": [{"id": f"wamid.out.{next(outbox.counter)}"}]}

        async def _buttons(to_phone_number, body_text, buttons, phone_number_id=None, access_token=None):
            to = to_phone_number
            outbox.by_phone[to].append(Sent("buttons", body_text, buttons=[
                (b.get("reply", {}).get("id") or b.get("id"), b.get("reply", {}).get("title") or b.get("title")) for b in buttons
            ]))
            return True

        async def _list(to_phone_number, header_text, body_text, button_text, sections, phone_number_id=None, access_token=None):
            to = to_phone_number
            rows = [(r.get("id"), r.get("title"), r.get("description")) for s in sections for r in s.get("rows", [])]
            outbox.by_phone[to].append(Sent("list", body_text, rows=rows, extra=f"{header_text}|{button_text}"))
            return True

        async def _image(to_phone_number, image_url, caption=None, phone_number_id=None, access_token=None):
            to = to_phone_number
            outbox.by_phone[to].append(Sent("image", caption or "", extra=image_url))
            return {"messages": [{"id": f"wamid.outimg.{next(outbox.counter)}"}]}

        async def _ig_dm(ig_user_id, recipient_igsid, message_text):
            outbox.by_phone[recipient_igsid].append(Sent("text", message_text))
            return {"message_id": f"mid.out.{next(outbox.counter)}"}

        async def _ig_quick(ig_user_id, recipient_igsid, message_text, quick_replies):
            outbox.by_phone[recipient_igsid].append(Sent("buttons", message_text, buttons=[(q.get("id") or q.get("payload"), q.get("title")) for q in quick_replies]))
            return True

        async def _ig_template(ig_user_id, recipient_igsid, elements):
            body = "\n".join(f"{e.get('title')} — {e.get('subtitle')}" for e in elements)
            outbox.by_phone[recipient_igsid].append(Sent("list", body, rows=[(
                (e.get("buttons") or [{}])[0].get("payload"), e.get("title"), e.get("subtitle")) for e in elements]))
            return True

        async def _ig_image(ig_user_id, recipient_igsid, image_url):
            outbox.by_phone[recipient_igsid].append(Sent("image", "", extra=image_url))
            return {"message_id": f"mid.outimg.{next(outbox.counter)}"}

        mp.setattr("app.services.instagram_service._raw_send_dm", _ig_dm)
        mp.setattr("app.services.instagram_service._raw_send_quick_replies", _ig_quick)
        mp.setattr("app.services.instagram_service._raw_send_generic_template", _ig_template)
        mp.setattr("app.services.instagram_service._raw_send_image", _ig_image)
        mp.setattr("app.services.whatsapp_service._raw_send_text_message", _text)
        mp.setattr("app.services.whatsapp_service._raw_send_button_message", _buttons)
        mp.setattr("app.services.whatsapp_service._raw_send_list_message", _list)
        mp.setattr("app.services.whatsapp_service._raw_send_image_message", _image)
        mp.setattr("app.services.whatsapp_service._raw_send_typing_indicator", mock.AsyncMock(return_value=None))
        mp.setattr("app.services.whatsapp_service._raw_send_document_message", mock.AsyncMock(return_value=None))


class ErrorCollector(logging.Handler):
    """Collects ERROR+ log records, attributed to the simulated turn that was running."""

    def __init__(self) -> None:
        """No records yet."""
        super().__init__(level=logging.ERROR)
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Store logger, message and (short) traceback with the active turn tag."""
        tag = CURRENT_TURN.get()
        exc = ""
        if record.exc_info:
            import traceback

            exc = "".join(traceback.format_exception(*record.exc_info))[-1500:]
        self.records.append({
            "tag": tag, "logger": record.name, "msg": record.getMessage()[:400], "exc": exc,
        })


# ── app wiring ────────────────────────────────────────────────────────────────

def make_app(mp, fake_llm):
    """Wire the real app to the sim DB with every external dependency stubbed; returns the ASGI app."""
    os.environ["DATABASE_URL"] = ASYNC_URL
    os.environ["ROUTER_V2_CLIENT_IDS"] = ""
    from app.config import get_settings

    get_settings.cache_clear()
    from tests.sim import fake_llm as fl

    fl.install(mp, fake_llm)
    mp.setattr("app.services.order_pipeline.random.uniform", lambda *_a, **_k: 0)
    mp.setattr("app.routers.webhook._RATE_LIMIT_MESSAGES", 10**9)
    mp.setattr("app.services.llm_usage_service._persist_row", mock.AsyncMock())

    async def _download(media_id, *a, **kw):
        from tests.replay.helpers import TINY_PNG

        # Voice notes: the "audio bytes" carry the media id so the fake transcriber can find the scripted words.
        return b"AUDIO:" + str(media_id).encode() if str(media_id).startswith("voice.") else TINY_PNG

    async def _ig_download(url, *a, **kw):
        from tests.replay.helpers import TINY_PNG

        return b"AUDIO:" + str(url).encode() if "voice." in str(url) else TINY_PNG

    async def _transcribe(audio_bytes, filename="audio.ogg", *a, **kw):
        phone = audio_bytes.decode(errors="ignore").split("voice.", 1)[-1]
        return VOICE_TRANSCRIPTS.get(phone, "")

    counter = itertools.count(1)

    async def _store(client_id, data, content_type, folder="chat"):
        return f"https://cdn.test/{folder}/{next(counter)}.png"

    mp.setattr("app.services.vision_service.download_whatsapp_media", _download)
    mp.setattr("app.services.vision_service.download_instagram_media", _ig_download)
    mp.setattr("app.services.voice_service.transcribe_voice_note", _transcribe)
    mp.setattr("app.services.media_service.store_media", _store)
    mp.setattr("app.services.ocr_service.detect_sku_from_image", lambda *_: None)
    mp.setattr("app.services.llm_usage_service._persist_row", mock.AsyncMock())
    from app.main import app

    return app


# ── seeding ───────────────────────────────────────────────────────────────────

CATALOGUE = [
    # sku, name, price, category, stock, variants[(color,size,stock,price)]
    ("SR20001", "Green Cotton Saree", 899.0, "Saree", 0, [("Green", None, 10**6, 899.0)]),
    ("SR20002", "Red Silk Saree", 2499.0, "Saree", 0, [("Red", None, 10**6, 2499.0), ("Pink", None, 10**6, 2499.0)]),
    ("SR20003", "Blue Cotton Saree", 950.0, "Saree", 0, [("Blue", None, 0, 950.0)]),          # sold out
    ("LH10042", "Cotton Lehenga", 500.0, "Lehenga", 0, [("Pink", "M", 10**6, 500.0), ("Pink", "XXL", 0, 500.0), ("Blue", "M", 10**6, 500.0), ("Blue", "XXL", 10**6, 500.0)]),
    ("KU30001", "Printed Kurti", 450.0, "Kurti", 10**6, []),
    ("KU30002", "Cotton Kurta", 650.0, "Kurti", 3, []),               # tiny stock: only used to ask for too many pieces
    ("CC40001", "Navratri Chaniya Choli", 1800.0, "Chaniya Choli", 0, [("Red", "M", 10**6, 1800.0), ("Green", "M", 10**6, 1800.0), ("Pink", "L", 10**6, 1800.0)]),
]


async def seed(session_factory) -> dict:
    """Create the two shops (router ON / OFF) with the shared catalogue; returns {kind: client_id}."""
    from app.models.client import Client
    from app.models.product import Product
    from app.models.product_variant import ProductVariant

    ids = {}
    async with session_factory() as session:
        for kind, flag in (("router", True), ("legacy", False), ("ig", True)):
            client = Client(
                business_name=f"Sim {kind} Store", email=f"sim_{kind}@test.com", phone=f"9100000{['router', 'legacy', 'ig'].index(kind) + 1}00",
                whatsapp_phone_number_id=PNIDS[kind], accepts_cod=True, accepts_upi=True, upi_id="shop@upi",
                hashed_password="x", delivery_days_min=3, delivery_days_max=7, router_v2_enabled=flag,
                catalogue_slug=f"sim-{kind}", plan_slug="growth" if kind == "ig" else "starter",
                **({"instagram_account_id": IG_ACCOUNT} if kind == "ig" else {}),
            )
            session.add(client)
            await session.flush()
            for sku, name, price, category, stock, variants in CATALOGUE:
                product = Product(
                    client_id=client.id, name=name, sku=f"{sku}", price=price, stock=sum(v[2] for v in variants) or stock,
                    is_active=True, has_variants=bool(variants), category=category,
                )
                session.add(product)
                await session.flush()
                for color, size, vstock, vprice in variants:
                    session.add(ProductVariant(product_id=product.id, client_id=client.id, color=color, size=size,
                                               stock=vstock, is_active=True, price=vprice))
            ids[kind] = client.id
        await session.commit()
    return ids


# ── conversation session ──────────────────────────────────────────────────────

def _sign(body: bytes) -> str:
    """X-Hub-Signature-256 for the webhook."""
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _payload(sender: str, pnid: str, message: dict) -> bytes:
    """Wrap one WhatsApp message in the webhook envelope."""
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "E", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp", "metadata": {"display_phone_number": "0", "phone_number_id": pnid},
            "contacts": [{"profile": {"name": "Sim"}, "wa_id": sender}], "messages": [message],
        }}]}],
    }).encode()


def _ig_payload(sender: str, message: dict) -> bytes:
    """Wrap one message in the Instagram DM webhook envelope (text, image attachment, or audio attachment)."""
    mid = message["id"]
    if message["type"] == "text":
        ig_message = {"mid": mid, "text": message["text"]["body"]}
    elif message["type"] == "image":
        ig_message = {"mid": mid, "attachments": [{"type": "image", "payload": {"url": f"https://cdn.test/ig/{sender}.png"}}]}
    elif message["type"] == "audio":
        ig_message = {"mid": mid, "attachments": [{"type": "audio", "payload": {"url": f"https://cdn.test/ig/voice.{sender}"}}]}
    else:
        raise ValueError(f"unsupported IG message type {message['type']}")
    return json.dumps({"object": "instagram", "entry": [{"id": IG_ACCOUNT, "time": int(time.time()), "messaging": [{
        "sender": {"id": sender}, "recipient": {"id": IG_ACCOUNT}, "timestamp": int(time.time() * 1000), "message": ig_message,
    }]}]}).encode()


@dataclass
class TurnRecord:
    """One customer message and everything the bot answered."""

    sent: str
    kind: str
    lang: str | None
    replies: list[Sent]
    status: int
    ms: int
    stage_after: str = ""


@dataclass
class Finding:
    """One defect observed in a conversation."""

    code: str
    detail: str
    flow: str
    lang: str
    shop: str
    turn: int
    customer: str = ""
    bot: str = ""
    phone: str = ""


class Session:
    """One simulated customer talking to one shop."""

    _wamid = itertools.count(1)

    def __init__(self, http, session_factory, shop: str, client_id: int, phone: str, lang: str, flow: str) -> None:
        """Bind to the shop and customer; `lang` is the language the customer writes in."""
        self.http, self.sf, self.shop, self.client_id = http, session_factory, shop, client_id
        self.phone, self.lang, self.flow = phone, lang, flow
        self.pnid = PNIDS[shop]
        self.turns: list[TurnRecord] = []
        self.findings: list[Finding] = []
        self.established: str | None = None   # language the bot is expected to answer in for no-signal messages (None until the customer has written something)
        self.outbox: Outbox | None = None

    # -- low level -------------------------------------------------------------
    async def _post(self, message: dict, label: str, lang: str | None, *, expect_reply: bool = True) -> TurnRecord:
        """POST a message, collect the replies, record the turn."""
        wamid = f"wamid.sim.{self.phone}.{next(self._wamid)}"
        message = {"from": self.phone, "id": wamid, "timestamp": str(int(time.time())), **message}
        if self.shop == "ig":
            path, body = "/instagram", _ig_payload(self.phone, message)
        else:
            path, body = "/webhook", _payload(self.phone, self.pnid, message)
        before = len(self.outbox.by_phone[self.phone])
        token = CURRENT_TURN.set(f"{self.flow}|{self.shop}|{self.lang}|{self.phone}|t{len(self.turns)}")
        t0 = time.perf_counter()
        try:
            resp = await self.http.post(path, content=body, headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"})
        finally:
            CURRENT_TURN.reset(token)
        ms = int((time.perf_counter() - t0) * 1000)
        replies = list(self.outbox.by_phone[self.phone][before:])
        rec = TurnRecord(label, message["type"], lang, replies, resp.status_code, ms)
        self.turns.append(rec)
        if resp.status_code != 200:
            self.flag("http_error", f"status {resp.status_code}: {resp.text[:200]}", rec)
        if expect_reply and not replies and resp.status_code == 200:
            self.flag("no_reply", "bot sent nothing", rec)
        return rec

    def flag(self, code: str, detail: str, rec: TurnRecord | None = None) -> None:
        """Record a finding against the latest (or given) turn."""
        rec = rec or (self.turns[-1] if self.turns else None)
        self.findings.append(Finding(
            code, detail[:600], self.flow, self.lang, self.shop, len(self.turns) - 1,
            customer=(rec.sent if rec else "")[:200], bot=" ⏎ ".join(r.text for r in (rec.replies if rec else []))[:600],
            phone=self.phone,
        ))

    # -- customer actions ------------------------------------------------------
    async def say(self, text: str, lang: str | None = None, *, expect_reply: bool = True) -> TurnRecord:
        """Send a text message. `lang` = language of the message (None = no language signal, e.g. '2')."""
        if lang:
            self.established = lang
        rec = await self._post({"type": "text", "text": {"body": text}}, text, lang or self.established, expect_reply=expect_reply)
        from tests.sim.checks import check_turn

        check_turn(self, rec, lang or self.established)
        return rec

    async def tap(self, button_id: str, title: str) -> TurnRecord:
        """Tap a quick-reply button (on Instagram the chip arrives as a text message carrying its title)."""
        if self.shop == "ig":
            return await self.say(title, None)
        rec = await self._post({"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": title}}},
                               f"[button:{button_id}]", self.established)
        from tests.sim.checks import check_turn

        check_turn(self, rec, self.established)
        return rec

    async def pick(self, row_id: str, title: str) -> TurnRecord:
        """Select a list row (on Instagram the carousel button sends its payload — the SKU — as text)."""
        if self.shop == "ig":
            return await self.say((row_id or title).split("~")[0], None)
        rec = await self._post({"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": row_id, "title": title}}},
                               f"[list:{row_id}]", self.established)
        from tests.sim.checks import check_turn

        check_turn(self, rec, self.established)
        return rec

    async def voice(self, spoken: str, lang: str | None = None) -> TurnRecord:
        """Send a voice note whose transcription is `spoken` (the fake STT returns it verbatim)."""
        if lang:
            self.established = lang
        VOICE_TRANSCRIPTS[self.phone] = spoken
        rec = await self._post({"type": "audio", "audio": {"id": f"voice.{self.phone}", "mime_type": "audio/ogg"}},
                               f"[voice: {spoken}]", lang or self.established)
        from tests.sim.checks import check_turn

        check_turn(self, rec, lang or self.established)
        return rec

    async def image(self) -> TurnRecord:
        """Send an image (payment screenshot)."""
        rec = await self._post({"type": "image", "image": {"id": f"media.{self.phone}", "mime_type": "image/png"}}, "[image]", self.established)
        from tests.sim.checks import check_turn

        check_turn(self, rec, self.established)
        return rec

    # -- state -----------------------------------------------------------------
    async def conv(self):
        """Fresh Conversation row (or None)."""
        from app.models.conversation import Conversation

        async with self.sf() as db:
            return (await db.execute(sa.select(Conversation).where(
                Conversation.client_id == self.client_id, Conversation.phone_number == self.phone))).scalar_one_or_none()

    async def orders(self) -> list:
        """The customer's orders, oldest first."""
        from app.models.order import Order

        async with self.sf() as db:
            return list((await db.execute(sa.select(Order).where(
                Order.client_id == self.client_id, Order.customer_phone == self.phone).order_by(Order.id))).scalars().all())

    def last_buttons(self) -> list:
        """Buttons attached to the most recent bot message (id, title)."""
        for rec in reversed(self.turns):
            for sent in reversed(rec.replies):
                if sent.buttons:
                    return sent.buttons
            if rec.replies:
                return []
        return []

    def last_text(self) -> str:
        """All text of the latest turn's replies."""
        return "\n".join(r.text for r in (self.turns[-1].replies if self.turns else []))
