"""
Language-isolated response templates for all supported languages.

All templates are keyed by a short action name. Use get_template() to
render a template for a given language with named keyword arguments.
NO template from one language may bleed into another — that is the entire
point of this file.
"""

from __future__ import annotations


def format_price(p: float | int) -> str:
    """Format a price as integer rupees with thousands separator: ₹2,450."""
    return f"₹{int(round(p)):,}"


ENGLISH_TEMPLATES: dict[str, str] = {
    "greeting": "Welcome to {business}! What are you looking for today?",
    "greeting_new": "Welcome to {business}! 👋 Here's our catalogue: {catalogue_url}\nWhat are you looking for today?",
    "greeting_returning": "Welcome back {name}! 👋 Here's our latest collection: {catalogue_url}\nWhat are you looking for today?",
    "product_found": "{name} is available at ₹{price}. {stock} pieces in stock.",
    "ask_quantity": "Quantity?",
    "ask_name": "May I have your name please?",
    "ask_address": "What is your delivery address?",
    "ask_color": "Colour? {colors}",
    "ask_size": "Size? {sizes}",
    "ask_material": "Material? {materials}",
    "out_of_stock_combo": "{combo} is sold out. Another {last_attr}?",
    "cross_sell": "You also looked at {name} (₹{price}). Add it too? (yes/no)",
    "ask_payment_method": "How would you like to pay — UPI{cod_option}?",
    "confirm_payment_upi": "Payment: UPI ✅ Confirm? (yes)",
    "ask_payment_upi_cod": "Payment? UPI / COD",
    "confirm_saved_address": "Deliver to {address}? (yes/change)",
    "ask_payment": (
        "Please pay ₹{amount} via UPI:\n"
        "UPI ID: {upi_id}\n"
        "Send via GPay, PhonePe, or Paytm.\n"
        "Reply PAID when done. ✅"
    ),
    "upi_instructions": (
        "Please pay ₹{total} via UPI to complete your order {order_number}.\n"
        "UPI ID: {upi_id}\n"
        "Send via GPay, PhonePe, or Paytm.\n"
        "Reply PAID when done. ✅"
    ),
    "stock_exceeded": "We only have {stock} pieces available. Would you like {stock} pieces?",
    "order_summary": (
        "We currently accept payments via UPI only.\n\n"
        "✅ Order Summary\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 Delivery in {delivery_time}\n"
        "━━━━━━━━━━━━━━━"
    ),
    "order_summary_variant": (
        "We currently accept payments via UPI only.\n\n"
        "✅ Order Summary\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} {variant} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 Delivery in {delivery_time}\n"
        "━━━━━━━━━━━━━━━"
    ),
    "order_summary_crosssell": (
        "We currently accept payments via UPI only.\n\n"
        "✅ Order Summary\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 Delivery in {delivery_time}\n"
        "━━━━━━━━━━━━━━━\n"
        "Reply:\n"
        "1️⃣ Confirm & pay\n"
        "2️⃣ Add {cs_name} ({cs_price}) too\n"
        "3️⃣ Cancel"
    ),
    "order_summary_variant_crosssell": (
        "We currently accept payments via UPI only.\n\n"
        "✅ Order Summary\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} {variant} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 Delivery in {delivery_time}\n"
        "━━━━━━━━━━━━━━━\n"
        "Reply:\n"
        "1️⃣ Confirm & pay\n"
        "2️⃣ Add {cs_name} ({cs_price}) too\n"
        "3️⃣ Cancel"
    ),
    "order_confirmed": "Order confirmed! ✅ {qty} × {product} = {total}. Delivery in {delivery_time}.",
    "order_confirmed_cod": (
        "🎉 Your order has been placed successfully! ✅\n"
        "📦 {product} {variant_part}× {qty} = {total}\n"
        "🚚 Delivery in {delivery_time}\n\n"
        "Thank you for shopping with us! 😊\n"
        "{catalogue_line}"
    ),
    "order_confirmed_paid": (
        "🎉 Your order has been placed successfully! ✅\n"
        "📦 {product} {variant_part}× {qty} = {total}\n"
        "🚚 Delivery in {delivery_time}\n\n"
        "Thank you for shopping with us! 😊\n"
        "{catalogue_line}"
    ),
    "already_confirmed": "Your order is already confirmed ✅ Anything else I can help with?",
    "interrupt_switch": (
        "You have an order in progress: {current_qty}x {current_product}{current_total_text}.\n"
        "{new_product} [{new_sku}] is ₹{new_price}.\n"
        "Reply 'continue' to finish your current order, or 'switch' to start a new order "
        "for {new_product} (this will discard your current progress)."
    ),
    "switch_reask": (
        "You have an order in progress for {current_product}.\n"
        "Reply 'continue' to finish your current order, or 'switch' to start a new order."
    ),
    "cancel_ack": "No worries! How else can I help you? Feel free to browse our catalogue.",
    "discount_policy": (
        "Our prices are fixed and reflect guaranteed quality. "
        "Would you like to continue with the order?"
    ),
    "out_of_stock": "{product} is currently out of stock. Can I show you similar items?",
    "out_of_stock_block": (
        "Sorry, {product} is currently out of stock. "
        "Would you like to see other products?"
    ),
    "delivery_info": "We deliver pan-India in {delivery_time}.",
    "off_topic": "I can only help with {business} products. What would you like to see?",
    "off_topic_idle": (
        "{contact_line}"
        "What would you like to shop for today? 🙂"
    ),
    "off_topic_midorder": "Let's finish your order first 🙂\n\n{slot_question}",
    "order_status": (
        "Order #{order_id}: {product} {variant_part}× {qty} = {total}.\n"
        "Status: {status}.\n"
        "Delivery to {address}.\n"
        "{delivery_time}"
    ),
    "no_orders": (
        "You don't have any orders yet. "
        "Would you like to browse our catalogue? {catalogue_url}"
    ),
    "quantity_exceeds_stock": (
        "Sorry, only {stock} piece(s) of {product} are available. "
        "How many would you like (1–{stock})?"
    ),
}

HINDI_TEMPLATES: dict[str, str] = {
    "greeting": "{business} mein aapka swagat hai! Aaj kya dekhna chahenge?",
    "greeting_new": "{business} mein aapka swagat hai! 👋 Hamara catalogue yahan dekhein: {catalogue_url}\nAaj kya dekhna chahenge?",
    "greeting_returning": "Wapas aaye {name}! 👋 Hamara latest collection yahan hai: {catalogue_url}\nAaj kya dekhna chahenge?",
    "product_found": "{name} ₹{price} mein available hai. {stock} pieces stock mein hain.",
    "ask_quantity": "Kitne pieces chahiye?",
    "ask_name": "Aapka naam kya hai?",
    "ask_address": "Delivery address kya hai?",
    "ask_color": "Color? {colors}",
    "ask_size": "Size? {sizes}",
    "ask_material": "Material? {materials}",
    "out_of_stock_combo": "{combo} sold out. Koi aur {last_attr}?",
    "cross_sell": "Aapne {name} (₹{price}) bhi dekha tha. Isko bhi add karein? (haan/nahi)",
    "ask_payment_method": "Aap UPI se denge{cod_option}?",
    "confirm_payment_upi": "Payment: UPI ✅ Confirm karein? (haan)",
    "ask_payment_upi_cod": "Payment? UPI / COD",
    "confirm_saved_address": "{address} pe deliver karein? (haan/change)",
    "ask_payment": (
        "₹{amount} UPI se bhejein:\n"
        "UPI ID: {upi_id}\n"
        "GPay, PhonePe, ya Paytm se send karein.\n"
        "Payment ke baad PAID reply karein. ✅"
    ),
    "upi_instructions": (
        "Order {order_number} ke liye ₹{total} UPI se bhejein.\n"
        "UPI ID: {upi_id}\n"
        "GPay, PhonePe, ya Paytm se send karein.\n"
        "Payment ke baad PAID reply karein. ✅"
    ),
    "stock_exceeded": "Sirf {stock} pieces available hain. {stock} pieces ka order karein?",
    "order_summary": (
        "Hum sirf UPI se payment accept karte hain.\n\n"
        "✅ Order Summary\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 Delivery in {delivery_time}\n"
        "━━━━━━━━━━━━━━━"
    ),
    "order_summary_variant": (
        "Hum sirf UPI se payment accept karte hain.\n\n"
        "✅ Order Summary\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} {variant} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 Delivery in {delivery_time}\n"
        "━━━━━━━━━━━━━━━"
    ),
    "order_summary_crosssell": (
        "Hum sirf UPI se payment accept karte hain.\n\n"
        "✅ Order Summary\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 Delivery in {delivery_time}\n"
        "━━━━━━━━━━━━━━━\n"
        "Reply karein:\n"
        "1️⃣ Confirm & pay\n"
        "2️⃣ {cs_name} ({cs_price}) bhi add karein\n"
        "3️⃣ Cancel"
    ),
    "order_summary_variant_crosssell": (
        "Hum sirf UPI se payment accept karte hain.\n\n"
        "✅ Order Summary\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} {variant} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 Delivery in {delivery_time}\n"
        "━━━━━━━━━━━━━━━\n"
        "Reply karein:\n"
        "1️⃣ Confirm & pay\n"
        "2️⃣ {cs_name} ({cs_price}) bhi add karein\n"
        "3️⃣ Cancel"
    ),
    "order_confirmed": "Order confirm ho gaya! ✅ {qty} × {product} = {total}. {delivery_time} mein delivery.",
    "order_confirmed_cod": (
        "🎉 Aapka order place ho gaya! ✅\n"
        "📦 {product} {variant_part}× {qty} = {total}\n"
        "🚚 Delivery in {delivery_time}\n\n"
        "Shopping karne ke liye shukriya! 😊\n"
        "{catalogue_line}"
    ),
    "order_confirmed_paid": (
        "🎉 Aapka order place ho gaya! ✅\n"
        "📦 {product} {variant_part}× {qty} = {total}\n"
        "🚚 Delivery in {delivery_time}\n\n"
        "Shopping karne ke liye shukriya! 😊\n"
        "{catalogue_line}"
    ),
    "already_confirmed": "Aapka order confirm ho chuka hai ✅ Koi aur help chahiye?",
    "interrupt_switch": (
        "Aapka ek order chal raha hai: {current_qty}x {current_product}{current_total_text}.\n"
        "{new_product} [{new_sku}] ₹{new_price} mein hai.\n"
        "'continue' reply karein apna current order finish karne ke liye, "
        "ya 'switch' karein {new_product} ka naya order shuru karne ke liye "
        "(current progress delete ho jayega)."
    ),
    "switch_reask": (
        "{current_product} ka order chal raha hai.\n"
        "'continue' reply karein finish karne ke liye, ya 'switch' karein naya order karne ke liye."
    ),
    "cancel_ack": "Koi baat nahi! Aur kuch help chahiye? Hamara catalogue dekhein.",
    "discount_policy": (
        "Hamare prices fixed hain, lekin quality guaranteed hai. "
        "Kya aap order continue karna chahenge?"
    ),
    "out_of_stock": "{product} abhi stock mein nahi hai. Koi aur product dekhein?",
    "out_of_stock_block": (
        "Sorry, {product} abhi stock mein nahi hai. "
        "Koi aur product dekhein?"
    ),
    "delivery_info": "Pan-India delivery {delivery_time} mein.",
    "off_topic": "Main sirf {business} ke products ke baare mein help kar sakta hoon.",
    "off_topic_idle": (
        "{contact_line}"
        "Aaj kya khareedna chahenge? 🙂"
    ),
    "off_topic_midorder": "Pehle apna order complete karte hain 🙂\n\n{slot_question}",
    "order_status": (
        "Order #{order_id}: {product} {variant_part}× {qty} = {total}.\n"
        "Status: {status}.\n"
        "Delivery: {address}.\n"
        "{delivery_time}"
    ),
    "no_orders": (
        "Aapka koi order nahi hai abhi. "
        "Hamara catalogue dekhein? {catalogue_url}"
    ),
    "quantity_exceeds_stock": (
        "Maafi, sirf {stock} pieces available hain {product} ke liye. "
        "Aap kitne lenge (1–{stock})?"
    ),
}

GUJARATI_TEMPLATES: dict[str, str] = {
    "greeting": "{business} maa aapanu swagat chhe! Aaj shu joivanu chhe?",
    "greeting_new": "{business} maa aapanu swagat chhe! 👋 Amaru catalogue joi lo: {catalogue_url}\nAaj shu joivanu chhe?",
    "greeting_returning": "Pachi avya {name}! 👋 Amaru latest collection joi lo: {catalogue_url}\nAaj shu joivanu chhe?",
    "product_found": "{name} ₹{price} maa available chhe. {stock} pieces stock maa chhe.",
    "ask_quantity": "Ketla pieces joiye?",
    "ask_name": "Tamaru naam shu chhe?",
    "ask_address": "Delivery address shu chhe?",
    "ask_color": "Color? {colors}",
    "ask_size": "Size? {sizes}",
    "ask_material": "Material? {materials}",
    "out_of_stock_combo": "{combo} sold out. Bijo {last_attr}?",
    "cross_sell": "Tamey {name} (₹{price}) joi hatu. Ene pan add karvu chhe? (ha/na)",
    "ask_payment_method": "Kem bharvu chhe — UPI{cod_option}?",
    "confirm_payment_upi": "Payment: UPI ✅ Confirm karo? (ha)",
    "ask_payment_upi_cod": "Payment? UPI / COD",
    "confirm_saved_address": "{address} pe deliver karvu? (ha/change)",
    "ask_payment": (
        "{amount} UPI thi moklo:\n"
        "UPI ID: {upi_id}\n"
        "GPay, PhonePe, ke Paytm thi moklo.\n"
        "Payment pachhi PAID reply karo. ✅"
    ),
    "upi_instructions": (
        "Order {order_number} mate ₹{total} UPI thi moklo.\n"
        "UPI ID: {upi_id}\n"
        "GPay, PhonePe, ke Paytm thi moklo.\n"
        "Payment pachhi PAID reply karo. ✅"
    ),
    "stock_exceeded": "Sirf {stock} pieces available chhe. {stock} pieces levo chhe?",
    "order_summary": (
        "Hum sirf UPI thi payment accept karie chhe.\n\n"
        "✅ Order Summary\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 Delivery in {delivery_time}\n"
        "━━━━━━━━━━━━━━━"
    ),
    "order_summary_variant": (
        "Hum sirf UPI thi payment accept karie chhe.\n\n"
        "✅ Order Summary\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} {variant} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 Delivery in {delivery_time}\n"
        "━━━━━━━━━━━━━━━"
    ),
    "order_summary_crosssell": (
        "Hum sirf UPI thi payment accept karie chhe.\n\n"
        "✅ Order Summary\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 Delivery in {delivery_time}\n"
        "━━━━━━━━━━━━━━━\n"
        "Reply karo:\n"
        "1️⃣ Confirm & pay\n"
        "2️⃣ {cs_name} ({cs_price}) pan add karo\n"
        "3️⃣ Cancel"
    ),
    "order_summary_variant_crosssell": (
        "Hum sirf UPI thi payment accept karie chhe.\n\n"
        "✅ Order Summary\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} {variant} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 Delivery in {delivery_time}\n"
        "━━━━━━━━━━━━━━━\n"
        "Reply karo:\n"
        "1️⃣ Confirm & pay\n"
        "2️⃣ {cs_name} ({cs_price}) pan add karo\n"
        "3️⃣ Cancel"
    ),
    "order_confirmed": "Order confirm thai gayu! ✅ {qty} × {product} = {total}. {delivery_time} maa delivery.",
    "order_confirmed_cod": (
        "🎉 Tamaro order place thai gayu! ✅\n"
        "📦 {product} {variant_part}× {qty} = {total}\n"
        "🚚 Delivery in {delivery_time}\n\n"
        "Shopping karva mate aabhar! 😊\n"
        "{catalogue_line}"
    ),
    "order_confirmed_paid": (
        "🎉 Tamaro order place thai gayu! ✅\n"
        "📦 {product} {variant_part}× {qty} = {total}\n"
        "🚚 Delivery in {delivery_time}\n\n"
        "Shopping karva mate aabhar! 😊\n"
        "{catalogue_line}"
    ),
    "already_confirmed": "Tamaro order confirm thai gayo chhe ✅ Koi madad joiye?",
    "interrupt_switch": (
        "Tamare ek order chal raho chhe: {current_qty}x {current_product}{current_total_text}.\n"
        "{new_product} [{new_sku}] ₹{new_price} maa chhe.\n"
        "'continue' reply karo tamaro current order finish karva, "
        "ya 'switch' karo {new_product} mate navo order sharu karva "
        "(current progress delete thase)."
    ),
    "switch_reask": (
        "{current_product} no order chal raho chhe.\n"
        "'continue' reply karo finish karva, ya 'switch' karo navo order karva."
    ),
    "cancel_ack": "Koi vaat nahi! Biju koi madad joiye? Amaru catalogue joi shakay chho.",
    "discount_policy": (
        "Amara prices fixed chhe, pan quality guarantee chhe. "
        "Kya tamey order continue karvanu chhe?"
    ),
    "out_of_stock": "{product} abhi stock maa nathi. Biju koi product joivo chhe?",
    "out_of_stock_block": (
        "Sorry, {product} abhi stock maa nathi. "
        "Biju koi product joivo chhe?"
    ),
    "delivery_info": "Pan-India delivery {delivery_time} maa.",
    "off_topic": "Huu sirf {business} na products vishe madad kari shakuu chhu.",
    "off_topic_idle": (
        "{contact_line}"
        "Aaj shu khareedvanu chhe? 🙂"
    ),
    "off_topic_midorder": "Pehla tamaro order pura kariye 🙂\n\n{slot_question}",
    "order_status": (
        "Order #{order_id}: {product} {variant_part}× {qty} = {total}.\n"
        "Status: {status}.\n"
        "Delivery: {address}.\n"
        "{delivery_time}"
    ),
    "no_orders": (
        "Tamaro koi order nathi abhi. "
        "Amaru catalogue joi shakay chho? {catalogue_url}"
    ),
    "quantity_exceeds_stock": (
        "Maafi, {product} na sirf {stock} pieces available chhe. "
        "Tamey ketla leva chhe (1–{stock})?"
    ),
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
