"""
Agent v3 — one customer turn: Cart + Planner + Validator, behind the per-client flag ``Client.agent_v3_enabled``.

    message ──► fast path (₹0: button / number / exact option / gate word / side question / SKU)
            └─► planner (ONE structured-output LLM call) ──► closed-set actions
                       │                                          │
                       ▼                                          ▼
              executor: validate against the DB, apply to the cart copy, ≤ 2 repair rounds with the planner,
              then a deterministic clarify built from REAL options
                       │
                       ▼
              invariants (grounded values, CTA, no unverified amounts, pending order acknowledged once, stage kept)
                       │
                       ▼
              renderer (templates + DB rows)  ──►  PipelineResult  ──►  the channel adapter

Strangler-fig: ``order_pipeline._handle_inbound_message_core`` hands the turn to ``handle_turn`` right after the early guards
(spam / blocklist / takeover / button nonce …) for clients where ``enabled(client)`` is true; everyone else runs the old
router / slot machine untouched. The cart lives in ``conversation_cart``; a few legacy ``conversations`` columns (stage,
name, address, mobile, payment, pinned SKU) are mirrored so dashboards, follow-ups and leads keep working.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select

from app.config import get_settings
from app.schemas.agent_actions import PlannerOutput, SearchCatalog, ShowProduct
from app.services import agent_cart, agent_fast_paths, agent_invariants as inv, agent_order_edit, agent_planner, agent_text, conversation_service, llm_usage_service, turn_trace
from app.services import agent_executor as ex
from app.services import language_service as _lang_svc
from app.services import language_tracker
from app.services import agent_renderer
from app.services.agent_catalog import Catalogue
from app.services.agent_renderer import Renderer, Reply

logger = logging.getLogger("app.services.agent_v3")

_AMOUNT_RE = re.compile(r"₹\s?([\d,]+)")


def enabled(client) -> bool:
    """
    True when agent v3 runs for this client.

    ``Client.agent_v3_enabled`` (True/False) wins; NULL follows ``AGENT_V3_CLIENT_IDS`` ("1" by default; "*" = every client;
    empty = none).
    """
    if client is None:
        return False
    override = getattr(client, "agent_v3_enabled", None)
    if isinstance(override, bool):
        return override
    raw = (get_settings().agent_v3_client_ids or "").strip()
    if raw == "*":
        return True
    ids = {int(tok) for tok in re.split(r"[,\s]+", raw) if tok.isdigit()}
    return getattr(client, "id", None) in ids


async def _pending_orders(db, client, conv, sender_phone: str) -> list:
    """Orders of this customer that still need money or a verification (pending_payment / payment_submitted)."""
    from app.models.order import Order
    from app.services.order_state_machine import PAYMENT_OPEN_STATES

    res = await db.execute(
        select(Order).where(
            Order.client_id == client.id, Order.status.in_(PAYMENT_OPEN_STATES),
            or_(Order.conversation_id == conv.id, Order.customer_phone == sender_phone),
        ).order_by(Order.id.desc()).limit(3)
    )
    return list(res.scalars().all())


def _to_pipeline_result(reply: Reply):
    """``Reply`` → the channel-neutral ``PipelineResult`` (raw action ids; the adapter adds nonces)."""
    from app.services.order_pipeline import ButtonSpec, ListOptionSpec, PipelineResult

    return PipelineResult(
        text=reply.text,
        buttons=[ButtonSpec(i, t) for i, t in reply.buttons] if reply.buttons else None,
        list_options=[ListOptionSpec(i, t, d) for i, t, d in reply.list_rows] if reply.list_rows else None,
        list_header=reply.list_header, list_button_text=reply.list_button_text,
        images=list(reply.images) if reply.images else None, image_fallback_text=reply.image_fallback_text,
        skip_localizer=reply.localized,
    )


async def _product_images(client, view) -> list[tuple[str, str]]:
    """Up to ``agent_max_images`` photos of a product — its own, then its variants' — each with the SKU watermark."""
    from types import SimpleNamespace

    from app.services import order_pipeline as op

    if view is None:
        return []
    candidates = [(view.product.image_url, view.product)] + [
        (v.image_url, SimpleNamespace(image_url=v.image_url, sku=view.sku)) for v in view.variants if v.image_url
    ]
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url, holder in candidates:
        if not url or url in seen or not op._is_valid_image_url(url):
            continue
        seen.add(url)
        sent_url = await op.watermarked_product_image_url(client, holder)
        if sent_url:
            out.append((sent_url, view.name))
        if len(out) >= get_settings().agent_max_images:
            break
    return out


_VISION_CATALOGUE_MAX = 80          # a vision match lists the catalogue in the prompt: only small catalogues are matched visually
_MATCHED_SKU_RE = re.compile(r"\[MATCHED_SKU:\s*([A-Za-z0-9_\-]+)\s*\]")
_IMAGE_REPEAT_SECONDS = 300         # the same reply to a photo is never sent twice within this time
_IMAGE_SEARCH_TOP = 5


async def _vision_match(ctx, catalogue: Catalogue, image: bytes) -> str | None:
    """The catalogue SKU a product photo most likely shows (vision model, small catalogues only), or None."""
    from app.services import llm_health, vision_service

    views = [v for v in catalogue.views if v.in_stock()]
    if not image or not views or len(views) > _VISION_CATALOGUE_MAX or not llm_health.is_vision_enabled():
        return None
    context = "\n".join(f"{v.sku} | {v.name} | {v.product.category or '-'} | ₹{int(v.price)}" for v in views)
    reply = await vision_service.analyze_product_image(image, context)
    match = _MATCHED_SKU_RE.search(reply or "")
    sku = match.group(1).upper() if match else None
    return sku if sku and catalogue.get(sku) is not None else None


async def image_burst_active(ctx) -> bool:
    """True when this image belongs to a burst that was already answered (see ``payment_inbound.image_burst``): ONE reply per burst."""
    from app.services import payment_inbound

    return await payment_inbound.image_burst(ctx)


async def _stamp_image_reply(db, conv) -> None:
    """Remember that an image was just answered (the burst window starts)."""
    from datetime import datetime, timezone

    conv.image_batch_reply_at = datetime.now(timezone.utc)
    await db.commit()


@dataclass
class ImageOutcome:
    """What an image turn found: the plan to run, a plain answer (or None), and whether to stay silent (a repeat / a burst)."""

    plan: ex.PlanOutcome
    answer: str | None = None
    silent: bool = False
    key: str = ""


async def _image_outcome(ctx, cart, catalogue, lang: str) -> ImageOutcome:
    """
    A photo never fills an order slot. While the order is waiting for something it is a 👍 + the open question; otherwise, in order:

    1. the SKU watermark (OCR, as the legacy flow does) or a perceptual-hash match with a catalogue picture → that product's card;
    2. the vision model's description of the picture (reused from the payment classifier when it already looked) → a keyword search of
       the catalogue → the 3–5 closest products as a list;
    3. no match → ONE reply asking what they are looking for.

    The same reply is never sent twice within five minutes, and the second image of a burst gets no reply of its own.
    """
    empty = ex.PlanOutcome(PlannerOutput(actions=[]))
    if getattr(ctx, "photo_bytes", None) is None and await image_burst_active(ctx):
        logger.info("AGENT image burst conv=%s — no extra reply", ctx.conv.id)
        return ImageOutcome(empty, silent=True, key="burst")
    if any(True for _ in ex.gaps(cart, catalogue, ctx.client, is_whatsapp=ctx.is_whatsapp)) and cart.lines:
        return ImageOutcome(empty, "👍", key="thumbs_up")
    from app.services import image_fingerprint, llm_health, ocr_service, payment_proof_classifier

    image = getattr(ctx, "photo_bytes", None) or getattr(ctx, "image_bytes", None)
    verdict = getattr(ctx, "image_verdict", None)
    sku = getattr(verdict, "sku", None)
    description = getattr(verdict, "vision", None)
    try:
        if image is None and getattr(ctx.message, "image", None) is not None:
            image = await ctx.download_media(ctx.message.image.id)
        if image and sku is None and await asyncio.to_thread(payment_proof_classifier.is_blank_image, image):
            # a black / blank frame: no OCR, no hash, no vision call (they would only read noise) — ask for the picture again
            return ImageOutcome(empty, agent_renderer.text_for(lang, "image_blank"), key="image_blank")
        if image and sku is None:
            sku = await asyncio.to_thread(ocr_service.detect_sku_from_image, image)
        if image and sku is None:
            sku = await image_fingerprint.match_async(ctx.client.id, image)
        if image and sku is None and description is None and llm_health.is_vision_enabled():
            from app.db import release_connection

            await release_connection(ctx.db)          # the vision call takes seconds and needs no connection
            description = await payment_proof_classifier.vision_read(image, client_id=ctx.client.id, conversation_id=ctx.conv.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("AGENT image read failed conv=%s: %s", ctx.conv.id, exc)
    view = catalogue.get(sku) if sku else None
    if view is not None:
        return ImageOutcome(ex.PlanOutcome(PlannerOutput(actions=[ShowProduct(type="show_product", sku=view.sku)])), key=f"product:{view.sku}")
    query = description.search_query() if description is not None and description.kind == "product_photo" else ""
    if query:
        hits = [v for v in catalogue.search(query, top_k=_IMAGE_SEARCH_TOP) if v.in_stock()]
        if hits:
            logger.info("AGENT image search conv=%s query=%r hits=%s", ctx.conv.id, query, [v.sku for v in hits])
            return ImageOutcome(ex.PlanOutcome(PlannerOutput(actions=[SearchCatalog(type="search_catalog", query=query)])), key=f"search:{query}")
    return ImageOutcome(empty, agent_renderer.text_for(lang, "image_unknown"), key="image_unknown")


def remaining_delay(*, target: float, elapsed: float, credit: float) -> float:
    """Seconds of human-like pause still to wait: the target total minus the work already done and the batching wait."""
    return max(0.0, target - elapsed - credit)


async def handle_turn(ctx, *, stored_stage: str, llm_budget: str, record_usage):
    """
    Answer one customer message with agent v3. Always returns a ``PipelineResult`` (never None, never raises into the caller
    unless the database itself is broken). A cart that changed under the turn (a second message committed first) re-runs the
    turn once on the fresh cart.

    The human-like pause is a target total latency (`agent_reply_delay_*`): after the work, only what is left of it is slept —
    nothing after a planner turn or a long batching wait.
    """
    from app.services.order_pipeline import _reply_delay_credit

    settings = get_settings()
    t0 = time.perf_counter()
    result = None
    for attempt in (1, 2):
        try:
            result = await _turn(ctx, stored_stage=stored_stage, llm_budget=llm_budget, record_usage=record_usage)
            break
        except agent_cart.CartConflict:
            logger.warning("AGENT cart conflict conv=%s attempt=%d — re-running the turn", ctx.conv.id, attempt)
            await ctx.db.rollback()
            try:
                await ctx.db.refresh(ctx.conv)
            except Exception:  # noqa: BLE001
                pass
    if result is None:
        from app.services.order_pipeline import PipelineResult

        lang = _lang_svc.normalize_language(getattr(ctx.conv, "last_customer_language", None))
        return PipelineResult(text=agent_renderer.text_for(lang, "retry_later"), skip_localizer=lang in agent_renderer.TABLE_LANGUAGES)
    pause = remaining_delay(
        target=random.uniform(settings.agent_reply_delay_min_seconds, settings.agent_reply_delay_max_seconds),
        elapsed=time.perf_counter() - t0, credit=_reply_delay_credit.get(),
    )
    if pause > 0:
        turn_trace.add_span("wait", pause)
        await asyncio.sleep(pause)
    return result


_MAX_CLOCK_SKEW_SECONDS = 60
_FREE_TEXT_SLOTS = ("name", "address", "mobile")


async def _reply_language(db, client, conv, cart, message, text: str, open_question: dict | None) -> str:
    """
    The language to answer in (see ``language_tracker``): per-message signal, no LLM, no flip-flopping.

    A strong message (native script, ≥ 3 markers, or 2+ markers and nothing from another language) switches at once; a weaker clear
    one needs a second consecutive message; taps, numbers, "ok" and names keep the current language, and the first message of a
    conversation with no signal starts in the seller's default. The tracker state lives in the cart (``state.lang_track``), the
    result is mirrored on ``Conversation.last_customer_language`` and logged as ``LANG … lang= conf= switched=``.
    """
    stored = _lang_svc.normalize_language(getattr(conv, "last_customer_language", None))
    decision = language_tracker.decide(
        text, cart.state.get("lang_track"), fallback=stored, default=language_tracker.seller_default(client),
        free_text_open=(open_question or {}).get("slot") in _FREE_TEXT_SLOTS, tap=getattr(message, "type", "text") == "interactive",
    )
    cart.state["lang_track"] = decision.state
    logger.info(
        "LANG conv=%s lang=%s conf=%.2f switched=%s pending=%s (%s)", conv.id, decision.lang, decision.confidence,
        str(decision.switched).lower(), decision.state.get("pending") or "-", decision.why,
    )
    if decision.lang != stored:
        try:
            await conversation_service.update_language(db, conv.id, decision.lang)
            conv.last_customer_language = decision.lang
        except Exception as exc:  # noqa: BLE001 - never block the reply on the language bookkeeping
            logger.error("AGENT language update failed conv=%s: %s", conv.id, exc)
    return decision.lang


def _inbound_epoch(message) -> float | None:
    """The sender-side (Meta) timestamp of the inbound message as epoch seconds; None when absent or implausible (future clock skew)."""
    raw = getattr(message, "timestamp", None)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if 0 < value <= time.time() + _MAX_CLOCK_SKEW_SECONDS else None


async def _turn(ctx, *, stored_stage: str, llm_budget: str, record_usage):
    """One attempt at a turn (see the module docstring for the stages)."""
    t0 = time.perf_counter()
    db, client, conv, message = ctx.db, ctx.client, ctx.conv, ctx.message
    settings = get_settings()
    text = ctx.user_text or ""
    button_id = ctx.btn_nonce_parsed[0] if ctx.btn_nonce_parsed else None

    from app.services import order_pipeline as op

    pending = await _pending_orders(db, client, conv, ctx.sender_phone)
    cart = await agent_cart.get_active(
        db, conv, client.id, stale_after=op._flow_state_ttl(client),
        mobile=ctx.sender_phone if conv.channel == "whatsapp" else None,
    )
    catalogue = await Catalogue.load(db, client.id)
    inbound_ts = _inbound_epoch(message)
    last_clarify = cart.clarify_as_of(inbound_ts)      # what was on the customer's screen when they typed (not always the newest list)
    lang = await _reply_language(db, client, conv, cart, message, text, last_clarify)
    if last_clarify != (cart.state.get("last_clarify") or None):
        logger.info("AGENT list-context conv=%s message sent before the newest reply was delivered → resolved against the previous one", conv.id)

    ectx = ex.ExecContext(
        user_text=text, language=lang, sender_phone=ctx.sender_phone, wamid=ctx.wamid, button_id=button_id,
        pending_orders=pending, is_whatsapp=ctx.is_whatsapp, last_clarify=last_clarify, known_values=cart.known_values(),
    )
    extra_answer: str | None = None
    fast = None
    first_plan: ex.PlanOutcome | None = None
    photo_turn = getattr(ctx, "photo_bytes", None) is not None       # "Product photo" tapped for an image sent some turns ago
    if photo_turn or (getattr(message, "type", "text") == "image" and getattr(message, "image", None) is not None):
        outcome = await _image_outcome(ctx, cart, catalogue, lang)
        last = cart.state.get("last_image_reply") or {}
        repeat = outcome.key == last.get("key") and time.time() - float(last.get("ts", 0)) < _IMAGE_REPEAT_SECONDS
        if outcome.silent or repeat:
            logger.info("AGENT image reply suppressed conv=%s key=%s (%s)", conv.id, outcome.key, "burst" if outcome.silent else "repeat within 5 min")
            turn_trace.note(stage=cart.stage if cart.lines else "browsing", pending=[f"{o.order_number}:{o.status}" for o in pending])
            from app.services.order_pipeline import PipelineResult

            return PipelineResult(text=None, skip_send=True, skip_reason="image_burst" if outcome.silent else "image_repeat")
        first_plan, extra_answer = outcome.plan, outcome.answer
        cart.state["last_image_reply"] = {"key": outcome.key, "ts": time.time()}
        await _stamp_image_reply(db, conv)
        fast_name = "image"
    elif getattr(message, "type", "text") not in ("text", "interactive"):
        first_plan, fast_name = ex.PlanOutcome(PlannerOutput(actions=[])), "non_text"
    else:
        # "I want 1 black and 1 grey" right after confirming: a change to the order that just waits for payment (₹0, no planner).
        try:
            edit = await agent_order_edit.maybe_handle(
                db=db, client=client, conv=conv, cart=cart, catalogue=catalogue, pending=pending, text=text, button_id=button_id, lang=lang,
                is_whatsapp=ctx.is_whatsapp, open_slot=(last_clarify or {}).get("slot"),
            )
        except agent_cart.CartConflict:
            raise
        except Exception as exc:  # noqa: BLE001 - the edit step is an add-on: the normal turn answers when it fails
            logger.error("AGENT order-edit step failed conv=%s: %s", conv.id, exc, exc_info=True)
            try:
                await db.rollback()
                for row in (conv, client, *pending):        # the rollback expired them; async lazy loads would raise later in the turn
                    await db.refresh(row)
            except Exception:  # noqa: BLE001
                pass
            edit = None
        if isinstance(edit, Reply):
            return await _direct_turn(ctx, cart=cart, reply=edit, fast_name="order_edit", pending=pending, record_usage=record_usage, t0=t0, text=text)
        restorable = ([], None) if cart.lines else await agent_cart.recent_cancelled(
            db, conv, timedelta(minutes=settings.draft_restore_ttl_minutes),
        )
        fast = agent_order_edit_fast_path(edit) if edit else agent_fast_paths.resolve(
            text=text, button_id=button_id, cart=cart, catalogue=catalogue, client=client, is_whatsapp=ctx.is_whatsapp,
            has_pending_order=bool(pending), restorable=restorable, clarify=last_clarify or {},
        )
        fast_name = fast.name if fast else None
    if fast is not None:
        first_plan = ex.PlanOutcome(PlannerOutput(actions=fast.actions, reply_intent=fast.reply_intent))
        ectx.grounded_by, ectx.cancel_confirmed, ectx.confirm_ok = fast.grounded_by, fast.cancel_confirmed, fast.confirm_ok
        ectx.unavailable_options, ectx.not_in_cart = list(fast.unavailable), fast.not_in_cart
        ectx.forced_issue, ectx.qty_confirmed = fast.forced_issue, fast.qty_confirmed
    else:
        ectx.confirm_ok = cart.summary_on_screen and (button_id == "v3_confirm" or agent_text.looks_like_confirmation(text))

    planner_used = first_plan is None
    planner_allowed = llm_budget != "soft"

    async def plan_fn(repair):
        """Planner round (first plan or a repair); None when the planner must not / cannot be used."""
        if first_plan is not None or not planner_allowed:
            return None
        return await agent_planner.plan(
            db=db, client=client, conv=conv, catalogue=catalogue, cart=repair.cart if repair else cart, message=text,
            pending_orders=pending, last_clarify=last_clarify, repair=repair, language=lang,
        )

    async def _no_first(repair):
        """Plan function for fast paths / non-text turns: never calls the planner."""
        return None

    result = await ex.execute_with_repair(
        plan_fn if first_plan is None else _no_first, db=db, client=client, conv=conv, catalogue=catalogue, cart=cart,
        ctx=ectx, first_plan=first_plan,
    )
    if result.planner_failed and not planner_allowed:
        logger.info("AGENT planner skipped conv=%s (soft LLM cap)", conv.id)
    result.effects.unavailable_options, result.effects.not_in_cart = list(ectx.unavailable_options), ectx.not_in_cart
    if extra_answer:
        result.effects.answers.append(extra_answer)
    if any(i.code == "UPI_NOT_CONFIGURED" for i in result.issues):
        from app.services import payment_verification_service as pvs

        try:
            await pvs.raise_missing_upi_alert(db, client, conv)
        except Exception as exc:  # noqa: BLE001
            logger.error("missing-UPI alert failed conv=%s: %s", conv.id, exc)

    # ── invariants on the cart, then the choices nobody has to make, then what to ask next ───────────
    violations = inv.enforce_grounding(cart, result, ectx, catalogue, client)
    ex.autofill_single_options(result, catalogue)      # after grounding: the value is the shop's only option, not the customer's words
    ex.finish(result, catalogue, client, conv, ectx, cart)

    if result.effects.handoff and conv.ai_enabled is not False:     # e.g. the clarify limit: nobody ran handoff_human
        await ex.pause_for_handoff(db, client, conv, result.effects.handoff)

    # ── render ───────────────────────────────────────────────────────────────
    final = result.cart
    intent = final.state.pop("reply_intent", "none")
    renderer = Renderer(lang, client, catalogue, final, is_whatsapp=ctx.is_whatsapp, idle_hours=agent_cart.idle_hours(cart))
    for answer in result.effects.answers:       # facts the seller wrote (knowledge base) may contain amounts
        renderer.amounts.update(int(a.replace(",", "")) for a in _AMOUNT_RE.findall(answer) if a.replace(",", "").isdigit())
    images = await _product_images(client, catalogue.get(result.effects.images_for)) if result.effects.images_for else None
    with turn_trace.span("render"):
        reply = renderer.render(result, cart, intent=intent, images=images, resume=last_clarify)
        inv.acknowledge_pending_order(reply, cart, final, pending, renderer)
        violations += inv.enforce_reply(reply, renderer=renderer, cart=final, catalogue=catalogue, client=client)
        violations += [inv.Violation(code, "planner free text") for code in renderer.violations]

    # ── persist cart ─────────────────────────────────────────────────────────
    eff = result.effects
    new_version = final.version + (1 if result.mutated else 0)
    if not reply.resume_greeting:       # a welcome-back changes nothing: the open question / summary stay as they were
        final.record_shown(reply.last_clarify or None, time.time())
        final.state["last_clarify"] = reply.last_clarify if reply.last_clarify else None
        if final.state["last_clarify"] is None:
            final.state.pop("last_clarify")
        final.set_stage("confirming" if reply.summary_shown else "collecting" if final.lines else "browsing")
        if reply.summary_shown:
            final.state["summary_version"] = new_version
    if reply.offered_upsell:
        final.remember("offered_upsell", reply.offered_upsell)
        final.state["upsell"] = {"cart_id": final.id, "sku": reply.offered_upsell}
        final.state["upsell_done"] = True        # one offer per order cycle: later summaries of this cart carry none
    planner = (await llm_usage_service.turn_ledger(db, conv.id))["by_purpose"].get("planner", {})     # this turn's planner rows in llm_usage
    in_t, cached_t, out_t = planner.get("prompt_tokens", 0), planner.get("cached_tokens", 0), planner.get("completion_tokens", 0)
    turn_cost = planner.get("cost_usd", 0.0)
    final.remember_chosen()
    order_method = eff.order.method if eff.order else None
    if eff.order is not None:
        await agent_cart.close(db, final, "confirmed", order_id=eff.order.order.id, conv=conv)
    elif eff.cancel == "cart":
        await agent_cart.close(db, final, "cancelled", conv=conv)
    else:
        await agent_cart.save(db, final, bump=result.mutated, touch=not reply.resume_greeting)

    # ── mirror to the legacy conversation columns + stage ────────────────────
    await _mirror(
        db, conv, final, pending=pending, order_method=order_method, cancelled=eff.cancel == "cart",
        shown_something=bool(eff.product or eff.search or eff.browse or eff.images_for),
    )

    # ── transcript, usage, leads ─────────────────────────────────────────────
    try:
        await conversation_service.save_message(db, conv.id, "user", text, wamid=ctx.wamid)
        await conversation_service.save_message(
            db, conv.id, "assistant", reply.text,
            path="LLM" if planner_used and not result.planner_failed else "TEMPLATE",
            model=settings.llm_model_planner if planner_used and not result.planner_failed else None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("AGENT save error conv=%s: %s", conv.id, exc)
    try:
        await record_usage(db, client, conv)
    except Exception as exc:  # noqa: BLE001
        logger.error("Usage tracking error: %s", exc)
    if not getattr(conv, "is_sandbox", False):
        try:
            from app.services import lead_service

            history = await conversation_service.get_history(db, conv.id)
            await lead_service.tag_lead(
                db, ctx.sender_phone, conv.id, [{"role": m.role, "content": m.content} for m in history][-10:],
                client_id=client.id, channel=conv.channel,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Lead tagging error: %s", exc)

    turn_trace.note(stage=final.stage, pending=[f"{o.order_number}:{o.status}" for o in pending])
    if planner_used:
        turn_trace.note(planner_prompt_tokens=in_t, planner_cached_tokens=cached_t)
    logger.info(
        "AGENT conv=%s fast=%s planner=%s actions=%s issues=%s repairs=%d violations=%s "
        "tok=%d/%d/%d cost_usd=%.6f cart=%s stage=%s ms=%d v=%s fixes=%s",
        conv.id, fast_name, planner_used and not result.planner_failed, result.plan_actions,
        [f"{i.code}@{i.action_type}" for i in result.issues], result.repairs, [v.code for v in violations],
        in_t, cached_t, out_t, turn_cost,
        [(ln["sku"], ln.get("color"), ln.get("size"), ln.get("qty")) for ln in final.lines], final.stage,
        int((time.perf_counter() - t0) * 1000), agent_planner.prompt_tag(), result.fixes,
    )
    return _to_pipeline_result(reply)


def agent_order_edit_fast_path(actions: list):
    """"New order" tapped after an order that cannot be edited: the asked-for lines go through the normal executor as ``add_line`` actions."""
    return agent_fast_paths.FastPath("order_edit_new", list(actions), "button")


async def _direct_turn(ctx, *, cart, reply, fast_name: str, pending: list, record_usage, t0: float, text: str):
    """
    Finish a turn whose answer was built without the planner / executor (the order-edit conversation): persist the cart state, the
    transcript and the usage counters, trace it, and hand the reply to the channel adapter.
    """
    db, client, conv = ctx.db, ctx.client, ctx.conv
    await agent_cart.save(db, cart, bump=False, touch=True)
    try:
        await conversation_service.save_message(db, conv.id, "user", text, wamid=ctx.wamid)
        await conversation_service.save_message(db, conv.id, "assistant", reply.text, path="TEMPLATE")
    except Exception as exc:  # noqa: BLE001
        logger.error("AGENT save error conv=%s: %s", conv.id, exc)
    try:
        await record_usage(db, client, conv)
    except Exception as exc:  # noqa: BLE001
        logger.error("Usage tracking error: %s", exc)
    turn_trace.note(stage=cart.stage, pending=[f"{o.order_number}:{o.status}" for o in pending])
    logger.info(
        "AGENT conv=%s fast=%s planner=False actions=[] issues=[] repairs=0 violations=[] tok=0/0/0 cost_usd=0.000000 cart=%s stage=%s ms=%d v=%s fixes=[]",
        conv.id, fast_name, [(ln["sku"], ln.get("color"), ln.get("size"), ln.get("qty")) for ln in cart.lines], cart.stage,
        int((time.perf_counter() - t0) * 1000), agent_planner.prompt_tag(),
    )
    return _to_pipeline_result(reply)


async def _mirror(db, conv, cart, *, pending: list, order_method: str | None, cancelled: bool, shown_something: bool) -> None:
    """
    Keep the legacy columns that dashboards / follow-ups / leads read in step with the cart: customer name, address, mobile,
    payment method, the pinned and last-shown SKU, and — unless an order is pending — the conversation stage.
    """
    first_sku = cart.lines[0]["sku"] if cart.lines else None
    focus = cart.state.get("focus_sku")
    c = cart.customer
    if c.get("name"):
        conv.customer_name = c["name"]
    if c.get("address"):
        conv.delivery_address = c["address"]
    if c.get("mobile"):
        conv.mobile_number = c["mobile"]
    conv.payment_method = cart.payment_method if not order_method and not cancelled else (None if cancelled else conv.payment_method)
    conv.pending_product_sku = None if (order_method or cancelled) else (first_sku or focus)
    if first_sku or focus:
        conv.last_shown_sku = first_sku or focus
    await db.commit()
    new_stage = inv.stage_to_mirror(
        cart, pending_orders=pending, order_created=order_method, shown_something=shown_something, current=conv.current_stage,
        cart_stage="browsing" if cancelled else None,      # a cancelled cart is gone: the fresh one is empty
    )
    if cancelled and not pending and new_stage is None and conv.current_stage not in ("greeting", "product_inquiry"):
        new_stage = "greeting"
    if new_stage:
        await conversation_service.update_stage(db, conv.id, new_stage)
        conv.current_stage = new_stage
