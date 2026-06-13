"""
AI service — generates conversational replies using Groq (Llama 3.3 70B).

Groq exposes an OpenAI-compatible REST API so the openai package is reused
with a custom base_url. The public interface is unchanged — all callers
(webhook, instagram, widget, lead_service, followup_service) work without
modification.

Public API:
    generate_reply(user_message, history, system_prompt, catalogue_context)

Note on role translation:
    The rest of the codebase stores AI replies with role='model' (Gemini
    convention). OpenAI-compatible APIs expect 'assistant'. This module
    translates transparently so callers and the DB schema are unaffected.
"""

from __future__ import annotations

import hashlib
import logging

from openai import AsyncOpenAI

from app.config import get_settings
from app.services import language_service
from app.services.conversation_flow import get_stage_instructions

logger = logging.getLogger(__name__)

_GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Tried in order — if one is rate-limited (429), fall back to the next.
_GROQ_MODELS = [
    "llama-3.3-70b-versatile",                 # primary
    "llama-3.1-8b-instant",                    # fallback 1 — smaller, fewer tokens
    "meta-llama/llama-4-scout-17b-16e-instruct",  # fallback 2 — Llama 4 Scout on Groq
    "qwen/qwen3-32b",                          # fallback 3 — Qwen 3 32B on Groq
]

_BUSY_FALLBACK_REPLY = "Abhi thodi busy hoon. 2 minute mein reply karungi. 🙏"

_FALLBACK_SYSTEM = (
    "You are a helpful AI assistant for an Indian business on WhatsApp. "
    "Answer customer questions clearly, help them find products or services, "
    "and guide them toward making a purchase. Be concise and friendly."
)


def _get_client() -> AsyncOpenAI:
    """
    Create and return an AsyncOpenAI client pointed at the Groq API.

    Extracted to a helper so tests can mock it independently without
    triggering a real API-key lookup at import time.

    Returns:
        AsyncOpenAI configured with GROQ_API_KEY and the Groq base URL.
    """
    settings = get_settings()
    return AsyncOpenAI(
        api_key=settings.groq_api_key,
        base_url=_GROQ_BASE_URL,
        # The SDK's built-in retry/backoff on 429s would stack on top of our
        # own model-fallback loop and blow past the webhook's response budget.
        # We handle 429s ourselves by moving to the next model immediately.
        max_retries=0,
    )


async def generate_reply(
    user_message: str,
    history: list[dict] | None = None,
    system_prompt: str | None = None,
    catalogue_context: str | None = None,
    language: str | None = None,
    previous_language: str | None = None,
) -> str:
    """
    Send a user message to Groq and return the AI-generated reply.

    Optionally includes prior conversation turns (history) so the model
    maintains context across messages.

    When catalogue_context is provided it is appended to the system prompt
    so the model can reference specific product details without inflating
    the user message. This keeps per-turn token cost low.

    Args:
        user_message:      The raw text of the latest customer message.
        history:           List of {'role': 'user'|'model', 'content': str}
                           dicts representing previous turns, oldest first.
                           At most the last 10 turns are included. The
                           'model' role is translated to 'assistant' for
                           the OpenAI API.
        system_prompt:     Optional instruction that sets the AI persona and
                           business context for this client. Falls back to a
                           generic helpful-assistant prompt when absent.
        catalogue_context: Optional pre-formatted product snippet produced by
                           catalogue_service.format_catalogue_context().
        language:          Pre-detected language code. When provided, skips
                           re-detection so the language rule matches the
                           conversation's established language exactly.
        previous_language: Fallback for ambiguous single-word replies when
                           language is not pre-detected (used only if language
                           is None).

    Returns:
        AI-generated reply text as a plain string. If every Groq model in
        _GROQ_MODELS is rate-limited (HTTP 429), a friendly Hinglish "busy"
        message is returned instead of raising — a reply beats no reply.

    Raises:
        RuntimeError: If a model returns an empty or null response.
        Exception: Any non-429 error from the Groq API is re-raised.
    """
    client = _get_client()

    # Use the pre-detected language when available to avoid re-detection errors
    # on ambiguous short replies ("yes", "20", "Amit") that would drop context.
    if language:
        lang = language
    else:
        lang = language_service.detect_language(user_message, previous_language=previous_language)
    lang_rule = language_service.build_language_rule(lang)
    base = system_prompt or _FALLBACK_SYSTEM
    full_system = lang_rule + "\n" + base
    if catalogue_context:
        full_system += f"\n\nAvailable products:\n{catalogue_context}"

    messages: list[dict] = [{"role": "system", "content": full_system}]

    # Include at most the last 10 turns from the conversation.
    # Translate 'model' → 'assistant' (OpenAI convention); coerce any other
    # invalid role to 'user' so Groq never sees a 400.
    _VALID_ROLES = {"user", "assistant", "system"}
    for msg in (history or [])[-10:]:
        raw = msg["role"]
        # "model" is Gemini/legacy convention; map to "assistant" for Groq.
        role = "assistant" if raw == "model" else raw
        if role not in _VALID_ROLES:
            role = "user"
        if not msg.get("content"):
            continue
        messages.append({"role": role, "content": msg["content"]})

    messages.append({"role": "user", "content": user_message})

    prompt_hash = hashlib.md5(full_system.encode()).hexdigest()[:8]
    logger.info("AI call | prompt_v:%s | model:%s | history_len:%d", prompt_hash, _GROQ_MODELS[0], len(history or []))

    for model in _GROQ_MODELS:
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=150,
                temperature=0.3,
            )
        except Exception as exc:
            if "429" in str(exc):
                logger.warning(f"Model {model} rate limited, trying next...")
                continue
            raise

        reply = response.choices[0].message.content
        if not reply:
            raise RuntimeError("OpenAI returned an empty response.")
        reply = reply.strip()
        logger.info("AI reply | prompt_v:%s | model:%s | reply_len:%d | tokens_approx:%d", prompt_hash, model, len(reply), len(reply.split()))
        return reply

    logger.error("All Groq models rate limited — returning busy fallback reply.")
    return _BUSY_FALLBACK_REPLY


def format_product_for_whatsapp(product) -> str:
    """
    Format a single product as a clean WhatsApp detail card.

    Mirrors the 👉-bulleted layout customers respond well to:
        *Product Name*
        👉 SKU: ...
        👉 Price: ₹...
        👉 Details: ...
        ✅ In Stock: N pieces  /  ❌ Out of Stock

    Args:
        product: Product ORM instance (or dict with the same keys).

    Returns:
        Multi-line WhatsApp-ready string.
    """
    name = getattr(product, "name", None) or (product.get("name") if isinstance(product, dict) else "Product")
    sku = getattr(product, "sku", None) or (product.get("sku") if isinstance(product, dict) else None)
    price = getattr(product, "price", None) or (product.get("price") if isinstance(product, dict) else 0)
    description = getattr(product, "description", None) or (product.get("description") if isinstance(product, dict) else None)
    stock = getattr(product, "stock", None) or (product.get("stock") if isinstance(product, dict) else None)

    lines = [f"*{name}*"]
    if sku:
        lines.append(f"👉 SKU: {sku}")
    if price:
        lines.append(f"👉 Price: ₹{price}")
    if description:
        lines.append(f"👉 Details: {description}")
    if stock and stock > 0:
        lines.append(f"✅ In Stock: {stock} pieces")
    else:
        lines.append("❌ Out of Stock")

    return "\n".join(lines)


# ── Sales-focused master prompt ───────────────────────────────────────────────


def format_products_for_prompt(products: list) -> str:
    """
    Format a list of product dicts/ORM objects into a readable catalogue block.

    For variant products (has_variants=True) the color/size breakdown is included
    so the AI can accurately answer availability questions.

    Args:
        products: List of Product ORM objects or dicts with product attributes.

    Returns:
        Multi-line string ready to embed in a system prompt.
    """
    if not products:
        return "No products in catalogue."

    lines = []
    for p in products:
        is_dict = isinstance(p, dict)
        name = getattr(p, "name", None) or (p.get("name") if is_dict else "Unknown")
        sku = getattr(p, "sku", None) or (p.get("sku") if is_dict else "")
        price = getattr(p, "price", None) or (p.get("price") if is_dict else 0)
        stock = getattr(p, "stock", None) if not is_dict else p.get("stock")
        category = getattr(p, "category", None) or (p.get("category") if is_dict else "")
        description = getattr(p, "description", None) or (p.get("description") if is_dict else "")
        has_variants = getattr(p, "has_variants", False) if not is_dict else p.get("has_variants", False)
        variants = getattr(p, "variants", None) if not is_dict else None

        line = f"• {name}"
        if sku:
            line += f" [{sku}]"
        if price:
            line += f" — ₹{price}"
        if category:
            line += f" | {category}"
        lines.append(line)

        if description:
            lines.append(f"  {description[:80]}")

        if has_variants and variants:
            active_v = [v for v in variants if getattr(v, "is_active", True)]
            colors: dict = {}
            sizes: set = set()
            for v in active_v:
                stk = v.stock or 0
                if stk > 0:
                    col = v.color or "default"
                    if col not in colors:
                        colors[col] = {}
                    if v.size:
                        colors[col][v.size] = stk
                        sizes.add(v.size)
                    else:
                        colors[col]["stock"] = colors[col].get("stock", 0) + stk
            if colors:
                parts = []
                for col, data in colors.items():
                    total = sum(v for k, v in data.items() if isinstance(v, int))
                    parts.append(f"{col}({total})")
                lines.append(f"  Colors available: {', '.join(parts)}")
            if sizes:
                lines.append(f"  Sizes available: {', '.join(sorted(sizes))}")
            oos = [f"{v.color}-{v.size}" for v in active_v if (v.stock or 0) == 0 and v.color and v.size]
            if oos:
                lines.append(f"  Out of stock: {', '.join(oos[:5])}")
        else:
            if stock is None:
                lines.append("  Stock: untracked")
            elif stock <= 0:
                lines.append("  Stock: ❌ Out of Stock")
            else:
                lines.append(f"  Stock: ✅ {stock} pcs")

    return "\n".join(lines)


def _build_payment_rule(accepts_cod: bool, upi_id: str | None = None) -> str:
    """
    Return the payment-method instruction block for the system prompt.

    Args:
        accepts_cod: Whether the client accepts Cash on Delivery.
        upi_id:      Client's UPI handle to embed directly in payment instructions.

    Returns:
        Formatted string ready for f-string insertion in the system prompt.
    """
    upi_display = upi_id if upi_id else "(UPI ID not configured — tell customer to contact you)"
    if accepts_cod:
        return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PAYMENT OPTIONS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
This business accepts BOTH UPI and Cash on Delivery (COD).
- For UPI: ALWAYS include the UPI ID — use the real amount from CURRENT ORDER block above:
  "Please pay [REAL AMOUNT] to UPI ID: {upi_display} via GPay / PhonePe / Paytm."
- For COD: confirm the order and say "COD order placed! We'll collect payment on delivery."
- NEVER write ₹[amount] — use the real calculated amount.
"""
    return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PAYMENT — UPI ONLY:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL: This business does NOT offer Cash on Delivery.
- NEVER mention COD, Cash on Delivery, or "cash pe dene ka" as an option.
- If the customer asks for COD, politely decline: "We only accept UPI payments."
- When sending UPI payment instructions, use the real amount from CURRENT ORDER above:
  UPI ID: {upi_display}
  Send via GPay, PhonePe, or Paytm.
- NEVER write ₹[amount] or ₹[Amount] — always use the real number.
- NEVER say "Our team will share UPI ID" — the UPI ID is: {upi_display}
"""


def build_master_system_prompt(
    client,
    products: list,
    conversation_stage: str,
    language: str,
    customer_history: dict | None = None,
    conversation=None,
    customer_profile=None,
    accepts_cod: bool = False,  # kept for call-site compatibility; overridden by client.accepts_cod
    variant_info: dict | None = None,
    kb_context: str = "",
    customer_context: str = "",
    current_instruction: str = "",
) -> str:
    """
    Build a laser-focused sales system prompt for the given client and stage.

    Embeds business details, catalogue, stage-specific instructions, language
    rules, and repeat-customer recognition into one cohesive prompt.

    Args:
        client:              Client ORM instance.
        products:            List of Product ORM objects for this client.
        conversation_stage:  Current STAGES key (e.g. "greeting").
        language:            Detected customer language ("hindi", "gujarati", "hinglish").
        customer_history:    Optional dict with total_orders, last_product, address.
        conversation:        Optional Conversation ORM instance — used during
                             order_collection to tell the agent which fields
                             (quantity/name/address) are already saved so it
                             does not re-ask them.
        customer_profile:    Optional Customer ORM instance for richer personalisation.
        accepts_cod:         Ignored — read directly from client.accepts_cod so the
                             prompt is always in sync with the client's settings.
        current_instruction: When non-empty and stage is order_collection or
                             awaiting_final_confirmation, this string REPLACES the
                             output of get_stage_instructions() — it contains a
                             single-slot instruction from the slot-filling state
                             machine that the AI must follow exactly.

    Returns:
        Full system prompt string.
    """
    collected: dict | None = None
    if conversation_stage == "order_collection" and conversation is not None:
        collected = {
            "quantity": getattr(conversation, "pending_order_quantity", None),
            "color": getattr(conversation, "selected_color", None),
            "size": getattr(conversation, "selected_size", None),
            "name": getattr(conversation, "customer_name", None),
            "address": getattr(conversation, "delivery_address", None),
        }

    # Compute real order total so the AI never uses ₹[amount] placeholders.
    order_product = None
    order_total: float = 0
    order_qty: int = 0
    order_product_name: str = ""

    if conversation is not None:
        order_qty = getattr(conversation, "pending_order_quantity", None) or 0
        pinned_sku = getattr(conversation, "pending_product_sku", None)
        if pinned_sku and products:
            order_product = next(
                (p for p in products if getattr(p, "sku", None) == pinned_sku), None
            )
        if order_product and order_qty:
            order_total = order_qty * (getattr(order_product, "price", 0) or 0)
            order_product_name = getattr(order_product, "name", "") or ""

    upi_id = getattr(client, "upi_id", None)
    client_accepts_cod = getattr(client, "accepts_cod", False) or False

    _slot_machine_stages = {"order_collection", "awaiting_final_confirmation"}
    if current_instruction and conversation_stage in _slot_machine_stages:
        # Slot-machine override: use the single-slot instruction from webhook.py.
        # Wrap it so formatting is consistent with other stage instruction blocks.
        stage_instructions = (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"CURRENT TASK — FOLLOW THIS EXACTLY, IGNORE EVERYTHING ELSE:\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{current_instruction}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
    else:
        stage_instructions = get_stage_instructions(
            conversation_stage,
            getattr(client, "business_type", "") or "",
            products,
            collected=collected,
            accepts_cod=client_accepts_cod,
            upi_id=upi_id,
            order_total=order_total,
            order_product_name=order_product_name,
            order_qty=order_qty,
            variant_info=variant_info,
        )

    repeat_customer_note = ""
    # Prefer richer Customer profile data; fall back to legacy conversation history dict
    _orders = 0
    _name = None
    _last_product = None
    _address = None
    _total_spent = None
    _preferred_payment = None
    _is_vip = False

    if customer_profile is not None:
        _orders = getattr(customer_profile, "total_orders", 0) or 0
        _name = getattr(customer_profile, "name", None)
        _address = getattr(customer_profile, "address", None)
        _total_spent = getattr(customer_profile, "total_spent", None)
        _preferred_payment = getattr(customer_profile, "preferred_payment", None)
        _is_vip = getattr(customer_profile, "is_vip", False)
    elif customer_history:
        _orders = customer_history.get("total_orders", 0) or 0
        _address = customer_history.get("address")

    if customer_history:
        _last_product = customer_history.get("last_product")

    if _orders > 0:
        vip_flag = "⭐ VIP CUSTOMER — Give them extra warmth and priority treatment.\n" if _is_vip else ""
        name_line = f"- Name: {_name}\n" if _name else ""
        spent_line = f"- Total spent: ₹{_total_spent:.0f}\n" if _total_spent else ""
        payment_line = f"- Preferred payment: {_preferred_payment}\n" if _preferred_payment else ""
        product_line = f"- Last product ordered: {_last_product}\n" if _last_product else ""
        address_line = f"- Address on file: {_address}\n" if _address else ""

        _confirm_name_rule = (
            f"- For name step: say 'Order for {_name}? (yes / change)' — do NOT ask name from scratch.\n"
            if _name else ""
        )
        _confirm_address_rule = (
            f"- For address step: say 'Deliver to {_address}? (yes / change)' — do NOT ask address from scratch.\n"
            if _address else ""
        )
        repeat_customer_note = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REPEAT CUSTOMER DETECTED:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{vip_flag}{name_line}- Total orders: {_orders}
{spent_line}{payment_line}{product_line}{address_line}
RULES FOR THIS RETURNING CUSTOMER:
⚠️  QUANTITY IS ALWAYS REQUIRED FIRST — even for repeat customers.
    ALWAYS ask 'How many pieces would you like?' BEFORE confirming name or address.
    NEVER skip quantity. NEVER assume quantity from context.
{_confirm_name_rule}{_confirm_address_rule}- Greet them personally by name{' (' + _name + ')' if _name else ''}. Reference their last order.
Use the correct language for the greeting — if language is english, say:
"Welcome back{' ' + _name + '!' if _name else '!'} How can I help you today?"
If Hindi/Hinglish: "Wapas aaye {_name or 'ji'}! Kya help kar sakta hoon?"
If Gujarati: "Pachhi phari aavya {_name or 'ji'}! Shu madad kari shakun?"
"""
    elif customer_history and customer_history.get("total_orders", 0) == 0:
        repeat_customer_note = "\nNew customer — no previous orders.\n"

    business_name = getattr(client, "business_name", "our business") or "our business"
    business_type = getattr(client, "business_type", "retail") or "retail"
    business_description = getattr(client, "business_description", "") or ""
    extra_prompt = getattr(client, "gemini_system_prompt", "") or ""

    # Build ORDER CONTEXT block with real values so AI never uses placeholders.
    if conversation is not None and (order_qty or getattr(conversation, "customer_name", None)):
        _qty = order_qty or getattr(conversation, "pending_order_quantity", 0) or 0
        _name = getattr(conversation, "customer_name", None) or "Not collected yet"
        _addr = getattr(conversation, "delivery_address", None) or "Not collected yet"
        _pay_method = getattr(conversation, "payment_method", None) or ("COD" if client_accepts_cod else "UPI")
        _total_str = f"₹{int(order_total):,}" if order_total > 0 else "TBD (qty × price)"
        _upi_display = upi_id if upi_id else "Contact us for UPI details"
        _summary_shown = getattr(conversation, "summary_shown", False)
        _sel_color = getattr(conversation, "selected_color", None) or "Not selected"
        _sel_size = getattr(conversation, "selected_size", None) or "Not selected"
        _vi = variant_info or {}
        _has_variants = _vi.get("has_variants", False)
        _variant_line = (
            f"Color:    {_sel_color}\nSize:     {_sel_size}\n"
            if _has_variants
            else ""
        )
        order_context_block = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURRENT ORDER (USE THESE EXACT VALUES — NEVER USE PLACEHOLDERS):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Product:  {order_product_name or 'Not selected yet'}
{_variant_line}Quantity: {_qty} pieces
Price:    ₹{getattr(order_product, 'price', 0) or 0:,.0f} per piece
TOTAL:    {_total_str}  ← USE THIS EXACT AMOUNT, NEVER ₹[amount]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Customer: {_name}
Address:  {_addr}
Payment:  {_pay_method}
Summary shown to customer: {'YES — proceed to payment step' if _summary_shown else 'NO — show summary before payment'}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UPI ID TO SHARE: {_upi_display}
COD Accepted: {client_accepts_cod}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    else:
        order_context_block = ""

    catalogue_slug = getattr(client, "catalogue_slug", None)
    from app.config import get_settings as _gs
    _settings = _gs()
    catalogue_link = (
        f"{_settings.catalogue_base_url}/{catalogue_slug}"
        if catalogue_slug
        else f"{_settings.catalogue_base_url}"
    )

    lang_rule = language_service.build_language_rule(language)
    lang_reminder = (
        f"🔁 FINAL REMINDER: Reply in {language.upper()} ONLY. "
        f"Customer wrote in {language.upper()} — match their language exactly."
    )

    from training.agent_training import get_examples_for_stage
    few_shot_block = get_examples_for_stage(conversation_stage)

    customer_context_block = (
        f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{customer_context}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        if customer_context.strip()
        else ""
    )

    return f"""{lang_rule}
You are a professional sales agent for {business_name} — a {business_type} business based in India.

YOUR ONLY JOB: Convert customer messages into orders.
You are NOT a general assistant.
You do NOT answer questions unrelated to the business.
{customer_context_block}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUSINESS DETAILS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Business: {business_name}
Type: {business_type}
Description: {business_description}
{order_context_block}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AVAILABLE PRODUCTS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{format_products_for_prompt(products)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURRENT CONVERSATION STAGE: {conversation_stage.upper()}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{stage_instructions}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRICT RULES — NEVER BREAK THESE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. ONLY discuss products from the catalogue above
2. NEVER discuss competitor products or prices
3. NEVER give discounts unless authorised (check products list)
4. NEVER discuss topics unrelated to business
   If asked: "Mujhe sirf {business_name} ke baare mein baat karni hai. Kya main aapki kuch madad kar sakta hoon?"
5. ALWAYS guide toward order placement
6. NEVER say "I don't know" — say "Main check karke batata hoon, ek minute"
7. Keep replies SHORT — max 2 sentences, max 30 words
8. ALWAYS end with a question or call to action
9. NEVER reveal you are an AI unless directly asked
   If asked: "Main {business_name} ka digital assistant hoon"
10. If customer is angry: apologize once, offer solution, move forward
11. Always use ₹ symbol for prices; use "aap" (not "tum"); add "ji" ONLY in Hindi/Gujarati replies — ABSOLUTELY NEVER in English replies. Not once. Not as a suffix. Never.
12. STRICT — NO HALLUCINATION: NEVER mention colors, sizes, materials, or any product attributes that are NOT explicitly listed in the catalogue data above. If customer asks about colors/sizes not in the catalogue: English → "For specific color/size details, please contact us directly." Hindi → "Color/size ke liye directly contact karein." Gujarati → "Color/size mate directly contact karo." NEVER say "available in red, pink, navy" unless the catalogue explicitly states it.
13. If customer switches language mid-conversation: switch your reply language immediately. Do NOT mention the language switch. Do NOT apologize. Just reply naturally in the new language.
14. When customer says goodbye / not interested / no thanks:
    - Be warm, never pushy
    - Share the catalogue link: {catalogue_link}
    - English example: "No problem! You can browse our full collection anytime at {catalogue_link} — we're here whenever you need us 😊"
    - Hinglish example: "Bilkul! Jab bhi dekhna ho, catalogue yahan hai: {catalogue_link} — hum hamesha available hain 😊"
    - IMPORTANT: Mention this link AT MOST ONCE in the entire conversation. Look at the chat
      history — if you (the assistant) already shared {catalogue_link} in any earlier reply,
      do NOT share it again. Just say goodbye warmly without the link.
15. CRITICAL ADVANTAGE — YOU CAN COMPLETE ORDERS DIRECTLY IN THIS CHAT:
    You CAN and MUST complete orders directly here. NEVER tell the customer to
    "contact our team", "call us", or "visit our website to place an order" —
    that is a FAILED conversion. You have full capability to:
    - Collect name, address, quantity
    - Confirm the order and show the order summary
    - Share UPI payment details or confirm COD
    - Complete the entire transaction, start to finish, in this chat
    When a customer shows ANY intent to order (e.g. "can you order 3 pieces?"):
    → Start the order_collection sequence immediately — do NOT redirect anywhere.
    This in-chat ordering capability is what makes us better than competitors.
16. NEVER reveal the AI platform's name or share any platform links.
    If asked "which AI are you?", "are you a bot?", "who made you?", "what app is this?":
    → Reply (in the customer's language): "Main {business_name} ka digital assistant
       hoon. Main yahan aapki help ke liye hoon." (or the natural English/Gujarati
       equivalent — e.g. "I'm {business_name}'s digital assistant, here to help you.")
    → NEVER mention AgentlyAI, the underlying platform, or any platform website/link.
    → You are {business_name}'s assistant. Period.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{few_shot_block}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

17. NEVER send images automatically. Only share a product photo when:
    - The customer explicitly asks to see it ("photo dikhao", "show me", "picture bhejo")
    - The customer shares a SKU and is asking for product details
    NEVER send brand/logo images or any unsolicited images after a reply.
18. STOCK ACCURACY — NEVER GUESS:
    Stock information comes from live catalogue data shown above (queried fresh
    from the database for this exact reply — never cached, never hardcoded).
    NEVER say stock is "1 piece" or "sirf 1 piece available" unless the catalogue
    above literally shows that product has exactly 1 in stock.
    Always read and quote the EXACT stock number from the catalogue above —
    do not estimate, round, or invent a number.
    CRITICAL: Always check stock for the product the customer ACTUALLY mentioned (by SKU or name).
    NEVER mix up products — if customer asked for SR27754, check SR27754's stock only.
19. LANGUAGE LOCK — ONCE SWITCHED, STAY SWITCHED:
    Track {language.upper()} as the customer's current conversation language
    (conversation.last_customer_language). Once the customer switches to a
    language (e.g. Hindi → Gujarati), reply in THAT language for the rest of
    the conversation — including quantity/name/address/payment questions and
    the order summary. Do NOT switch back to an earlier language unless the
    customer switches first.
20. "reset" IS A NORMAL CUSTOMER MESSAGE — NOT A COMMAND:
    If the customer types "reset", "restart", or "start over" treat it as a
    regular message — never wipe the conversation or jump back to step 1.
    Respond naturally: "I'm not sure what you mean — how can I help you today? 😊"
    (Hindi: "Main samjha nahi — kya main aapki kuch madad kar sakta hoon? 😊")
21. PAYMENT STAGE — STAY FOCUSED, NEVER GO BACKWARD:
    Once in the PAYMENT stage, NEVER move backward or restart the order collection.
    If the customer asks an off-topic question while payment is pending:
    → Answer in ONE short sentence, then IMMEDIATELY show the UPI details again.
    → Example: "Great quality! Please complete payment — UPI ID: {upi_id or "(check with us)"} ✅"
    → NEVER forget the payment context. NEVER repeat "Which payment method?" if customer already chose.
    → NEVER write ₹[amount] — always use the real amount from the CURRENT ORDER block above.
22. COD — NEVER HALLUCINATE:
    NEVER mention COD, Cash on Delivery, or any COD-related phrase unless:
    a) The customer specifically asked for COD, AND
    b) accepts_cod is True for this client.
    If neither condition is met, do NOT mention COD at all — not to offer it,
    not to decline it, not to reference it in any way.
    Bringing up COD when the customer never asked is a critical error.
23. SIZE RULES BY PRODUCT TYPE — STRICT:
    Saree, Dupatta, Stole, Scarf, Odhni → ONE SIZE FITS ALL → NEVER ask for size
    Lehenga, Kurti, Dress, Suit, Shirt, Top, Blouse → HAS SIZES → MUST ask size (S/M/L/XL/XXL)
    Shoes, Sandals, Footwear → NUMERIC SIZES → MUST ask size (e.g. 6, 7, 8, 9, 10)
    → Read the product NAME and CATEGORY from the catalogue — do NOT guess the type.
    → A Lehenga is NOT a Saree. A Saree does NOT have sizes. Never confuse them.
    → If product is size-dependent, ask size as part of STEP 1 of order_collection.
{_build_payment_rule(client_accepts_cod, upi_id=upi_id)}{repeat_customer_note}
{kb_context}{extra_prompt}
{lang_reminder}"""
