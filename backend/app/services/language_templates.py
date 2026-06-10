"""
Language-isolated response templates for all supported languages.

All templates are keyed by a short action name. Use get_template() to
render a template for a given language with named keyword arguments.
NO template from one language may bleed into another — that is the entire
point of this file.
"""

from __future__ import annotations

ENGLISH_TEMPLATES: dict[str, str] = {
    "greeting": "Welcome to {business}! What are you looking for today?",
    "product_found": "{name} is available at ₹{price}. {stock} pieces in stock.",
    "ask_quantity": "How many pieces would you like?",
    "ask_name": "May I have your name please?",
    "ask_address": "What is your delivery address?",
    "ask_payment": (
        "Please pay {amount} via UPI:\n"
        "UPI ID: {upi_id}\n"
        "Send via GPay, PhonePe, or Paytm.\n"
        "Reply PAID when done. ✅"
    ),
    "stock_exceeded": "We only have {stock} pieces available. Would you like {stock} pieces?",
    "order_summary": (
        "✅ Order Summary:\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} × {qty} = ₹{total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "━━━━━━━━━━━━━━━\n"
        "Confirm? (yes/no)"
    ),
    "order_confirmed": "Order confirmed! ✅ {qty} × {product} = ₹{total}. Delivery in 3-5 business days.",
    "off_topic": "I can only help with {business} products. What would you like to see?",
    "out_of_stock": "{product} is currently out of stock. Can I show you similar items?",
    "delivery_info": "We deliver pan-India in 3-5 business days.",
}

HINDI_TEMPLATES: dict[str, str] = {
    "greeting": "{business} mein aapka swagat hai! Aaj kya dekhna chahenge?",
    "product_found": "{name} ₹{price} mein available hai. {stock} pieces stock mein hain.",
    "ask_quantity": "Kitne pieces chahiye?",
    "ask_name": "Aapka naam kya hai?",
    "ask_address": "Delivery address kya hai?",
    "ask_payment": (
        "{amount} UPI se bhejein:\n"
        "UPI ID: {upi_id}\n"
        "GPay, PhonePe, ya Paytm se send karein.\n"
        "Payment ke baad PAID reply karein. ✅"
    ),
    "stock_exceeded": "Sirf {stock} pieces available hain. {stock} pieces ka order karein?",
    "order_summary": (
        "✅ Order Summary:\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} × {qty} = ₹{total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "━━━━━━━━━━━━━━━\n"
        "Confirm karein? (haan/nahi)"
    ),
    "order_confirmed": "Order confirm ho gaya! ✅ {qty} × {product} = ₹{total}. 3-5 business days mein delivery.",
    "off_topic": "Main sirf {business} ke products ke baare mein help kar sakta hoon.",
    "out_of_stock": "{product} abhi stock mein nahi hai. Koi aur product dekhein?",
    "delivery_info": "Pan-India delivery 3-5 business days mein.",
}

GUJARATI_TEMPLATES: dict[str, str] = {
    "greeting": "{business} maa aapanu swagat chhe! Aaj shu joivanu chhe?",
    "product_found": "{name} ₹{price} maa available chhe. {stock} pieces stock maa chhe.",
    "ask_quantity": "Ketla pieces joiye chhe?",
    "ask_name": "Tamaru naam shu chhe?",
    "ask_address": "Delivery address shu chhe?",
    "ask_payment": (
        "{amount} UPI thi moklo:\n"
        "UPI ID: {upi_id}\n"
        "GPay, PhonePe, ke Paytm thi moklo.\n"
        "Payment pachhi PAID reply karo. ✅"
    ),
    "stock_exceeded": "Sirf {stock} pieces available chhe. {stock} pieces levo chhe?",
    "order_summary": (
        "✅ Order Summary:\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} × {qty} = ₹{total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "━━━━━━━━━━━━━━━\n"
        "Confirm karo? (ha/na)"
    ),
    "order_confirmed": "Order confirm thai gayu! ✅ {qty} × {product} = ₹{total}. 3-5 business days maa delivery.",
    "off_topic": "Huu sirf {business} na products vishe madad kari shakuu chhu.",
    "out_of_stock": "{product} abhi stock maa nathi. Biju koi product joivo chhe?",
    "delivery_info": "Pan-India delivery 3-5 business days maa.",
}

TEMPLATES: dict[str, dict[str, str]] = {
    "english": ENGLISH_TEMPLATES,
    "hindi_roman": HINDI_TEMPLATES,
    "hindi_devanagari": HINDI_TEMPLATES,
    "hinglish": HINDI_TEMPLATES,
    "gujarati_roman": GUJARATI_TEMPLATES,
    "gujarati_script": GUJARATI_TEMPLATES,
}


def get_template(lang: str, key: str, **kwargs: object) -> str:
    """
    Render a response template for the given language and action key.

    Falls back to ENGLISH_TEMPLATES when lang is unknown, and to an empty
    string when key is missing from both the target and English dicts.

    Args:
        lang:   Language code as returned by language_service.detect_language().
        key:    Template key (e.g. "greeting", "ask_name").
        **kwargs: Named format arguments for the template string.

    Returns:
        Rendered template string.
    """
    templates = TEMPLATES.get(lang, ENGLISH_TEMPLATES)
    template = templates.get(key) or ENGLISH_TEMPLATES.get(key, "")
    if not template:
        return ""
    try:
        return template.format(**kwargs)
    except KeyError:
        return template
