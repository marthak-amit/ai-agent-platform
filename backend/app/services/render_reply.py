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

from app.services.language_templates import get_template as _tpl
from app.services.llm_intent import IntentResult

# Maps a llm_intent question_topic to the language_templates key holding its
# canonical answer — kept in language_templates.py so every language variant
# lives next to the rest of the localized copy, not duplicated here.
_KNOWN_FACT_KEYS = {
    "delivery_time": "known_fact_delivery_time",
    "payment": "known_fact_payment",
    "quality": "known_fact_quality",
}


def render_product_list_reply(products: list, language: str = "english") -> str:
    """
    Render a "browse this category" reply listing several catalogue products
    (e.g. customer says "saree dikhao" and several sarees match) — facts come
    straight from the DB-fetched product rows, never from model text.

    Args:
        products: DB-fetched product rows to list.
        language: Language code as returned by language_service.detect_language();
                  determines which localized template wraps the product lines.
    """
    lines = [_tpl(language, "browse_list_header")]
    for p in products:
        price = int(getattr(p, "price", 0) or 0)
        lines.append(f"• {p.name} [{p.sku}] — ₹{price:,}")
    lines.append(_tpl(language, "browse_list_footer"))
    return "\n".join(lines)


def render_open_browsing_reply(
    intent_result: IntentResult,
    product,
    variant_info: dict,
    client,
    business_name: str,
    user_text: str = "",
    language: str = "english",
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
        language:      Language code as returned by language_service.detect_language();
                       every fixed phrase in the reply is drawn from language_templates
                       for this code so the reply matches the customer's language
                       even on this fallback/generic path.

    Returns:
        Customer-facing text. Never contains raw model prose.
    """
    if intent_result.question_topic and intent_result.question_topic in _KNOWN_FACT_KEYS:
        fact = _tpl(language, _KNOWN_FACT_KEYS[intent_result.question_topic])
        if product:
            order_line = _tpl(language, "would_you_like_to_order_named", name=product.name, sku=product.sku)
            return f"{fact}\n\n{order_line}"
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
            lines.append(_tpl(language, "not_carry_have_instead"))
        lines.append(f"{product.name} [{product.sku}] — ₹{price:,}")
        if colors:
            lines.append(_tpl(language, "available_colors_line", colors=", ".join(colors)))
        if sizes:
            lines.append(_tpl(language, "available_sizes_line", sizes=", ".join(sizes)))
        lines.append("")
        lines.append(_tpl(language, "would_you_like_to_order_generic"))
        return "\n".join(lines)

    return _tpl(language, "which_item", business=business_name)
