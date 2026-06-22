"""
Deterministic renderer for LLM-understood (open-browsing) turns.

Pairs with llm_intent.classify_turn(): the LLM returns structured JSON
(intent/sku/slots/question_topic), and this module turns that into the
exact customer-facing text, reading product facts from the DB — never
from anything the model wrote. This is what stops voice drift and
prevents an unparseable/wrong-product LLM reply from ever reaching a
customer (see Section 1 of external_data/trim_and_unify_prompt.md).
"""

from __future__ import annotations

from app.services.llm_intent import IntentResult

_KNOWN_FACTS = {
    "delivery_time": "Delivery usually takes 3–7 business days.",
    "payment": "We accept payments via UPI.",
    "quality": "All our products go through a quality check before dispatch.",
}


def render_product_list_reply(products: list) -> str:
    """
    Render a "browse this category" reply listing several catalogue products
    (e.g. customer says "saree dikhao" and several sarees match) — facts come
    straight from the DB-fetched product rows, never from model text.
    """
    lines = ["Here are our options:"]
    for p in products:
        price = int(getattr(p, "price", 0) or 0)
        lines.append(f"• {p.name} [{p.sku}] — ₹{price:,}")
    lines.append("Which one interests you?")
    return "\n".join(lines)


def render_open_browsing_reply(
    intent_result: IntentResult,
    product,
    variant_info: dict,
    client,
    business_name: str,
    user_text: str = "",
) -> str:
    """
    Render the customer-facing reply for an open-browsing turn.

    Args:
        intent_result: Structured result from llm_intent.classify_turn().
        product:       Product ORM instance resolved from intent_result.sku
                       (already re-fetched from the DB by the caller), or None
                       if no SKU could be resolved.
        variant_info:  Dict from catalogue_service.get_product_variant_info
                       for `product` (or the default empty dict if product is None).
        client:        Client ORM instance, or None.
        business_name: Display name to use when no product/topic is known.

    Returns:
        Customer-facing text. Never contains raw model prose.
    """
    if intent_result.question_topic and intent_result.question_topic in _KNOWN_FACTS:
        fact = _KNOWN_FACTS[intent_result.question_topic]
        if product:
            return f"{fact}\n\nWould you like to order {product.name} [{product.sku}]? (Yes / No)"
        return fact

    if product:
        price = int(getattr(product, "price", 0) or 0)
        vi = variant_info or {}
        colors = vi.get("available_colors", [])
        sizes = vi.get("available_sizes", [])
        lines = []
        # The model couldn't be resolved to a confirmed match for this query
        # (intent didn't parse, or no SKU resolved) — `product` here is just
        # the previously-pinned product, not necessarily what was asked
        # about. Say so honestly rather than implying it's a direct answer.
        product_mentioned = (
            product.name.lower() in (user_text or "").lower()
            or product.sku.lower() in (user_text or "").lower()
        )
        if not intent_result.parsed_ok and not product_mentioned:
            lines.append("Sorry, we don't carry that. Here's what we do have:")
        lines.append(f"{product.name} [{product.sku}] — ₹{price:,}")
        if colors:
            lines.append(f"Available colors: {', '.join(colors)}")
        if sizes:
            lines.append(f"Available sizes: {', '.join(sizes)}")
        lines.append("")
        lines.append("Would you like to order? (Yes / No)")
        return "\n".join(lines)

    return (
        f"I'd be happy to help you find the right product from {business_name}! "
        "Which item are you interested in?"
    )
