"""
Tool-router: single LLM call per turn that returns structured tool proposals.

Architecture: LLM PROPOSES → deterministic gates validate → state machine executes → templates render.
The LLM never writes to DB and never produces customer-facing text in order stages.

Phase 0: shadow mode (USE_TOOL_ROUTER=False) — call runs in parallel, output is logged only.
Phase 1+: replaces extract_order_field / classify_user_intent / detect_stage.

Tools the LLM may propose:
  set_slot(field, value)       — fill an order slot
  switch_product(sku)          — customer wants a different product
  check_stock()                — availability question
  confirm_order()              — affirmative at awaiting_final_confirmation
  cancel_order()               — customer wants to cancel
  report_payment()             — "paid" / UPI done in payment stage
  answer_question(topic)       — browsing/discovery → falls through to generate_reply
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Valid tool names and slot field names — used for proposal validation.
_VALID_TOOLS: frozenset[str] = frozenset({
    "set_slot", "switch_product", "check_stock",
    "confirm_order", "cancel_order", "report_payment", "answer_question",
})
_VALID_SLOT_FIELDS: frozenset[str] = frozenset({
    "color", "size", "material", "quantity",
    "customer_name", "delivery_address", "payment_method",
})

# Compact system prompt — must work on llama-3.1-8b-instant.
# Strict JSON output, no prose. Tiny schema to minimise token usage.
_SYSTEM_PROMPT = """\
You are an order-flow router for a WhatsApp shopping bot.
Given conversation context, output EXACTLY one JSON object: {"calls": [...]}.
Each call: {"tool": "<name>", "args": {<args>}}

Available tools:
  set_slot       args: {"field": "<color|size|material|quantity|customer_name|delivery_address|payment_method>", "value": "<string>"}
  switch_product args: {"sku": "<sku>"}
  check_stock    args: {}
  confirm_order  args: {}
  cancel_order   args: {}
  report_payment args: {}
  answer_question args: {"topic": "<brief topic>"}

Decision rules:
- set_slot: customer is answering the current slot question (colour, size, name, address, etc.)
  quantity value must be numeric string e.g. "2".
- confirm_order: customer says yes/haan/ok/confirm at summary/confirmation stage.
- cancel_order: customer explicitly says cancel/no/stop.
- report_payment: customer says paid/done/sent/transferred/ho gaya.
- switch_product: customer mentions a different product SKU.
- check_stock: customer asks about availability/stock.
- answer_question: browsing, product questions, greetings, anything else.
- Multiple calls allowed when clearly multiple actions in one message.
- Empty calls=[] when nothing fits.
- Output ONLY valid JSON. No explanation, no markdown."""


def _validate_proposals(calls: list[dict]) -> list[dict[str, Any]]:
    """Validate and sanitise raw call list from LLM output. Drops invalid entries."""
    validated = []
    for call in calls:
        tool = call.get("tool")
        if tool not in _VALID_TOOLS:
            logger.debug("tool_router: unknown tool %r — skipping", tool)
            continue
        args = call.get("args") or {}
        if tool == "set_slot":
            field = args.get("field")
            if field not in _VALID_SLOT_FIELDS:
                logger.debug("tool_router: invalid slot field %r — skipping", field)
                continue
            value = args.get("value")
            if value is None:
                logger.debug("tool_router: set_slot missing value — skipping")
                continue
        if tool == "switch_product" and not args.get("sku"):
            logger.debug("tool_router: switch_product missing sku — skipping")
            continue
        validated.append({"tool": tool, "args": args})
    return validated


async def call_tool_router(
    user_text: str,
    stage: str,
    next_slot: str | None,
    variant_info: dict,
    conversation_history: list[dict],
    product_name: str,
    conversation_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    Make ONE LLM call and return validated tool proposals.

    Args:
        user_text:            Customer's latest message.
        stage:                Current conversation stage (keyword-layer decision).
        next_slot:            Next unfilled slot name, or None.
        variant_info:         Variant metadata dict from catalogue_service.
        conversation_history: Last N turns as [{"role": str, "content": str}].
        product_name:         Display name of the pinned product (or "").
        conversation_id:      PK of the Conversation row, for cost-log
                               attribution. None skips cost logging (e.g. a
                               caller that hasn't resolved a conversation yet).

    Returns:
        List of validated tool-call dicts, e.g. [{"tool": "set_slot", "args": {...}}].
        Returns [] on any error — callers must treat [] as "no proposal" (safe default).
    """
    try:
        from openai import AsyncOpenAI
        from app.config import get_settings

        settings = get_settings()
        client = AsyncOpenAI(
            api_key=settings.groq_api_key,
            base_url="https://api.groq.com/openai/v1",
            max_retries=0,
        )

        # Build compact context — last 4 turns max to keep tokens low.
        recent = conversation_history[-4:] if len(conversation_history) > 4 else conversation_history
        hist_lines = [
            f"{m['role'].upper()}: {(m.get('content') or '')[:100]}"
            for m in recent
        ]
        hist_text = "\n".join(hist_lines) if hist_lines else "(none)"

        # Slot context for the router.
        vi = variant_info or {}
        slot_ctx = ""
        if next_slot:
            slot_ctx = f"Collecting slot: {next_slot}."
            if next_slot == "color" and vi.get("available_colors"):
                slot_ctx += f" Valid colors: {vi['available_colors']}."
            elif next_slot == "size" and vi.get("available_sizes"):
                slot_ctx += f" Valid sizes: {vi['available_sizes']}."
            elif next_slot == "material" and vi.get("available_materials"):
                slot_ctx += f" Valid materials: {vi['available_materials']}."
        elif stage == "order_collection":
            slot_ctx = "All slots filled — awaiting confirmation."

        user_msg = (
            f"Stage: {stage}. Product: {product_name or '(unknown)'}. {slot_ctx}\n"
            f"Recent history:\n{hist_text}\n"
            f"Customer message: {user_text}"
        )

        resp = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=150,
            temperature=0,
        )

        if conversation_id is not None and getattr(resp, "usage", None) is not None:
            from app.services import cost_log
            cost_log.log(
                conversation_id, "IN", user_text,
                path="LLM", model="llama-3.1-8b-instant",
                in_tok=resp.usage.prompt_tokens, out_tok=resp.usage.completion_tokens,
                call_kind="classify",
            )

        raw = (resp.choices[0].message.content or "").strip()

        # Strip markdown fences if present.
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if fence_match:
            raw = fence_match.group(1)

        # Find first JSON object in the output.
        obj_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not obj_match:
            logger.warning("tool_router: no JSON in response: %r", raw[:100])
            return []

        parsed = json.loads(obj_match.group(0))
        raw_calls = parsed.get("calls", [])
        if not isinstance(raw_calls, list):
            return []

        return _validate_proposals(raw_calls)

    except Exception as exc:
        logger.warning("tool_router call failed (returning []): %s", exc)
        return []
