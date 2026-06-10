"""
Sandbox router — lets a logged-in client test their AI agent without sending
real WhatsApp messages.

Endpoints:
- POST /sandbox/message  : process a message through the full AI pipeline and
                           return the reply (no WhatsApp send)
- GET  /sandbox/reset    : wipe the sandbox conversation for a fresh start
"""

import logging
import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.client import Client
from app.routers.auth import get_current_client
from app.services import (
    catalogue_service,
    conversation_service,
    customer_service,
    gemini_service,
    intent_service,
    lead_service,
)
from app.services import conversation_flow, language_service as _lang_svc
from app.services.language_templates import get_template as _get_tpl

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sandbox", tags=["sandbox"])

_SANDBOX_CHANNEL = "sandbox"


class SandboxMessageRequest(BaseModel):
    """Body for POST /sandbox/message."""

    message: str
    phone: str = "test_user"


class SandboxMessageResponse(BaseModel):
    """Full pipeline result returned to the dashboard."""

    reply: str
    language_detected: str
    stage: str
    lead_status: str
    products_matched: list[str]
    response_time_ms: int


def _sandbox_phone(client_id: int) -> str:
    """Unique sandbox phone key per client so different clients don't share history."""
    return f"sandbox_{client_id}"


@router.post("/message", response_model=SandboxMessageResponse)
async def sandbox_message(
    body: SandboxMessageRequest,
    db: AsyncSession = Depends(get_db),
    client: Client = Depends(get_current_client),
) -> SandboxMessageResponse:
    """
    Process a test message through the full AI pipeline and return the reply.

    The message is never sent to WhatsApp — this is a safe sandbox environment
    for clients to preview how their agent responds before going live.

    Args:
        body:   The test message and optional phone alias.
        db:     Injected async DB session.
        client: Authenticated client from JWT.

    Returns:
        AI reply plus detected language, conversation stage, lead status,
        matched product names, and wall-clock response time.
    """
    start_ms = time.monotonic()
    user_text = body.message
    phone_key = _sandbox_phone(client.id)

    # ── Conversation (sandbox-flagged) ────────────────────────────────────────
    conv = await conversation_service.get_or_create_conversation(
        db, phone_key, channel=_SANDBOX_CHANNEL, is_sandbox=True
    )

    history = await conversation_service.get_history(db, conv.id)
    history_dicts = [{"role": m.role, "content": m.content} for m in history]

    # ── Catalogue context ─────────────────────────────────────────────────────
    products_matched: list[str] = []
    catalogue_context: str | None = None
    skus = catalogue_service.extract_skus_from_text(user_text)
    if skus:
        sku_products = []
        for sku in skus:
            p = await catalogue_service.find_product_by_sku(db, client.id, sku)
            if p:
                sku_products.append(p)
        if sku_products:
            catalogue_context = catalogue_service.format_catalogue_context(sku_products)
            products_matched = [p.name for p in sku_products]

    if not catalogue_context:
        all_products = await catalogue_service.list_products(db, client.id)
        relevant = catalogue_service.search_products(all_products, user_text)
        if relevant:
            catalogue_context = catalogue_service.format_catalogue_context(relevant)
            products_matched = [p.name for p in relevant]

    # ── Intent / language / stage ─────────────────────────────────────────────
    intent_service.detect_intent(user_text)
    previous_language = getattr(conv, "last_customer_language", None) or "english"
    language = _lang_svc.detect_language(user_text, previous_language=previous_language)
    # Pass stored_stage so payment/completed are never reversed.
    stage = conversation_flow.detect_stage(
        history_dicts, user_text, stored_stage=conv.current_stage
    )

    # ── Resolve pinned product and variant info ───────────────────────────────
    pinned_product = None
    variant_info: dict = {
        "has_variants": False, "needs_color": False, "needs_size": False,
        "available_colors": [], "available_sizes": [],
    }
    try:
        pinned_sku = getattr(conv, "pending_product_sku", None)
        if not pinned_sku:
            # Pin from first SKU found in this message
            skus_in_msg = catalogue_service.extract_skus_from_text(user_text)
            if skus_in_msg:
                p = await catalogue_service.find_product_by_sku(db, client.id, skus_in_msg[0])
                if p:
                    await conversation_service.update_order_field(
                        db, conv.id, "pending_product_sku", skus_in_msg[0]
                    )
                    conv.pending_product_sku = skus_in_msg[0]
                    pinned_sku = skus_in_msg[0]
        if pinned_sku:
            pinned_product = await catalogue_service.find_product_by_sku(db, client.id, pinned_sku)
            if pinned_product:
                variant_info = await catalogue_service.get_product_variant_info(db, pinned_product)
    except Exception as exc:
        logger.warning("Sandbox SKU/variant resolution error: %s", exc)

    # ── Extract and persist order fields during order_collection ─────────────
    if stage == "order_collection":
        try:
            extracted = conversation_flow.extract_order_field(conv, user_text, variant_info=variant_info)
            if extracted:
                field, value = extracted
                await conversation_service.update_order_field(db, conv.id, field, value)
                setattr(conv, field, value)
        except Exception as exc:
            logger.warning("Sandbox order field extraction error: %s", exc)

    # ── Build system prompt ───────────────────────────────────────────────────
    catalogue_products: list = []
    try:
        catalogue_products = await catalogue_service.list_products(db, client.id)
    except Exception:
        pass

    customer_profile = None
    try:
        customer_profile = await customer_service.get_customer(
            db, client_id=client.id, phone=phone_key
        )
    except Exception:
        pass

    system_prompt = gemini_service.build_master_system_prompt(
        client=client,
        products=catalogue_products,
        conversation_stage=stage,
        language=language,
        customer_history=None,
        conversation=conv,
        customer_profile=customer_profile,
        accepts_cod=getattr(client, "accepts_cod", False),
        variant_info=variant_info,
    )

    # ── Generate AI reply ─────────────────────────────────────────────────────
    try:
        ai_reply = await gemini_service.generate_reply(
            user_text,
            history=history_dicts,
            system_prompt=system_prompt,
            catalogue_context=catalogue_context,
            language=language,
        )
    except Exception as exc:
        logger.error("Sandbox AI error: %s", exc)
        ai_reply = "Sorry, I couldn't generate a reply. Please try again."

    # ── Issue 4 fix: template override for payment/confirmation messages ──────
    _tpl_lang = getattr(conv, "last_customer_language", None) or language or "english"
    _tpl_order_total: float = 0
    if pinned_product and getattr(conv, "pending_order_quantity", None):
        _tpl_order_total = (
            (getattr(pinned_product, "price", 0) or 0)
            * (conv.pending_order_quantity or 0)
        )

    if stage == "payment" and conv.current_stage != "payment":
        _upi_val = getattr(client, "upi_id", None)
        if _upi_val and _tpl_order_total > 0:
            _tpl_msg = _get_tpl(
                _tpl_lang, "ask_payment",
                amount=f"₹{int(_tpl_order_total):,}", upi_id=_upi_val,
            )
            if _tpl_msg:
                ai_reply = _tpl_msg
    elif stage == "completed" and conv.current_stage == "payment":
        _qty_val = getattr(conv, "pending_order_quantity", 1) or 1
        _prod_name = (
            getattr(pinned_product, "name", None) if pinned_product else None
        ) or getattr(conv, "pending_product_sku", "product") or "product"
        _amt_str = f"{int(_tpl_order_total):,}" if _tpl_order_total > 0 else "—"
        _tpl_msg = _get_tpl(
            _tpl_lang, "order_confirmed",
            qty=_qty_val, product=_prod_name, total=_amt_str,
        )
        if _tpl_msg:
            ai_reply = _tpl_msg

    # ── Persist messages ──────────────────────────────────────────────────────
    await conversation_service.save_message(db, conv.id, "user", user_text)
    await conversation_service.save_message(db, conv.id, "assistant", ai_reply)

    try:
        await conversation_service.update_stage(db, conv.id, stage)
    except Exception:
        pass

    if not _lang_svc.is_ambiguous(user_text):
        try:
            await conversation_service.update_language(db, conv.id, language)
        except Exception:
            pass

    # ── Lead classification (sandbox only — no DB lead row created) ───────────
    all_messages = history_dicts + [
        {"role": "user", "content": user_text},
        {"role": "model", "content": ai_reply},
    ]
    lead_status = lead_service._classify(all_messages)  # noqa: SLF001

    # Auto-advance onboarding to "agent tested" (step 5) on first sandbox use
    if client.onboarding_step < 5:
        client.onboarding_step = 5
        await db.commit()

    elapsed_ms = int((time.monotonic() - start_ms) * 1000)

    return SandboxMessageResponse(
        reply=ai_reply,
        language_detected=language,
        stage=stage,
        lead_status=lead_status,
        products_matched=products_matched,
        response_time_ms=elapsed_ms,
    )


@router.get("/reset")
async def sandbox_reset(
    db: AsyncSession = Depends(get_db),
    client: Client = Depends(get_current_client),
) -> dict:
    """
    Delete the sandbox conversation history for the authenticated client.

    This gives a clean slate for testing without affecting real analytics.

    Args:
        db:     Injected async DB session.
        client: Authenticated client from JWT.

    Returns:
        {"status": "reset"} on success.
    """
    phone_key = _sandbox_phone(client.id)
    await conversation_service.delete_sandbox_conversation(
        db, phone_key, channel=_SANDBOX_CHANNEL
    )
    return {"status": "reset"}
