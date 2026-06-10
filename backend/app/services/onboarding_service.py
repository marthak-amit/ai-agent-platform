"""
Onboarding service — system prompt generation and setup-status computation.
"""

import secrets
from typing import Any, Optional

from app.models.client import Client

# ── prompt templates ────────────────────────────────────────────────────────

_PROMPT_TEMPLATES: dict[str, str] = {
    "textile": (
        "You are a friendly and knowledgeable sales assistant for {business_name}, "
        "a textile business. {description} "
        "Help customers explore our fabric collection, understand material qualities, "
        "check pricing and stock, and place orders. "
        "Share care instructions when relevant. Be warm, professional, and guide "
        "every customer toward finding exactly what they need. "
        "Aap Gujarati mein bhi baat kar sakte hain. "
        "Hamaara agent Gujarati samajhta hai."
    ),
    "clinic": (
        "You are a helpful patient-support assistant for {business_name}, a medical clinic. "
        "{description} "
        "Help patients book appointments, answer questions about our services and doctors, "
        "and share general health information. "
        "Always remind patients that specific medical advice must come from a qualified doctor. "
        "Be empathetic, clear, and reassuring."
    ),
    "realestate": (
        "You are a professional real-estate assistant for {business_name}. "
        "{description} "
        "Help clients find the right property by understanding their budget, location "
        "preferences, and requirements. Share details about available listings, "
        "arrange site visits, and answer questions about pricing, amenities, and "
        "legal formalities. Be persuasive yet honest, and always focus on the "
        "client's best interest."
    ),
    "ecommerce": (
        "You are an enthusiastic shopping assistant for {business_name}, an online store. "
        "{description} "
        "Help customers browse products, compare options, check availability and pricing, "
        "track orders, and resolve common issues like returns or exchanges. "
        "Be upbeat, clear, and proactively suggest products the customer might like."
    ),
    "other": (
        "You are a helpful AI assistant for {business_name}. "
        "{description} "
        "Answer customer questions professionally, help them find products or services, "
        "and guide them toward making a purchase or booking. "
        "Be concise, friendly, and always focused on solving the customer's problem."
    ),
}


def _format_products(products: Optional[list[dict[str, Any]]]) -> str:
    """Return a human-readable product catalogue string, or empty string if none."""
    if not products:
        return ""
    lines = []
    for p in products:
        name = p.get("name", "")
        price = p.get("price", "")
        stock = p.get("stock", "")
        parts = [name]
        if price:
            parts.append(f"₹{price}")
        if stock is not None and stock != "":
            parts.append(f"stock: {stock}")
        lines.append(" — ".join(parts))
    return "Our catalogue: " + "; ".join(lines) + "."


def generate_system_prompt(
    business_type: str,
    business_name: str,
    business_description: str,
    products: Optional[list[dict[str, Any]]] = None,
) -> str:
    """
    Build a tailored Gemini system prompt for the given business.

    Args:
        business_type:        One of textile/clinic/realestate/ecommerce/other.
        business_name:        The client's trading name.
        business_description: Free-text description of the business.
        products:             Optional list of {name, price, stock} dicts.

    Returns:
        A ready-to-use system prompt string.
    """
    template = _PROMPT_TEMPLATES.get(business_type, _PROMPT_TEMPLATES["other"])
    description_part = business_description.strip().rstrip(".")
    product_str = _format_products(products)
    if product_str:
        description_part = f"{description_part}. {product_str}"

    return template.format(
        business_name=business_name,
        description=description_part,
    )


def generate_api_key() -> str:
    """
    Generate a cryptographically secure API key.

    Returns:
        A 43-character URL-safe token prefixed with 'vp_'.
    """
    return f"vp_{secrets.token_urlsafe(32)}"


# ── setup status ─────────────────────────────────────────────────────────────

_STEPS = [
    "registered",
    "agent_configured",
    "products_added",
    "whatsapp_connected",
]


def get_setup_status(client: Client) -> dict:
    """
    Compute the onboarding completion state for a client.

    Steps:
        registered         — always true (client exists and is authenticated).
        agent_configured   — business_type and business_description are set.
        products_added     — products list is non-empty.
        whatsapp_connected — whatsapp_number is set.

    Args:
        client: The authenticated Client ORM instance.

    Returns:
        {
            "completion_percentage": int,
            "steps_done": [...],
            "steps_pending": [...],
        }
    """
    state = {
        "registered": True,
        "agent_configured": bool(client.business_type and client.business_description),
        "products_added": bool(client.products),
        "whatsapp_connected": bool(client.whatsapp_number),
    }

    done = [s for s in _STEPS if state[s]]
    pending = [s for s in _STEPS if not state[s]]
    pct = round(len(done) / len(_STEPS) * 100)

    return {
        "completion_percentage": pct,
        "steps_done": done,
        "steps_pending": pending,
    }
