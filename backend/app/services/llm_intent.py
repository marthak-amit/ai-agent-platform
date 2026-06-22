"""
JSON-only LLM understanding layer.

The LLM is an *understander*, never a *writer*: every customer-facing turn
that needs LLM help to resolve a product/intent gets a structured JSON
result here, never free prose. render_reply() is the only place that turns
this into customer text — see app.services.render_reply.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app.services import gemini_service

logger = logging.getLogger(__name__)

_SCHEMA_INSTRUCTION = """
RESPOND WITH ONLY A JSON OBJECT — NO PROSE, NO MARKDOWN, NO EXPLANATION.
Match exactly this schema:
{"intent": "ANSWER|ASK_PRODUCT|ASK_PRICE|CHANGE_ADDRESS|SIDE_QUESTION|CHITCHAT|LIST_PRODUCTS",
 "sku": "<single SKU string from the catalogue above, or null>",
 "skus": ["<SKU string>", ...] or null  — use this instead of "sku" ONLY when the
   customer is browsing a category and several catalogue items match (e.g. "saree dikhao"),
 "slots": {"color": "<string or null>", "size": "<string or null>", "quantity": <int or null>},
 "question_topic": "delivery_time|payment|quality|null"}
Only use SKUs that are explicitly listed in the product catalogue given to you above.
Never invent a SKU, product name, or price. If the customer names/confirms a
product by name (not SKU), resolve it to the matching catalogue SKU.
"""


@dataclass
class IntentResult:
    intent: str = "PARSE_FAILED"
    sku: str | None = None
    skus: list[str] | None = None
    color: str | None = None
    size: str | None = None
    quantity: int | None = None
    question_topic: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def parsed_ok(self) -> bool:
        return self.intent != "PARSE_FAILED"


def _parse(raw_text: str | None) -> IntentResult | None:
    """Parse a model response into an IntentResult, or None if it isn't valid JSON."""
    if not raw_text:
        return None
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or "intent" not in data:
        return None
    slots = data.get("slots") or {}
    if not isinstance(slots, dict):
        slots = {}
    quantity = slots.get("quantity")
    try:
        quantity = int(quantity) if quantity is not None else None
    except (TypeError, ValueError):
        quantity = None
    skus = data.get("skus")
    if not isinstance(skus, list) or not all(isinstance(s, str) for s in skus):
        skus = None
    return IntentResult(
        intent=str(data.get("intent") or "PARSE_FAILED"),
        sku=(data.get("sku") or None),
        skus=(skus or None),
        color=(slots.get("color") or None),
        size=(slots.get("size") or None),
        quantity=quantity,
        question_topic=(data.get("question_topic") or None),
        raw=data,
    )


async def classify_turn(
    user_text: str,
    history: list[dict] | None,
    system_prompt: str,
    catalogue_context: str | None,
    language: str | None,
) -> IntentResult:
    """
    Classify a customer turn as structured JSON instead of free prose.

    Retries once with an explicit repair instruction on parse failure. On a
    second failure, returns IntentResult(intent="PARSE_FAILED") — callers
    must never forward raw model text to the customer in that case.
    """
    full_prompt = f"{system_prompt}\n\n{_SCHEMA_INSTRUCTION}"
    raw = await gemini_service.generate_reply(
        user_text,
        history=history,
        system_prompt=full_prompt,
        catalogue_context=catalogue_context,
        language=language,
        response_format={"type": "json_object"},
    )
    result = _parse(raw)
    if result is not None:
        return result

    logger.warning("llm_intent: JSON parse failed, retrying once. raw=%r", (raw or "")[:200])
    repair_text = (
        f"{user_text}\n\n(Your previous reply was not valid JSON. "
        "Return ONLY the JSON object matching the schema.)"
    )
    raw2 = await gemini_service.generate_reply(
        repair_text,
        history=history,
        system_prompt=full_prompt,
        catalogue_context=catalogue_context,
        language=language,
        response_format={"type": "json_object"},
    )
    result2 = _parse(raw2)
    if result2 is not None:
        return result2

    logger.error("llm_intent: JSON parse failed twice, returning PARSE_FAILED. raw=%r", (raw2 or "")[:200])
    return IntentResult(intent="PARSE_FAILED")
