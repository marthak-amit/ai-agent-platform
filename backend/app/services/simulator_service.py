"""
In-dashboard chat simulator: the owner plays a customer against the REAL engine, with nothing leaving the server.

It is not a second implementation. A turn builds the same `InboundContext` the WhatsApp webhook builds and calls
`order_pipeline.handle_inbound_message` — so the LLM intent router (when the shop is on it) or the keyword flow, the
slot machine, side questions, the spam guard, language mirroring and the order engine all run exactly as they do for a
real customer, against the tenant's real catalogue/knowledge base/payment settings. The only differences are the fences:

  * the customer is the fake `sandbox_<client_id>` on a conversation flagged `is_sandbox` (channel "whatsapp", so the
    engine behaves like WhatsApp: buttons, lists, templates);
  * the reply is RETURNED as data, never handed to a channel adapter, and `outbound` refuses the sandbox phone anyway;
  * never counted: no conversation-window usage (billing/trial), no legacy usage counters, no LLM budget caps, no
    leads, no inbox notifications, no owner alerts (see order_pipeline._billing_preflight and friends);
  * orders it creates are `Order.is_sandbox` (own SIM-… numbers; no stock reserved or deducted; not listed or counted);
  * it works before go-live (that is its purpose) and is unlimited unless SIMULATOR_DAILY_MESSAGE_LIMIT is set.

LLM tokens ARE spent (it is the real engine) and show in the owner's LLM usage report.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.client import Client
from app.models.conversation import Conversation
from app.models.cost_log import CostLogEntry
from app.models.customer import Customer
from app.models.follow_up import FollowUp
from app.models.lead import Lead
from app.models.message import Message
from app.models.order import Order
from app.models.payment_proof import PaymentProof
from app.models.product import Product
from app.schemas.simulator import (
    SimBubbleOut,
    SimButtonOut,
    SimListRowOut,
    SimulatorReplyOut,
    SimulatorSessionOut,
    SimulatorTurnOut,
)
from app.schemas.webhook import ButtonReply, InteractiveContent, TextContent, WhatsAppMessage
from app.services import conversation_service, intent_router, llm_health
from app.services.order_pipeline import (
    InboundContext,
    PipelineResult,
    _decode_btn,
    _encode_btn,
    _rotate_nonce,
    ensure_reply,
    handle_inbound_message,
)
from app.services.sandbox_guard import sandbox_phone

logger = logging.getLogger(__name__)

SIM_CHANNEL = "whatsapp"
#: Customer turns the simulator must have answered before the checklist item "Test your agent" is done.
TEST_PASS_TURNS = 3
#: Skips the pipeline's artificial human-like reply delay (seconds credited against it).
_NO_DELAY_CREDIT = 3600.0

_locks: dict[int, asyncio.Lock] = {}


def _lock_for(client_id: int) -> asyncio.Lock:
    """One lock per tenant: two simultaneous simulator turns would race on the same conversation row."""
    return _locks.setdefault(client_id, asyncio.Lock())


class SimulatorLimitError(Exception):
    """The configured daily simulator message limit has been reached."""


async def _download_unsupported(_source: object) -> bytes:
    """The simulator has no media: images/voice notes are not offered, so this is never called."""
    raise RuntimeError("media is not supported in the simulator")


def interactive_to_text(kind: str, tapped_id: str, title: str | None) -> tuple[str, tuple[str, str, str] | None]:
    """
    Map a tapped button / list row to the text the pipeline sees, exactly as routers/webhook.py does for WhatsApp.

    Returns (user_text, decoded_nonce_tuple_or_None). The nonce check then rejects stale taps like in production.
    """
    from app.services.catalogue_service import SKU_PATTERN

    raw = (tapped_id or "").lower()
    decoded = _decode_btn(raw)
    if kind == "button":
        action = decoded[0] if decoded else raw
        if action == "confirm_pay":
            text = "confirm"
        elif action in ("cancel_order", "confirm_no"):
            text = "cancel"
        elif action == "paid_done":
            text = "paid"
        elif action == "offer_yes":
            text = "yes"
        elif action == "offer_no":
            text = "no"
        elif action in ("cancel_yes", "cancel_no"):
            text = "yes" if action == "cancel_yes" else "no"
        elif action == "switch_product":
            text = "switch"
        elif action == "add_both":
            text = "add both"
        elif action == "keep_current":
            text = "keep current"
        elif action == "add_crosssell":
            text = "2"
        elif action == "addr_change":
            text = "change"
        elif action in ("upi", "cod", "bank_transfer"):
            text = {"upi": "UPI", "cod": "COD", "bank_transfer": "bank transfer"}[action]
        elif SKU_PATTERN.fullmatch(action.upper()):
            text = action.upper()
        else:
            text = title or ""
        return text, decoded
    # list row
    action = (decoded[0] if decoded else raw).upper()
    text = action if SKU_PATTERN.fullmatch(action) else (title or "")
    return text, decoded


def _message(client_id: int, text: str, interactive: InteractiveContent | None) -> WhatsAppMessage:
    """Build the inbound message object the pipeline expects (the same model the WhatsApp webhook parses)."""
    wamid = f"sim.{client_id}.{int(time.time() * 1000)}"
    common = dict(id=wamid, timestamp=str(int(time.time())), **{"from": sandbox_phone(client_id)})
    if interactive is not None:
        return WhatsAppMessage(type="interactive", interactive=interactive, **common)
    return WhatsAppMessage(type="text", text=TextContent(body=text), **common)


async def get_conversation(db: AsyncSession, client: Client, create: bool = False) -> Conversation | None:
    """The tenant's simulator conversation (one per client), optionally creating it."""
    if create:
        return await conversation_service.get_or_create_conversation(
            db, sandbox_phone(client.id), channel=SIM_CHANNEL, is_sandbox=True, client_id=client.id
        )
    return await db.scalar(
        select(Conversation).where(
            Conversation.client_id == client.id, Conversation.phone_number == sandbox_phone(client.id),
            Conversation.channel == SIM_CHANNEL, Conversation.is_sandbox.is_(True),
        )
    )


async def _sent_today(db: AsyncSession, client_id: int) -> int:
    """Customer messages the simulator received today (UTC) — for the optional daily limit."""
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(await db.scalar(
        select(func.count()).select_from(Message).join(Conversation, Conversation.id == Message.conversation_id).where(
            Conversation.client_id == client_id, Conversation.is_sandbox.is_(True),
            Message.role == "user", Message.created_at >= midnight,
        )
    ) or 0)


async def _customer_turns(db: AsyncSession, conv_id: int) -> int:
    """Customer messages in the simulator conversation."""
    return int(await db.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == conv_id, Message.role == "user")
    ) or 0)


async def _bubbles(result: PipelineResult, *, db: AsyncSession, conv: Conversation) -> list[SimBubbleOut]:
    """
    Turn a PipelineResult into the bubbles a customer would receive, in the adapter's delivery order:
    pre-images, pre-texts, the main text (+ buttons / list, with nonce-encoded ids like production), then images.
    """
    out: list[SimBubbleOut] = []
    for url, caption in result.pre_images or []:
        out.append(SimBubbleOut(kind="image", image_url=url, text=caption))
    for pre in result.pre_texts or []:
        out.append(SimBubbleOut(text=pre))
    nonce = await _rotate_nonce(db, conv.id, conv) if (result.buttons or result.list_options) else None

    def enc(action: str) -> str:
        """Encode a button / row id exactly like the WhatsApp adapter (stale-tap protection included)."""
        return _encode_btn(action, conv.id, nonce) if nonce else action

    if result.text or result.buttons or result.list_options:
        main = SimBubbleOut(text=result.text)
        if result.buttons:
            main.buttons = [SimButtonOut(id=enc(b.id), title=b.title) for b in result.buttons]
        elif result.list_options:
            main.list_header = result.list_header
            main.list_button_text = result.list_button_text
            main.list_rows = [SimListRowOut(id=enc(r.id), title=r.title, description=r.description) for r in result.list_options]
        out.append(main)
    for url, caption in result.images or []:
        out.append(SimBubbleOut(kind="image", image_url=url, text=caption))
    return out


async def run_turn(
    db: AsyncSession,
    client: Client,
    *,
    text: str | None = None,
    button_id: str | None = None,
    list_row_id: str | None = None,
    title: str | None = None,
) -> SimulatorReplyOut:
    """
    Play one customer turn through the real engine and return what the customer would have received.

    Raises:
        SimulatorLimitError: SIMULATOR_DAILY_MESSAGE_LIMIT (when > 0) is used up for today.
    """
    limit = int(get_settings().simulator_daily_message_limit)
    if limit and await _sent_today(db, client.id) >= limit:
        raise SimulatorLimitError(f"Simulator limit of {limit} messages per day reached.")

    async with _lock_for(client.id):
        started = time.monotonic()
        conv = await get_conversation(db, client, create=True)
        nonce_parsed = None
        interactive = None
        if button_id is not None:
            user_text, nonce_parsed = interactive_to_text("button", button_id, title)
            interactive = InteractiveContent(type="button_reply", button_reply=ButtonReply(id=button_id, title=title or ""))
        elif list_row_id is not None:
            user_text, nonce_parsed = interactive_to_text("list", list_row_id, title)
            interactive = InteractiveContent(type="list_reply", list_reply=ButtonReply(id=list_row_id, title=title or ""))
        else:
            user_text = text or ""
        message = _message(client.id, user_text, interactive)

        ctx = InboundContext(
            db=db, client=client, conv=conv, sender_phone=sandbox_phone(client.id), message=message,
            user_text=user_text, wamid=message.id, btn_nonce_parsed=nonce_parsed,
            download_media=_download_unsupported, is_whatsapp=True, reply_delay_credit=_NO_DELAY_CREDIT,
        )
        try:
            result = await handle_inbound_message(ctx)
        except Exception:
            logger.exception("simulator: pipeline crashed conv=%s — answering with the deterministic fallback", conv.id)
            try:
                await db.rollback()
                await db.refresh(conv)
                await db.refresh(client)
            except Exception:
                pass
            result = None
        result = await ensure_reply(result, ctx)

        bubbles = await _bubbles(result, db=db, conv=conv)
        await db.refresh(conv)
        passed = await _mark_tested_if_ready(db, client, conv)
        return SimulatorReplyOut(
            bubbles=bubbles,
            stage=conv.current_stage or "greeting",
            language=getattr(conv, "last_customer_language", None),
            response_time_ms=int((time.monotonic() - started) * 1000),
            test_passed=passed,
            llm_available=llm_health.is_llm_available(),
            silent=not bubbles,
        )


async def _mark_tested_if_ready(db: AsyncSession, client: Client, conv: Conversation) -> bool:
    """
    Stamp `agent_tested_at` once the simulator has had TEST_PASS_TURNS customer turns while the LLM was healthy.

    A turn answered only because the LLM is down proves nothing about the agent, so it never completes the test.
    Returns whether the test is passed.
    """
    if client.agent_tested_at is not None:
        return True
    if not llm_health.is_llm_available():
        return False
    if await _customer_turns(db, conv.id) < TEST_PASS_TURNS:
        return False
    client.agent_tested_at = datetime.now(timezone.utc)
    await db.commit()
    return True


async def session(db: AsyncSession, client: Client) -> SimulatorSessionOut:
    """The current simulator transcript and settings (so the page can resume after a reload)."""
    conv = await get_conversation(db, client)
    turns: list[SimulatorTurnOut] = []
    if conv is not None:
        rows = (await db.execute(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.id)
        )).scalars().all()
        turns = [
            SimulatorTurnOut(role="customer" if m.role == "user" else "bot", text=m.content, created_at=m.created_at)
            for m in rows if m.role in ("user", "assistant", "model")
        ]
    samples = int(await db.scalar(
        select(func.count()).select_from(Product).where(Product.client_id == client.id, Product.is_demo.is_(True))
    ) or 0)
    return SimulatorSessionOut(
        messages=turns,
        stage=getattr(conv, "current_stage", None),
        test_passed=client.agent_tested_at is not None,
        llm_available=llm_health.is_llm_available(),
        turns_required=TEST_PASS_TURNS,
        router_enabled=bool(intent_router.router_enabled(client)),
        sample_products=samples,
        daily_limit=int(get_settings().simulator_daily_message_limit),
        sent_today=await _sent_today(db, client.id),
    )


async def reset(db: AsyncSession, client: Client) -> int:
    """
    Wipe the simulator chat so the owner can start over: the conversation, its messages, and everything the engine
    created for it (orders with their line items/audit rows, the fake customer, cost-log rows). Returns orders removed.
    """
    async with _lock_for(client.id):
        conv = await get_conversation(db, client)
        removed = 0
        if conv is not None:
            order_ids = (await db.execute(select(Order.id).where(Order.conversation_id == conv.id))).scalars().all()
            removed = len(order_ids)
            lead_ids = (await db.execute(select(Lead.id).where(Lead.conversation_id == conv.id))).scalars().all()
            if lead_ids:
                await db.execute(delete(FollowUp).where(FollowUp.lead_id.in_(lead_ids)))
                await db.execute(delete(Lead).where(Lead.id.in_(lead_ids)))
            await db.execute(delete(PaymentProof).where(PaymentProof.conversation_id == conv.id))
            await db.execute(delete(Order).where(Order.conversation_id == conv.id))      # children cascade in the DB
            await db.execute(delete(CostLogEntry).where(CostLogEntry.conversation_id == conv.id))
            await db.execute(delete(Message).where(Message.conversation_id == conv.id))
            await db.execute(delete(Conversation).where(Conversation.id == conv.id))
        await db.execute(delete(Customer).where(Customer.client_id == client.id, Customer.phone == sandbox_phone(client.id)))
        await db.commit()
        return removed
