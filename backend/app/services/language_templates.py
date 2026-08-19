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
    "ask_mobile": "What is your mobile number for delivery?",
    "ask_color": "Colour? {colors}",
    "ask_size": "Size? {sizes}",
    "ask_material": "Material? {materials}",
    "ask_variant_mode": "Got it, {color_size}. Same for all {n}, or different for each? (Same/Different)",
    "ask_cart_item_color": "Item {item_num} — which color? Available: {colors}",
    "ask_cart_item_size": "Item {item_num} — which size? Available: {sizes}",
    "ask_cart_item_qty": "How many of {color_size}? ({remaining} left to assign)",
    "ask_cart_breakdown": "And the remaining {remaining} — give me the breakdown, e.g. '30 blue M, 37 green S'",
    "ask_cart_breakdown_confirm": "That's {breakdown}, correct?",
    "cart_breakdown_mismatch": "Total in your breakdown is {sum}, you said {remaining} remaining. Please recheck.",
    "cart_breakdown_invalid_variant": "'{invalid}' isn't a variant we carry. Valid colors: {colors}. Valid sizes: {sizes}.",
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
    "cart_order_summary": (
        "We currently accept payments via UPI only.\n\n"
        "✅ Order Summary\n"
        "━━━━━━━━━━━━━━━\n"
        "{items_block}\n"
        "Total: {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 Delivery in {delivery_time}\n"
        "━━━━━━━━━━━━━━━\n"
        "Reply:\n"
        "1️⃣ Confirm & pay\n"
        "2️⃣ Add another item\n"
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
    "order_confirmed_cod_cart": (
        "🎉 Your order has been placed successfully! ✅\n"
        "{items_block}\n"
        "Total: {total}\n"
        "🚚 Delivery in {delivery_time}\n\n"
        "Thank you for shopping with us! 😊\n"
        "{catalogue_line}"
    ),
    "order_confirmed_paid_cart": (
        "🎉 Your order has been placed successfully! ✅\n"
        "{items_block}\n"
        "Total: {total}\n"
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
    "which_item": "I'd be happy to help you find the right product from {business}! Which item are you interested in?",
    "pinned_availability": "{name} [{sku}] — ₹{price} is available.{color_part}{size_part}\n\nWould you like to order? (Yes / No)",
    "pinned_availability_guard": "{name} [{sku}] — ₹{price} is available.{color_part}{size_part} Reply Yes to order, or No to keep browsing. Would you like to order?",
    "available_colors_suffix": " Available in {colors}.",
    "available_sizes_suffix": " Sizes: {sizes}.",
    "known_fact_delivery_time": "Delivery usually takes 3–7 business days.",
    "known_fact_payment": "We accept payments via UPI.",
    "known_fact_quality": "All our products go through a quality check before dispatch.",
    "aside_question_no_info": "Sorry, I don't have that specific information right now — please check our catalogue or ask our team.",
    "would_you_like_to_order_named": "Would you like to order {name} [{sku}]? (Yes / No)",
    "would_you_like_to_order_generic": "Would you like to order? (Yes / No)",
    "browse_list_header": "Here are our options:",
    "browse_list_footer": "Which one interests you?",
    "not_carry_have_instead": "Sorry, we don't carry that. Here's what we do have:",
    "available_colors_line": "Available colors: {colors}",
    "available_sizes_line": "Available sizes: {sizes}",
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
    "ask_mobile": "Delivery ke liye mobile number kya hai?",
    "ask_color": "Color? {colors}",
    "ask_size": "Size? {sizes}",
    "ask_material": "Material? {materials}",
    "ask_variant_mode": "Theek hai, {color_size}. Sabhi {n} same chahiye, ya alag alag? (Same/Different)",
    "ask_cart_item_color": "Item {item_num} — kaunsa color? Available: {colors}",
    "ask_cart_item_size": "Item {item_num} — kaunsa size? Available: {sizes}",
    "ask_cart_item_qty": "{color_size} ke kitne? ({remaining} baaki hain)",
    "ask_cart_breakdown": "Aur baaki {remaining} ka breakdown batao — jaise '30 blue M, 37 green S'",
    "ask_cart_breakdown_confirm": "Yeh hai {breakdown}, sahi hai?",
    "cart_breakdown_mismatch": "Aapke breakdown ka total {sum} hai, aapne {remaining} bataya tha. Please recheck karein.",
    "cart_breakdown_invalid_variant": "'{invalid}' hamare paas nahi hai. Valid colors: {colors}. Valid sizes: {sizes}.",
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
    "cart_order_summary": (
        "Hum sirf UPI se payment accept karte hain.\n\n"
        "✅ Order Summary\n"
        "━━━━━━━━━━━━━━━\n"
        "{items_block}\n"
        "Total: {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 Delivery in {delivery_time}\n"
        "━━━━━━━━━━━━━━━\n"
        "Reply karein:\n"
        "1️⃣ Confirm & pay\n"
        "2️⃣ Ek aur item add karein\n"
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
    "order_confirmed_cod_cart": (
        "🎉 Aapka order place ho gaya! ✅\n"
        "{items_block}\n"
        "Total: {total}\n"
        "🚚 Delivery in {delivery_time}\n\n"
        "Shopping karne ke liye shukriya! 😊\n"
        "{catalogue_line}"
    ),
    "order_confirmed_paid_cart": (
        "🎉 Aapka order place ho gaya! ✅\n"
        "{items_block}\n"
        "Total: {total}\n"
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
    "which_item": "Main aapko {business} ka sahi product dhoondhne mein madad karunga! Aapko kaunsa item chahiye?",
    "pinned_availability": "{name} [{sku}] — ₹{price} mein available hai.{color_part}{size_part}\n\nKya aap order karna chahenge? (Haan / Nahi)",
    "pinned_availability_guard": "{name} [{sku}] — ₹{price} mein available hai.{color_part}{size_part} Order ke liye Yes, browsing jaari rakhne ke liye No reply karein. Kya aap order karna chahenge?",
    "available_colors_suffix": " {colors} mein available hai.",
    "available_sizes_suffix": " Size: {sizes}.",
    "known_fact_delivery_time": "Delivery mein 3–7 business days lagte hain.",
    "known_fact_payment": "Hum sirf UPI se payment accept karte hain.",
    "known_fact_quality": "Hamare saare products dispatch se pehle quality check hote hain.",
    "aside_question_no_info": "Sorry, abhi mere paas yeh specific information nahi hai — hamara catalogue check karein ya team se poochhein.",
    "would_you_like_to_order_named": "Kya aap {name} [{sku}] order karna chahenge? (Haan / Nahi)",
    "would_you_like_to_order_generic": "Kya aap order karna chahenge? (Haan / Nahi)",
    "browse_list_header": "Yeh hain hamare options:",
    "browse_list_footer": "Kaunsa pasand hai?",
    "not_carry_have_instead": "Sorry, woh hamare paas nahi hai. Yeh hai jo available hai:",
    "available_colors_line": "Available colors: {colors}",
    "available_sizes_line": "Available sizes: {sizes}",
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
    "ask_mobile": "Delivery mate mobile number shu chhe?",
    "ask_color": "Color? {colors}",
    "ask_size": "Size? {sizes}",
    "ask_material": "Material? {materials}",
    "ask_variant_mode": "Bhalu, {color_size}. Badha {n} same joiye, ke alag alag? (Same/Different)",
    "ask_cart_item_color": "Item {item_num} — kayo color? Available: {colors}",
    "ask_cart_item_size": "Item {item_num} — kayo size? Available: {sizes}",
    "ask_cart_item_qty": "{color_size} na ketla? ({remaining} baaki chhe)",
    "ask_cart_breakdown": "Ane baaki {remaining} nu breakdown aapo — jem ke '30 blue M, 37 green S'",
    "ask_cart_breakdown_confirm": "Aa che {breakdown}, barabar chhe?",
    "cart_breakdown_mismatch": "Tamara breakdown no total {sum} chhe, tame {remaining} kahyu hatu. Please recheck karo.",
    "cart_breakdown_invalid_variant": "'{invalid}' amara paase nathi. Valid colors: {colors}. Valid sizes: {sizes}.",
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
    "cart_order_summary": (
        "Hum sirf UPI thi payment accept karie chhe.\n\n"
        "✅ Order Summary\n"
        "━━━━━━━━━━━━━━━\n"
        "{items_block}\n"
        "Total: {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 Delivery in {delivery_time}\n"
        "━━━━━━━━━━━━━━━\n"
        "Reply karo:\n"
        "1️⃣ Confirm & pay\n"
        "2️⃣ Bijo item add karo\n"
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
    "order_confirmed_cod_cart": (
        "🎉 Tamaro order place thai gayu! ✅\n"
        "{items_block}\n"
        "Total: {total}\n"
        "🚚 Delivery in {delivery_time}\n\n"
        "Shopping karva mate aabhar! 😊\n"
        "{catalogue_line}"
    ),
    "order_confirmed_paid_cart": (
        "🎉 Tamaro order place thai gayu! ✅\n"
        "{items_block}\n"
        "Total: {total}\n"
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
    "which_item": "Huu tamne {business} nu sahi product shodhva madad karish! Tamne kayu item joiye chhe?",
    "pinned_availability": "{name} [{sku}] — ₹{price} maa available chhe.{color_part}{size_part}\n\nShu tame order karva mangso cho? (Haa / Na)",
    "pinned_availability_guard": "{name} [{sku}] — ₹{price} maa available chhe.{color_part}{size_part} Order karva Yes, browsing chalu rakhva No reply karo. Shu tame order karva mangso cho?",
    "available_colors_suffix": " {colors} maa available.",
    "available_sizes_suffix": " Size: {sizes}.",
    "known_fact_delivery_time": "Delivery maa 3–7 business days lage chhe.",
    "known_fact_payment": "Hum sirf UPI thi payment accept karie chhe.",
    "known_fact_quality": "Amara badha products dispatch pehla quality check thai chhe.",
    "aside_question_no_info": "Sorry, mari pase have e specific mahiti nathi — amaro catalogue check karo athva team ne pucho.",
    "would_you_like_to_order_named": "Shu tame {name} [{sku}] order karva mangso cho? (Haa / Na)",
    "would_you_like_to_order_generic": "Shu tame order karva mangso cho? (Haa / Na)",
    "browse_list_header": "Aa rahya amara options:",
    "browse_list_footer": "Kayu pasand chhe?",
    "not_carry_have_instead": "Sorry, e amara paase nathi. Aa available chhe:",
    "available_colors_line": "Available colors: {colors}",
    "available_sizes_line": "Available sizes: {sizes}",
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

HINDI_DEVANAGARI_TEMPLATES: dict[str, str] = {
    "greeting": "{business} में आपका स्वागत है! आज क्या देखना चाहेंगे?",
    "greeting_new": "{business} में आपका स्वागत है! 👋 हमारा कैटलॉग यहाँ देखें: {catalogue_url}\nआज क्या देखना चाहेंगे?",
    "greeting_returning": "वापस आए {name}! 👋 हमारा नया कलेक्शन यहाँ है: {catalogue_url}\nआज क्या देखना चाहेंगे?",
    "product_found": "{name} ₹{price} में उपलब्ध है। {stock} पीस स्टॉक में हैं।",
    "ask_quantity": "कितने पीस चाहिए?",
    "ask_name": "आपका नाम क्या है?",
    "ask_address": "डिलीवरी एड्रेस क्या है?",
    "ask_mobile": "डिलीवरी के लिए मोबाइल नंबर क्या है?",
    "ask_color": "कलर? {colors}",
    "ask_size": "साइज़? {sizes}",
    "ask_material": "मटेरियल? {materials}",
    "ask_variant_mode": "ठीक है, {color_size}। सभी {n} एक जैसे चाहिए, या अलग-अलग? (Same/Different)",
    "ask_cart_item_color": "आइटम {item_num} — कौनसा कलर? उपलब्ध: {colors}",
    "ask_cart_item_size": "आइटम {item_num} — कौनसा साइज़? उपलब्ध: {sizes}",
    "ask_cart_item_qty": "{color_size} के कितने? ({remaining} बाकी हैं)",
    "ask_cart_breakdown": "और बाकी {remaining} का ब्रेकडाउन बताओ — जैसे '30 blue M, 37 green S'",
    "ask_cart_breakdown_confirm": "यह है {breakdown}, सही है?",
    "cart_breakdown_mismatch": "आपके ब्रेकडाउन का टोटल {sum} है, आपने {remaining} बताया था। कृपया दोबारा चेक करें।",
    "cart_breakdown_invalid_variant": "'{invalid}' हमारे पास नहीं है। मान्य कलर: {colors}। मान्य साइज़: {sizes}।",
    "out_of_stock_combo": "{combo} सोल्ड आउट है। कोई और {last_attr}?",
    "cross_sell": "आपने {name} (₹{price}) भी देखा था। इसे भी ऐड करें? (हाँ/नहीं)",
    "ask_payment_method": "आप UPI से देंगे{cod_option}?",
    "confirm_payment_upi": "पेमेंट: UPI ✅ कन्फर्म करें? (हाँ)",
    "ask_payment_upi_cod": "पेमेंट? UPI / COD",
    "confirm_saved_address": "{address} पर डिलीवर करें? (हाँ/change)",
    "ask_payment": (
        "₹{amount} UPI से भेजें:\n"
        "UPI ID: {upi_id}\n"
        "GPay, PhonePe, या Paytm से भेजें।\n"
        "पेमेंट के बाद PAID रिप्लाई करें। ✅"
    ),
    "upi_instructions": (
        "ऑर्डर {order_number} के लिए ₹{total} UPI से भेजें।\n"
        "UPI ID: {upi_id}\n"
        "GPay, PhonePe, या Paytm से भेजें।\n"
        "पेमेंट के बाद PAID रिप्लाई करें। ✅"
    ),
    "stock_exceeded": "सिर्फ {stock} पीस उपलब्ध हैं। {stock} पीस का ऑर्डर करें?",
    "order_summary": (
        "हम सिर्फ UPI से पेमेंट स्वीकार करते हैं।\n\n"
        "✅ ऑर्डर सारांश\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 डिलीवरी {delivery_time} में\n"
        "━━━━━━━━━━━━━━━"
    ),
    "order_summary_variant": (
        "हम सिर्फ UPI से पेमेंट स्वीकार करते हैं।\n\n"
        "✅ ऑर्डर सारांश\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} {variant} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 डिलीवरी {delivery_time} में\n"
        "━━━━━━━━━━━━━━━"
    ),
    "order_summary_crosssell": (
        "हम सिर्फ UPI से पेमेंट स्वीकार करते हैं।\n\n"
        "✅ ऑर्डर सारांश\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 डिलीवरी {delivery_time} में\n"
        "━━━━━━━━━━━━━━━\n"
        "रिप्लाई करें:\n"
        "1️⃣ Confirm & pay\n"
        "2️⃣ {cs_name} ({cs_price}) भी ऐड करें\n"
        "3️⃣ Cancel"
    ),
    "order_summary_variant_crosssell": (
        "हम सिर्फ UPI से पेमेंट स्वीकार करते हैं।\n\n"
        "✅ ऑर्डर सारांश\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} {variant} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 डिलीवरी {delivery_time} में\n"
        "━━━━━━━━━━━━━━━\n"
        "रिप्लाई करें:\n"
        "1️⃣ Confirm & pay\n"
        "2️⃣ {cs_name} ({cs_price}) भी ऐड करें\n"
        "3️⃣ Cancel"
    ),
    "cart_order_summary": (
        "हम सिर्फ UPI से पेमेंट स्वीकार करते हैं।\n\n"
        "✅ ऑर्डर सारांश\n"
        "━━━━━━━━━━━━━━━\n"
        "{items_block}\n"
        "Total: {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 डिलीवरी {delivery_time} में\n"
        "━━━━━━━━━━━━━━━\n"
        "रिप्लाई करें:\n"
        "1️⃣ Confirm & pay\n"
        "2️⃣ एक और आइटम ऐड करें\n"
        "3️⃣ Cancel"
    ),
    "order_confirmed": "ऑर्डर कन्फर्म हो गया! ✅ {qty} × {product} = {total}। {delivery_time} में डिलीवरी।",
    "order_confirmed_cod": (
        "🎉 आपका ऑर्डर प्लेस हो गया! ✅\n"
        "📦 {product} {variant_part}× {qty} = {total}\n"
        "🚚 डिलीवरी {delivery_time} में\n\n"
        "शॉपिंग करने के लिए शुक्रिया! 😊\n"
        "{catalogue_line}"
    ),
    "order_confirmed_paid": (
        "🎉 आपका ऑर्डर प्लेस हो गया! ✅\n"
        "📦 {product} {variant_part}× {qty} = {total}\n"
        "🚚 डिलीवरी {delivery_time} में\n\n"
        "शॉपिंग करने के लिए शुक्रिया! 😊\n"
        "{catalogue_line}"
    ),
    "order_confirmed_cod_cart": (
        "🎉 आपका ऑर्डर प्लेस हो गया! ✅\n"
        "{items_block}\n"
        "Total: {total}\n"
        "🚚 डिलीवरी {delivery_time} में\n\n"
        "शॉपिंग करने के लिए शुक्रिया! 😊\n"
        "{catalogue_line}"
    ),
    "order_confirmed_paid_cart": (
        "🎉 आपका ऑर्डर प्लेस हो गया! ✅\n"
        "{items_block}\n"
        "Total: {total}\n"
        "🚚 डिलीवरी {delivery_time} में\n\n"
        "शॉपिंग करने के लिए शुक्रिया! 😊\n"
        "{catalogue_line}"
    ),
    "already_confirmed": "आपका ऑर्डर कन्फर्म हो चुका है ✅ कोई और मदद चाहिए?",
    "interrupt_switch": (
        "आपका एक ऑर्डर चल रहा है: {current_qty}x {current_product}{current_total_text}।\n"
        "{new_product} [{new_sku}] ₹{new_price} में है।\n"
        "'continue' रिप्लाई करें अपना मौजूदा ऑर्डर पूरा करने के लिए, "
        "या 'switch' करें {new_product} का नया ऑर्डर शुरू करने के लिए "
        "(मौजूदा प्रगति डिलीट हो जाएगी)।"
    ),
    "switch_reask": (
        "{current_product} का ऑर्डर चल रहा है।\n"
        "'continue' रिप्लाई करें पूरा करने के लिए, या 'switch' करें नया ऑर्डर करने के लिए।"
    ),
    "cancel_ack": "कोई बात नहीं! और कुछ मदद चाहिए? हमारा कैटलॉग देखें।",
    "discount_policy": (
        "हमारे दाम फिक्स हैं, लेकिन क्वालिटी गारंटीड है। "
        "क्या आप ऑर्डर जारी रखना चाहेंगे?"
    ),
    "out_of_stock": "{product} अभी स्टॉक में नहीं है। कोई और प्रोडक्ट देखें?",
    "out_of_stock_block": (
        "माफ़ कीजिए, {product} अभी स्टॉक में नहीं है। "
        "कोई और प्रोडक्ट देखें?"
    ),
    "delivery_info": "पूरे भारत में डिलीवरी {delivery_time} में।",
    "off_topic": "मैं सिर्फ {business} के प्रोडक्ट्स के बारे में मदद कर सकता हूँ।",
    "which_item": "मैं आपको {business} का सही प्रोडक्ट ढूंढने में मदद करूंगा! आपको कौनसा आइटम चाहिए?",
    "pinned_availability": "{name} [{sku}] — ₹{price} में उपलब्ध है।{color_part}{size_part}\n\nक्या आप ऑर्डर करना चाहेंगे? (हाँ / नहीं)",
    "pinned_availability_guard": "{name} [{sku}] — ₹{price} में उपलब्ध है।{color_part}{size_part} ऑर्डर के लिए Yes, ब्राउज़िंग जारी रखने के लिए No रिप्लाई करें। क्या आप ऑर्डर करना चाहेंगे?",
    "available_colors_suffix": " {colors} में उपलब्ध है।",
    "available_sizes_suffix": " साइज़: {sizes}।",
    "known_fact_delivery_time": "डिलीवरी में 3–7 बिज़नेस डेज़ लगते हैं।",
    "known_fact_payment": "हम सिर्फ UPI से पेमेंट स्वीकार करते हैं।",
    "known_fact_quality": "हमारे सभी प्रोडक्ट्स डिस्पैच से पहले क्वालिटी चेक होते हैं।",
    "aside_question_no_info": "माफ़ कीजिए, अभी मेरे पास यह विशेष जानकारी नहीं है — कृपया हमारा कैटलॉग देखें या हमारी टीम से पूछें।",
    "would_you_like_to_order_named": "क्या आप {name} [{sku}] ऑर्डर करना चाहेंगे? (हाँ / नहीं)",
    "would_you_like_to_order_generic": "क्या आप ऑर्डर करना चाहेंगे? (हाँ / नहीं)",
    "browse_list_header": "यह हैं हमारे विकल्प:",
    "browse_list_footer": "कौनसा पसंद है?",
    "not_carry_have_instead": "माफ़ कीजिए, वो हमारे पास नहीं है। यह है जो उपलब्ध है:",
    "available_colors_line": "उपलब्ध कलर: {colors}",
    "available_sizes_line": "उपलब्ध साइज़: {sizes}",
    "off_topic_idle": (
        "{contact_line}"
        "आज क्या खरीदना चाहेंगे? 🙂"
    ),
    "off_topic_midorder": "पहले अपना ऑर्डर पूरा करते हैं 🙂\n\n{slot_question}",
    "order_status": (
        "ऑर्डर #{order_id}: {product} {variant_part}× {qty} = {total}।\n"
        "स्टेटस: {status}।\n"
        "डिलीवरी: {address}।\n"
        "{delivery_time}"
    ),
    "no_orders": (
        "आपका कोई ऑर्डर नहीं है अभी। "
        "हमारा कैटलॉग देखें? {catalogue_url}"
    ),
    "quantity_exceeds_stock": (
        "माफ़ी, सिर्फ {stock} पीस उपलब्ध हैं {product} के लिए। "
        "आप कितने लेंगे (1–{stock})?"
    ),
}

GUJARATI_SCRIPT_TEMPLATES: dict[str, str] = {
    "greeting": "{business} માં આપનું સ્વાગત છે! આજ શું જોઈવાનું છે?",
    "greeting_new": "{business} માં આપનું સ્વાગત છે! 👋 અમારું કેટલોગ જુઓ: {catalogue_url}\nઆજ શું જોઈવાનું છે?",
    "greeting_returning": "પાછા આવ્યા {name}! 👋 અમારું નવું કલેક્શન જુઓ: {catalogue_url}\nઆજ શું જોઈવાનું છે?",
    "product_found": "{name} ₹{price} માં ઉપલબ્ધ છે. {stock} પીસ સ્ટોકમાં છે.",
    "ask_quantity": "કેટલા પીસ જોઈએ?",
    "ask_name": "તમારું નામ શું છે?",
    "ask_address": "ડિલિવરી એડ્રેસ શું છે?",
    "ask_mobile": "ડિલિવરી માટે મોબાઈલ નંબર શું છે?",
    "ask_color": "કલર? {colors}",
    "ask_size": "સાઈઝ? {sizes}",
    "ask_material": "મટીરીયલ? {materials}",
    "ask_variant_mode": "ભલું, {color_size}. બધા {n} સરખા જોઈએ, કે અલગ અલગ? (Same/Different)",
    "ask_cart_item_color": "આઇટમ {item_num} — કયો કલર? ઉપલબ્ધ: {colors}",
    "ask_cart_item_size": "આઇટમ {item_num} — કઈ સાઈઝ? ઉપલબ્ધ: {sizes}",
    "ask_cart_item_qty": "{color_size} ના કેટલા? ({remaining} બાકી છે)",
    "ask_cart_breakdown": "અને બાકી {remaining} નું બ્રેકડાઉન આપો — જેમ કે '30 blue M, 37 green S'",
    "ask_cart_breakdown_confirm": "આ છે {breakdown}, બરાબર છે?",
    "cart_breakdown_mismatch": "તમારા બ્રેકડાઉનનો ટોટલ {sum} છે, તમે {remaining} કહ્યું હતું. કૃપા કરી ફરી ચેક કરો.",
    "cart_breakdown_invalid_variant": "'{invalid}' અમારી પાસે નથી. માન્ય કલર: {colors}. માન્ય સાઈઝ: {sizes}.",
    "out_of_stock_combo": "{combo} સોલ્ડ આઉટ છે. બીજો {last_attr}?",
    "cross_sell": "તમે {name} (₹{price}) પણ જોયું હતું. તેને પણ ઉમેરવું છે? (હા/ના)",
    "ask_payment_method": "કેમ ભરવું છે — UPI{cod_option}?",
    "confirm_payment_upi": "પેમેન્ટ: UPI ✅ કન્ફર્મ કરો? (હા)",
    "ask_payment_upi_cod": "પેમેન્ટ? UPI / COD",
    "confirm_saved_address": "{address} પર ડિલિવર કરવું? (હા/change)",
    "ask_payment": (
        "₹{amount} UPI થી મોકલો:\n"
        "UPI ID: {upi_id}\n"
        "GPay, PhonePe, કે Paytm થી મોકલો.\n"
        "પેમેન્ટ પછી PAID રિપ્લાય કરો. ✅"
    ),
    "upi_instructions": (
        "ઓર્ડર {order_number} માટે ₹{total} UPI થી મોકલો.\n"
        "UPI ID: {upi_id}\n"
        "GPay, PhonePe, કે Paytm થી મોકલો.\n"
        "પેમેન્ટ પછી PAID રિપ્લાય કરો. ✅"
    ),
    "stock_exceeded": "ફક્ત {stock} પીસ ઉપલબ્ધ છે. {stock} પીસ લેવા છે?",
    "order_summary": (
        "અમે ફક્ત UPI થી પેમેન્ટ સ્વીકારીએ છીએ.\n\n"
        "✅ ઓર્ડર સારાંશ\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 ડિલિવરી {delivery_time} માં\n"
        "━━━━━━━━━━━━━━━"
    ),
    "order_summary_variant": (
        "અમે ફક્ત UPI થી પેમેન્ટ સ્વીકારીએ છીએ.\n\n"
        "✅ ઓર્ડર સારાંશ\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} {variant} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 ડિલિવરી {delivery_time} માં\n"
        "━━━━━━━━━━━━━━━"
    ),
    "order_summary_crosssell": (
        "અમે ફક્ત UPI થી પેમેન્ટ સ્વીકારીએ છીએ.\n\n"
        "✅ ઓર્ડર સારાંશ\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 ડિલિવરી {delivery_time} માં\n"
        "━━━━━━━━━━━━━━━\n"
        "રિપ્લાય કરો:\n"
        "1️⃣ Confirm & pay\n"
        "2️⃣ {cs_name} ({cs_price}) પણ ઉમેરો\n"
        "3️⃣ Cancel"
    ),
    "order_summary_variant_crosssell": (
        "અમે ફક્ત UPI થી પેમેન્ટ સ્વીકારીએ છીએ.\n\n"
        "✅ ઓર્ડર સારાંશ\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} {variant} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 ડિલિવરી {delivery_time} માં\n"
        "━━━━━━━━━━━━━━━\n"
        "રિપ્લાય કરો:\n"
        "1️⃣ Confirm & pay\n"
        "2️⃣ {cs_name} ({cs_price}) પણ ઉમેરો\n"
        "3️⃣ Cancel"
    ),
    "cart_order_summary": (
        "અમે ફક્ત UPI થી પેમેન્ટ સ્વીકારીએ છીએ.\n\n"
        "✅ ઓર્ડર સારાંશ\n"
        "━━━━━━━━━━━━━━━\n"
        "{items_block}\n"
        "Total: {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 ડિલિવરી {delivery_time} માં\n"
        "━━━━━━━━━━━━━━━\n"
        "રિપ્લાય કરો:\n"
        "1️⃣ Confirm & pay\n"
        "2️⃣ બીજી આઇટમ ઉમેરો\n"
        "3️⃣ Cancel"
    ),
    "order_confirmed": "ઓર્ડર કન્ફર્મ થઈ ગયું! ✅ {qty} × {product} = {total}. {delivery_time} માં ડિલિવરી.",
    "order_confirmed_cod": (
        "🎉 તમારો ઓર્ડર પ્લેસ થઈ ગયો! ✅\n"
        "📦 {product} {variant_part}× {qty} = {total}\n"
        "🚚 ડિલિવરી {delivery_time} માં\n\n"
        "શોપિંગ કરવા બદલ આભાર! 😊\n"
        "{catalogue_line}"
    ),
    "order_confirmed_paid": (
        "🎉 તમારો ઓર્ડર પ્લેસ થઈ ગયો! ✅\n"
        "📦 {product} {variant_part}× {qty} = {total}\n"
        "🚚 ડિલિવરી {delivery_time} માં\n\n"
        "શોપિંગ કરવા બદલ આભાર! 😊\n"
        "{catalogue_line}"
    ),
    "order_confirmed_cod_cart": (
        "🎉 તમારો ઓર્ડર પ્લેસ થઈ ગયો! ✅\n"
        "{items_block}\n"
        "Total: {total}\n"
        "🚚 ડિલિવરી {delivery_time} માં\n\n"
        "શોપિંગ કરવા બદલ આભાર! 😊\n"
        "{catalogue_line}"
    ),
    "order_confirmed_paid_cart": (
        "🎉 તમારો ઓર્ડર પ્લેસ થઈ ગયો! ✅\n"
        "{items_block}\n"
        "Total: {total}\n"
        "🚚 ડિલિવરી {delivery_time} માં\n\n"
        "શોપિંગ કરવા બદલ આભાર! 😊\n"
        "{catalogue_line}"
    ),
    "already_confirmed": "તમારો ઓર્ડર કન્ફર્મ થઈ ગયો છે ✅ કોઈ મદદ જોઈએ?",
    "interrupt_switch": (
        "તમારો એક ઓર્ડર ચાલી રહ્યો છે: {current_qty}x {current_product}{current_total_text}.\n"
        "{new_product} [{new_sku}] ₹{new_price} માં છે.\n"
        "'continue' રિપ્લાય કરો તમારો હાલનો ઓર્ડર પૂરો કરવા, "
        "અથવા 'switch' કરો {new_product} માટે નવો ઓર્ડર શરૂ કરવા "
        "(હાલની પ્રગતિ ડિલીટ થશે)."
    ),
    "switch_reask": (
        "{current_product} નો ઓર્ડર ચાલી રહ્યો છે.\n"
        "'continue' રિપ્લાય કરો પૂરો કરવા, અથવા 'switch' કરો નવો ઓર્ડર કરવા."
    ),
    "cancel_ack": "કોઈ વાત નહીં! બીજી કોઈ મદદ જોઈએ? અમારું કેટલોગ જોઈ શકો છો.",
    "discount_policy": (
        "અમારા ભાવ ફિક્સ છે, પણ ક્વોલિટી ગેરંટી છે. "
        "શું તમે ઓર્ડર ચાલુ રાખવા માંગો છો?"
    ),
    "out_of_stock": "{product} અત્યારે સ્ટોકમાં નથી. બીજું કોઈ પ્રોડક્ટ જોવું છે?",
    "out_of_stock_block": (
        "માફ કરશો, {product} અત્યારે સ્ટોકમાં નથી. "
        "બીજું કોઈ પ્રોડક્ટ જોવું છે?"
    ),
    "delivery_info": "આખા ભારતમાં ડિલિવરી {delivery_time} માં.",
    "off_topic": "હું ફક્ત {business} ના પ્રોડક્ટ્સ વિશે મદદ કરી શકું છું.",
    "which_item": "હું તમને {business} નું સાચું પ્રોડક્ટ શોધવા મદદ કરીશ! તમને કયું આઇટમ જોઈએ છે?",
    "pinned_availability": "{name} [{sku}] — ₹{price} માં ઉપલબ્ધ છે.{color_part}{size_part}\n\nશું તમે ઓર્ડર કરવા માંગો છો? (હા / ના)",
    "pinned_availability_guard": "{name} [{sku}] — ₹{price} માં ઉપલબ્ધ છે.{color_part}{size_part} ઓર્ડર માટે Yes, બ્રાઉઝિંગ ચાલુ રાખવા No રિપ્લાય કરો. શું તમે ઓર્ડર કરવા માંગો છો?",
    "available_colors_suffix": " {colors} માં ઉપલબ્ધ.",
    "available_sizes_suffix": " સાઈઝ: {sizes}.",
    "known_fact_delivery_time": "ડિલિવરીમાં 3–7 બિઝનેસ ડેઝ લાગે છે.",
    "known_fact_payment": "અમે ફક્ત UPI થી પેમેન્ટ સ્વીકારીએ છીએ.",
    "known_fact_quality": "અમારા બધા પ્રોડક્ટ્સ ડિસ્પેચ પહેલા ક્વોલિટી ચેક થાય છે.",
    "aside_question_no_info": "માફ કરશો, હાલમાં મારી પાસે આ ચોક્કસ માહિતી નથી — કૃપા કરી અમારો કેટલોગ જુઓ અથવા અમારી ટીમને પૂછો.",
    "would_you_like_to_order_named": "શું તમે {name} [{sku}] ઓર્ડર કરવા માંગો છો? (હા / ના)",
    "would_you_like_to_order_generic": "શું તમે ઓર્ડર કરવા માંગો છો? (હા / ના)",
    "browse_list_header": "આ રહ્યા અમારા વિકલ્પો:",
    "browse_list_footer": "કયું પસંદ છે?",
    "not_carry_have_instead": "માફ કરશો, એ અમારી પાસે નથી. આ ઉપલબ્ધ છે:",
    "available_colors_line": "ઉપલબ્ધ કલર: {colors}",
    "available_sizes_line": "ઉપલબ્ધ સાઈઝ: {sizes}",
    "off_topic_idle": (
        "{contact_line}"
        "આજ શું ખરીદવાનું છે? 🙂"
    ),
    "off_topic_midorder": "પહેલા તમારો ઓર્ડર પૂરો કરીએ 🙂\n\n{slot_question}",
    "order_status": (
        "ઓર્ડર #{order_id}: {product} {variant_part}× {qty} = {total}.\n"
        "સ્ટેટસ: {status}.\n"
        "ડિલિવરી: {address}.\n"
        "{delivery_time}"
    ),
    "no_orders": (
        "તમારો કોઈ ઓર્ડર નથી અત્યારે. "
        "અમારું કેટલોગ જોઈ શકો છો? {catalogue_url}"
    ),
    "quantity_exceeds_stock": (
        "માફ કરશો, {product} ના ફક્ત {stock} પીસ ઉપલબ્ધ છે. "
        "તમે કેટલા લેશો (1–{stock})?"
    ),
}

TEMPLATES: dict[str, dict[str, str]] = {
    "english": ENGLISH_TEMPLATES,
    "hindi_roman": HINDI_TEMPLATES,
    "hindi_devanagari": HINDI_DEVANAGARI_TEMPLATES,
    "hinglish": HINDI_TEMPLATES,
    "gujarati_roman": GUJARATI_TEMPLATES,
    "gujarati_script": GUJARATI_SCRIPT_TEMPLATES,
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
