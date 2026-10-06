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
    "rt_order_open_note": "Your order for {product} is still open — answer the last question to continue, or type 'cancel' to stop it.",
    "rt_card": "{name} [{sku}] — {price}.{options_line}",
    # ── ROUTER_V2 (LLM intent router) replies: deterministic, engine-written ──
    "rt_clarify_two": "Did you mean {a} or {b}? Please reply with the product name or code.",
    "rt_clarify_open": "Sorry, I didn't quite get that. What are you looking for? You can type a product name or code, or 'order status'.",
    "rt_not_found": "Sorry, we don't have an exact match for that right now.",
    "rt_alternatives_header": "Here are the closest options we have:",
    "rt_search_header": "Here's what we have 👇",
    "rt_search_footer": "Reply with a number or product code to see details.",
    "rt_browse_more": "You can browse everything here: {catalogue_url}",
    "rt_handoff": "Our team will reply here shortly.",
    "rt_handoff_offer": "I don't have a confirmed answer for that. If you'd like our team to help, type 'talk to someone'.",
    "rt_general_fallback": "Happy to help! You can type a product name or code, or 'order status'.",
    "rt_status_new": "Received",
    "rt_status_pending_payment": "Waiting for payment",
    "rt_status_payment_submitted": "Payment under verification",
    "rt_status_confirmed": "Confirmed ✅",
    "rt_status_paid": "Confirmed ✅ (payment received)",
    "rt_status_processing": "Being prepared",
    "rt_status_dispatched": "Dispatched 🚚",
    "rt_status_delivered": "Delivered ✅",
    "rt_status_cancelled": "Cancelled",
    "rt_order_line": "🧾 Order #{order_number} — {status}",
    "rt_eta_window": "📦 Expected delivery: {date_from} – {date_to}.",
    "rt_eta_awaiting": "📦 Delivery ({days}) starts once your payment is verified.",
    "rt_eta_overdue": "📦 It's taking longer than the usual {days}. Type 'talk to someone' and our team will check on it.",
    "rt_tracking": "🚚 Courier: {courier} · Tracking no.: {tracking}",
    "rt_delivered_on": "✅ Delivered on {date}.",
    "rt_cancelled_line": "This order was cancelled.",
    "rt_pay_received": "💳 Payment received ✅ ({date}).",
    "rt_pay_cod": "💵 Cash on Delivery — please pay {amount} when it arrives.",
    "rt_items_head": "🧾 Order #{order_number}\n{items}\nTotal: {total}",
    "rt_days_range": "{lo}–{hi} business days",
    "rt_days_fixed": "{n} business days",
    "rt_variant_yes": "Yes, {option} is available for {product} ✅",
    "rt_variant_no": "Sorry, {option} isn't available for {product}.{options_line}",
    "rt_opt_sizes": " Available sizes: {options}.",
    "rt_opt_colors": " Available colors: {options}.",
    "rt_opt_out": " It's currently out of stock.",
    "rt_order_cta": "Want to order it?",
    "rt_cancel_ok": "Order cancelled. ✅ Anything else I can help you with?",
    "rt_cancel_verifying": "Your payment screenshot is already with our team for verification, so this order can't be cancelled here. Type 'talk to someone' and we'll help.",
    "rt_cancel_not_allowed": "Order #{order_number} is already '{status}', so it can't be cancelled here. Type 'talk to someone' and our team will help.",
    "rt_cancel_none": "You don't have an active order to cancel. Type a product name or code to start a new one.",
    "rt_slot_updated": "Updated ✅ {label}: {value}",
    "rt_label_color": "Colour",
    "rt_label_size": "Size",
    "rt_label_material": "Material",
    "rt_label_quantity": "Quantity",
    "rt_label_name": "Name",
    "rt_label_address": "Address",
    "rt_label_phone": "Phone",
    "rt_label_payment_method": "Payment",
    "rt_slot_invalid": "Sorry, that isn't one of the available options.",
    "rt_change_not_now": "This order has already been placed, so I can't edit it here. Type 'talk to someone' and our team will help.",
    "rt_change_no_order": "There's no order in progress to change. Type a product name or code to start one.",
    "rt_change_multi": "This order has several different items, which I can't edit here. Type 'talk to someone' and our team will help.",
    "rt_combo_oos": "{product} isn't available in {option}.{options_line}",
    "pay_unavailable": "Thanks! Payment details for this order are being set up — the seller will message you here shortly.",
    "llm_unavailable_rephrase": "Sorry, could you rephrase? Or type a product code, or 'order status'.",
    # ── Manual UPI payment verification (deterministic; engine acts, LLM never writes these) ──
    "pay_instruction": "🧾 Order #{order_number}\n{items}\nTotal: {amount}\n\nPay to UPI ID: {upi_id}{payee_line}\n(GPay / PhonePe / Paytm / any UPI app){extra}",
    "pay_send_screenshot": "After payment, please send the payment screenshot here.",
    "pay_proof_received": "Thanks! Our team will verify your payment shortly and update you here.",
    "pay_proof_more": "Received, verification in progress.",
    "pay_ask_screenshot": "Please send the payment screenshot here so we can verify it. 📸",
    "pay_confirmed": "Payment confirmed ✅ Your order #{order_number} is confirmed.",
    "pay_rejected": "We couldn't confirm your payment{reason_part}. Please check and send the screenshot again.",
    "pay_cancelled": "Your order #{order_number} has been cancelled.{reason_part}",
    "order_status_summary": "🧾 Order #{order_number}\n{items}\nTotal: {total}\nStatus: {status}",
    "order_status_pay_reminder": "⏳ Payment pending: please pay {amount} to UPI ID {upi_id} and send the payment screenshot here so we can confirm your order.",
    "order_status_id_not_found": "I couldn't find order #{order_number} on this number. Please check the order ID, or send 'order status' to see your latest order.",
    "greeting": "Welcome to {business}! What are you looking for today?",
    "greeting_new": "Welcome to {business}! 👋 Here's our catalogue: {catalogue_url}\nWhat are you looking for today?",
    "greeting_returning": "Welcome back {name}! 👋 Here's our latest collection: {catalogue_url}\nWhat are you looking for today?",
    "greeting_resume_slot": "Welcome back! You were looking at {product} — {question}\nOr type a new product name.",
    "greeting_resume_product": "Welcome back! You were looking at {product}. Tell me if you'd like to order it, or type a new product name.",
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
    "rt_order_open_note": "{product} ka aapka order abhi khula hai — jaari rakhne ke liye aakhri sawaal ka jawab dein, ya rokne ke liye 'cancel' likhein.",
    "rt_card": "{name} [{sku}] — {price}.{options_line}",
    # ── ROUTER_V2 (LLM intent router) replies: deterministic, engine-written ──
    "rt_clarify_two": "Kya aapka matlab {a} ya {b} hai? Kripya product ka naam ya code bhejein.",
    "rt_clarify_open": "Maaf kijiye, main samajh nahi paya. Aap kya dhoondh rahe hain? Product ka naam ya code likhein, ya 'order status'.",
    "rt_not_found": "Maaf kijiye, abhi iske liye exact match nahi mila.",
    "rt_alternatives_header": "Ye sabse milte-julte options hain:",
    "rt_search_header": "Ye hamare paas hai 👇",
    "rt_search_footer": "Details dekhne ke liye number ya product code bhejein.",
    "rt_browse_more": "Poora collection yahan dekhein: {catalogue_url}",
    "rt_handoff": "Hamari team yahin jald hi reply karegi.",
    "rt_handoff_offer": "Iska pakka jawab mere paas nahi hai. Agar aap chahte hain ki hamari team madad kare, toh 'talk to someone' likhein.",
    "rt_general_fallback": "Zaroor madad karunga! Product ka naam ya code likhein, ya 'order status'.",
    "rt_status_new": "Mil gaya",
    "rt_status_pending_payment": "Payment ka intezaar",
    "rt_status_payment_submitted": "Payment verify ho raha hai",
    "rt_status_confirmed": "Confirm ✅",
    "rt_status_paid": "Confirm ✅ (payment mil gaya)",
    "rt_status_processing": "Taiyaar ho raha hai",
    "rt_status_dispatched": "Dispatch ho gaya 🚚",
    "rt_status_delivered": "Deliver ho gaya ✅",
    "rt_status_cancelled": "Cancel ho gaya",
    "rt_order_line": "🧾 Order #{order_number} — {status}",
    "rt_eta_window": "📦 Delivery ka anumaan: {date_from} – {date_to}.",
    "rt_eta_awaiting": "📦 Aapka payment verify hone ke baad delivery ({days}) shuru hogi.",
    "rt_eta_overdue": "📦 Delivery mein normal {days} se zyada samay lag raha hai. 'talk to someone' likhein, hamari team check karegi.",
    "rt_tracking": "🚚 Courier: {courier} · Tracking no.: {tracking}",
    "rt_delivered_on": "✅ {date} ko deliver ho gaya.",
    "rt_cancelled_line": "Ye order cancel ho chuka hai.",
    "rt_pay_received": "💳 Payment mil gaya ✅ ({date}).",
    "rt_pay_cod": "💵 Cash on Delivery — saaman aane par {amount} dein.",
    "rt_items_head": "🧾 Order #{order_number}\n{items}\nKul: {total}",
    "rt_days_range": "{lo}–{hi} working days",
    "rt_days_fixed": "{n} working days",
    "rt_variant_yes": "Haan, {product} mein {option} available hai ✅",
    "rt_variant_no": "Maaf kijiye, {product} mein {option} available nahi hai.{options_line}",
    "rt_opt_sizes": " Available sizes: {options}.",
    "rt_opt_colors": " Available colors: {options}.",
    "rt_opt_out": " Abhi stock mein nahi hai.",
    "rt_order_cta": "Order karna chahenge?",
    "rt_cancel_ok": "Order cancel kar diya gaya. ✅ Kya main kuch aur help kar sakta hoon?",
    "rt_cancel_verifying": "Aapka payment screenshot verification ke liye hamari team ke paas hai, isliye ye order yahan cancel nahi ho sakta. 'talk to someone' likhein, hum madad karenge.",
    "rt_cancel_not_allowed": "Order #{order_number} ab '{status}' hai, isliye yahan cancel nahi ho sakta. 'talk to someone' likhein, hamari team madad karegi.",
    "rt_cancel_none": "Abhi koi active order nahi hai jise cancel kiya ja sake. Naya order shuru karne ke liye product ka naam ya code likhein.",
    "rt_slot_updated": "Update ho gaya ✅ {label}: {value}",
    "rt_label_color": "Colour",
    "rt_label_size": "Size",
    "rt_label_material": "Material",
    "rt_label_quantity": "Quantity",
    "rt_label_name": "Naam",
    "rt_label_address": "Address",
    "rt_label_phone": "Phone",
    "rt_label_payment_method": "Payment",
    "rt_slot_invalid": "Maaf kijiye, ye available options mein nahi hai.",
    "rt_change_not_now": "Ye order ho chuka hai, isliye yahan badla nahi ja sakta. 'talk to someone' likhein, hamari team madad karegi.",
    "rt_change_no_order": "Abhi koi order chal nahi raha jise badla ja sake. Naya order shuru karne ke liye product ka naam ya code likhein.",
    "rt_change_multi": "Is order mein kai alag items hain, jo yahan edit nahi ho sakte. 'talk to someone' likhein, hamari team madad karegi.",
    "rt_combo_oos": "{product} mein {option} available nahi hai.{options_line}",
    "pay_unavailable": "Shukriya! Is order ke payment details set ho rahe hain — seller jaldi hi yahan message karenge.",
    "llm_unavailable_rephrase": "Sorry, kya aap dobara likh sakte hain? Ya product code ya 'order status' type karein.",
    "pay_instruction": "🧾 Order #{order_number}\n{items}\nTotal: {amount}\n\nUPI ID par payment karein: {upi_id}{payee_line}\n(GPay / PhonePe / Paytm / koi bhi UPI app){extra}",
    "pay_send_screenshot": "Payment karne ke baad, kripya payment ka screenshot yahan bhejein.",
    "pay_proof_received": "Shukriya! Hamari team aapka payment jaldi verify karke yahin update degi.",
    "pay_proof_more": "Mil gaya, verification chal raha hai.",
    "pay_ask_screenshot": "Kripya payment ka screenshot yahan bhejein taaki hum verify kar sakein. 📸",
    "pay_confirmed": "Payment confirm ho gaya ✅ Aapka order #{order_number} confirm hai.",
    "pay_rejected": "Hum aapka payment confirm nahi kar paaye{reason_part}. Kripya check karke screenshot dobara bhejein.",
    "pay_cancelled": "Aapka order #{order_number} cancel kar diya gaya hai.{reason_part}",
    "order_status_summary": "🧾 Order #{order_number}\n{items}\nKul: {total}\nStatus: {status}",
    "order_status_pay_reminder": "⏳ Payment pending hai: kripya {amount} UPI ID {upi_id} par bhejein aur payment ka screenshot yahan bhejein taaki hum order confirm kar sakein.",
    "order_status_id_not_found": "Is number par order #{order_number} nahi mila. Kripya order ID check karein, ya latest order dekhne ke liye 'order status' bhejein.",
    "greeting": "{business} mein aapka swagat hai! Aaj kya dekhna chahenge?",
    "greeting_new": "{business} mein aapka swagat hai! 👋 Hamara catalogue yahan dekhein: {catalogue_url}\nAaj kya dekhna chahenge?",
    "greeting_returning": "Wapas aaye {name}! 👋 Hamara latest collection yahan hai: {catalogue_url}\nAaj kya dekhna chahenge?",
    "greeting_resume_slot": "Wapas aaye! Aap {product} dekh rahe the — {question}\nYa naye product ka naam likhein.",
    "greeting_resume_product": "Wapas aaye! Aap {product} dekh rahe the. Order karna ho toh bataiye, ya naye product ka naam likhein.",
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
    "rt_order_open_note": "{product} no tamaro order hajuy chalu chhe — chalu rakhva chhella sawal no jawab aapo, athva rokva 'cancel' lakho.",
    "rt_card": "{name} [{sku}] — {price}.{options_line}",
    # ── ROUTER_V2 (LLM intent router) replies: deterministic, engine-written ──
    "rt_clarify_two": "Shu tamaro matlab {a} ke {b} chhe? Krupa kari ne product nu naam ke code moklo.",
    "rt_clarify_open": "Maaf karjo, mane samajaayu nahi. Tame shu shodhi rahya chho? Product nu naam ke code lakho, athva 'order status'.",
    "rt_not_found": "Maaf karjo, hamna aa mate exact match malyu nathi.",
    "rt_alternatives_header": "Aa sauthi malta aavta options chhe:",
    "rt_search_header": "Amari paase aa chhe 👇",
    "rt_search_footer": "Details jova mate number ke product code moklo.",
    "rt_browse_more": "Puru collection ahi juo: {catalogue_url}",
    "rt_handoff": "Amari team ahi j thodi var ma reply karshe.",
    "rt_handoff_offer": "Aa vishe mari paase pakku jawab nathi. Jo tame icho cho ke amari team madad kare, to 'talk to someone' lakho.",
    "rt_general_fallback": "Jarur madad karish! Product nu naam ke code lakho, athva 'order status'.",
    "rt_status_new": "Malyo",
    "rt_status_pending_payment": "Payment ni raah jovai chhe",
    "rt_status_payment_submitted": "Payment verify thai rahyu chhe",
    "rt_status_confirmed": "Confirm ✅",
    "rt_status_paid": "Confirm ✅ (payment malyu)",
    "rt_status_processing": "Taiyar thai rahyo chhe",
    "rt_status_dispatched": "Dispatch thai gayo 🚚",
    "rt_status_delivered": "Deliver thai gayo ✅",
    "rt_status_cancelled": "Cancel thayo",
    "rt_order_line": "🧾 Order #{order_number} — {status}",
    "rt_eta_window": "📦 Delivery no anumaan: {date_from} – {date_to}.",
    "rt_eta_awaiting": "📦 Tamaru payment verify thaya pachi delivery ({days}) shuru thase.",
    "rt_eta_overdue": "📦 Delivery ma saamanya {days} karta vadhu samay lagi rahyo chhe. 'talk to someone' lakho, amari team check karshe.",
    "rt_tracking": "🚚 Courier: {courier} · Tracking no.: {tracking}",
    "rt_delivered_on": "✅ {date} na roj deliver thayo.",
    "rt_cancelled_line": "Aa order cancel thai gayo chhe.",
    "rt_pay_received": "💳 Payment malyu ✅ ({date}).",
    "rt_pay_cod": "💵 Cash on Delivery — saaman aave tyare {amount} aapo.",
    "rt_items_head": "🧾 Order #{order_number}\n{items}\nKul: {total}",
    "rt_days_range": "{lo}–{hi} working days",
    "rt_days_fixed": "{n} working days",
    "rt_variant_yes": "Ha, {product} ma {option} available chhe ✅",
    "rt_variant_no": "Maaf karjo, {product} ma {option} available nathi.{options_line}",
    "rt_opt_sizes": " Available sizes: {options}.",
    "rt_opt_colors": " Available colors: {options}.",
    "rt_opt_out": " Hamna stock ma nathi.",
    "rt_order_cta": "Order karvo chhe?",
    "rt_cancel_ok": "Order cancel thai gayu. ✅ Koi biju kaam hoy to kaho!",
    "rt_cancel_verifying": "Tamaro payment screenshot verification mate amari team pase chhe, etle aa order ahi cancel nahi thai shake. 'talk to someone' lakho, ame madad karishu.",
    "rt_cancel_not_allowed": "Order #{order_number} have '{status}' chhe, etle ahi cancel nahi thai shake. 'talk to someone' lakho, amari team madad karshe.",
    "rt_cancel_none": "Hamna koi active order nathi je cancel kari shakay. Navo order shuru karva product nu naam ke code lakho.",
    "rt_slot_updated": "Update thai gayu ✅ {label}: {value}",
    "rt_label_color": "Colour",
    "rt_label_size": "Size",
    "rt_label_material": "Material",
    "rt_label_quantity": "Quantity",
    "rt_label_name": "Naam",
    "rt_label_address": "Address",
    "rt_label_phone": "Phone",
    "rt_label_payment_method": "Payment",
    "rt_slot_invalid": "Maaf karjo, aa available options ma nathi.",
    "rt_change_not_now": "Aa order thai gayo chhe, etle ahi badli nahi shakay. 'talk to someone' lakho, amari team madad karshe.",
    "rt_change_no_order": "Hamna koi order chalu nathi je badli shakay. Navo order shuru karva product nu naam ke code lakho.",
    "rt_change_multi": "Aa order ma ghana alag items chhe, je ahi edit nahi thai shake. 'talk to someone' lakho, amari team madad karshe.",
    "rt_combo_oos": "{product} ma {option} available nathi.{options_line}",
    "pay_unavailable": "Aabhar! Aa order na payment details set thai rahya chhe — seller jaldi ahi message karshe.",
    "llm_unavailable_rephrase": "Maaf karjo, shu tame fari thi lakhi shako? Athva product code athva 'order status' lakho.",
    "pay_instruction": "🧾 Order #{order_number}\n{items}\nKul: {amount}\n\nUPI ID par payment karo: {upi_id}{payee_line}\n(GPay / PhonePe / Paytm / koi pan UPI app){extra}",
    "pay_send_screenshot": "Payment karya pachhi, krupa kari ne payment no screenshot ahi moklo.",
    "pay_proof_received": "Aabhar! Amari team tamaru payment jaldi verify kari ne ahi j update api dese.",
    "pay_proof_more": "Mali gayu, verification chaalu chhe.",
    "pay_ask_screenshot": "Krupa kari ne payment no screenshot ahi moklo jethi ame verify kari shakiye. 📸",
    "pay_confirmed": "Payment confirm thai gayu ✅ Tamaro order #{order_number} confirm chhe.",
    "pay_rejected": "Ame tamaru payment confirm kari shakya nahi{reason_part}. Krupa kari ne check karine screenshot pharithi moklo.",
    "pay_cancelled": "Tamaro order #{order_number} cancel karvama aavyo chhe.{reason_part}",
    "order_status_summary": "🧾 Order #{order_number}\n{items}\nKul: {total}\nStatus: {status}",
    "order_status_pay_reminder": "⏳ Payment baki chhe: krupa kari ne {amount} UPI ID {upi_id} par moklo ane payment no screenshot ahi moklo jethi ame order confirm kari shakiye.",
    "order_status_id_not_found": "Aa number par order #{order_number} malyo nathi. Krupa kari ne order ID check karo, athva latest order jova mate 'order status' moklo.",
    "greeting": "{business} maa aapanu swagat chhe! Aaj shu joivanu chhe?",
    "greeting_new": "{business} maa aapanu swagat chhe! 👋 Amaru catalogue joi lo: {catalogue_url}\nAaj shu joivanu chhe?",
    "greeting_returning": "Pachi avya {name}! 👋 Amaru latest collection joi lo: {catalogue_url}\nAaj shu joivanu chhe?",
    "greeting_resume_slot": "Pacha aavya! Tame {product} joi rahya hata — {question}\nAthva navu product naam lakho.",
    "greeting_resume_product": "Pacha aavya! Tame {product} joi rahya hata. Order karvu hoy to kaho, athva navu product naam lakho.",
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
    "rt_order_open_note": "{product} का आपका ऑर्डर अभी खुला है — जारी रखने के लिए आख़िरी सवाल का जवाब दें, या रोकने के लिए 'cancel' लिखें।",
    "rt_card": "{name} [{sku}] — {price}।{options_line}",
    # ── ROUTER_V2 (LLM intent router) replies: deterministic, engine-written ──
    "rt_clarify_two": "क्या आपका मतलब {a} या {b} है? कृपया प्रोडक्ट का नाम या कोड भेजें।",
    "rt_clarify_open": "क्षमा करें, मैं समझ नहीं पाया। आप क्या ढूँढ रहे हैं? प्रोडक्ट का नाम या कोड लिखें, या 'order status'।",
    "rt_not_found": "क्षमा करें, अभी इसके लिए सटीक मैच नहीं मिला।",
    "rt_alternatives_header": "ये सबसे मिलते-जुलते विकल्प हैं:",
    "rt_search_header": "हमारे पास ये है 👇",
    "rt_search_footer": "विवरण देखने के लिए नंबर या प्रोडक्ट कोड भेजें।",
    "rt_browse_more": "पूरा कलेक्शन यहाँ देखें: {catalogue_url}",
    "rt_handoff": "हमारी टीम यहीं जल्द ही जवाब देगी।",
    "rt_handoff_offer": "इसका पक्का जवाब मेरे पास नहीं है। अगर आप चाहते हैं कि हमारी टीम मदद करे, तो 'किसी से बात करनी है' लिखें।",
    "rt_general_fallback": "ज़रूर मदद करूँगा! प्रोडक्ट का नाम या कोड लिखें, या 'order status'।",
    "rt_status_new": "मिल गया",
    "rt_status_pending_payment": "पेमेंट का इंतज़ार",
    "rt_status_payment_submitted": "पेमेंट वेरिफ़ाई हो रहा है",
    "rt_status_confirmed": "कन्फर्म ✅",
    "rt_status_paid": "कन्फर्म ✅ (पेमेंट मिल गया)",
    "rt_status_processing": "तैयार हो रहा है",
    "rt_status_dispatched": "भेज दिया गया 🚚",
    "rt_status_delivered": "डिलीवर हो गया ✅",
    "rt_status_cancelled": "कैंसल हो गया",
    "rt_order_line": "🧾 ऑर्डर #{order_number} — {status}",
    "rt_eta_window": "📦 डिलीवरी का अनुमान: {date_from} – {date_to}।",
    "rt_eta_awaiting": "📦 आपका पेमेंट वेरिफ़ाई होने के बाद डिलीवरी ({days}) शुरू होगी।",
    "rt_eta_overdue": "📦 डिलीवरी में सामान्य {days} से ज़्यादा समय लग रहा है। 'किसी से बात करनी है' लिखें, हमारी टीम जाँच करेगी।",
    "rt_tracking": "🚚 कूरियर: {courier} · ट्रैकिंग नं.: {tracking}",
    "rt_delivered_on": "✅ {date} को डिलीवर हो गया।",
    "rt_cancelled_line": "यह ऑर्डर कैंसल हो चुका है।",
    "rt_pay_received": "💳 पेमेंट मिल गया ✅ ({date})।",
    "rt_pay_cod": "💵 कैश ऑन डिलीवरी — सामान आने पर {amount} दें।",
    "rt_items_head": "🧾 ऑर्डर #{order_number}\n{items}\nकुल: {total}",
    "rt_days_range": "{lo}–{hi} कार्यदिवस",
    "rt_days_fixed": "{n} कार्यदिवस",
    "rt_variant_yes": "हाँ, {product} में {option} उपलब्ध है ✅",
    "rt_variant_no": "क्षमा करें, {product} में {option} उपलब्ध नहीं है।{options_line}",
    "rt_opt_sizes": " उपलब्ध साइज़: {options}।",
    "rt_opt_colors": " उपलब्ध रंग: {options}।",
    "rt_opt_out": " अभी स्टॉक में नहीं है।",
    "rt_order_cta": "ऑर्डर करना चाहेंगे?",
    "rt_cancel_ok": "ऑर्डर कैंसल कर दिया गया। ✅ क्या मैं कुछ और मदद कर सकता हूँ?",
    "rt_cancel_verifying": "आपका पेमेंट स्क्रीनशॉट वेरिफ़िकेशन के लिए हमारी टीम के पास है, इसलिए यह ऑर्डर यहाँ कैंसल नहीं हो सकता। 'किसी से बात करनी है' लिखें, हम मदद करेंगे।",
    "rt_cancel_not_allowed": "ऑर्डर #{order_number} अब '{status}' है, इसलिए यहाँ कैंसल नहीं हो सकता। 'किसी से बात करनी है' लिखें, हमारी टीम मदद करेगी।",
    "rt_cancel_none": "अभी कोई सक्रिय ऑर्डर नहीं है जिसे कैंसल किया जा सके। नया ऑर्डर शुरू करने के लिए प्रोडक्ट का नाम या कोड लिखें।",
    "rt_slot_updated": "अपडेट हो गया ✅ {label}: {value}",
    "rt_label_color": "रंग",
    "rt_label_size": "साइज़",
    "rt_label_material": "मटेरियल",
    "rt_label_quantity": "मात्रा",
    "rt_label_name": "नाम",
    "rt_label_address": "पता",
    "rt_label_phone": "फ़ोन",
    "rt_label_payment_method": "पेमेंट",
    "rt_slot_invalid": "क्षमा करें, यह उपलब्ध विकल्पों में नहीं है।",
    "rt_change_not_now": "यह ऑर्डर हो चुका है, इसलिए यहाँ बदला नहीं जा सकता। 'किसी से बात करनी है' लिखें, हमारी टीम मदद करेगी।",
    "rt_change_no_order": "अभी कोई ऑर्डर चल नहीं रहा जिसे बदला जा सके। नया ऑर्डर शुरू करने के लिए प्रोडक्ट का नाम या कोड लिखें।",
    "rt_change_multi": "इस ऑर्डर में कई अलग आइटम हैं, जो यहाँ एडिट नहीं हो सकते। 'किसी से बात करनी है' लिखें, हमारी टीम मदद करेगी।",
    "rt_combo_oos": "{product} में {option} उपलब्ध नहीं है।{options_line}",
    "pay_unavailable": "धन्यवाद! इस ऑर्डर की पेमेंट डिटेल्स सेट हो रही हैं — विक्रेता जल्द ही यहाँ संदेश करेंगे।",
    "llm_unavailable_rephrase": "क्षमा करें, क्या आप दोबारा लिख सकते हैं? या प्रोडक्ट कोड या 'order status' टाइप करें।",
    "pay_instruction": "🧾 ऑर्डर #{order_number}\n{items}\nकुल: {amount}\n\nUPI ID पर भुगतान करें: {upi_id}{payee_line}\n(GPay / PhonePe / Paytm / कोई भी UPI ऐप){extra}",
    "pay_send_screenshot": "भुगतान के बाद, कृपया पेमेंट का स्क्रीनशॉट यहाँ भेजें।",
    "pay_proof_received": "धन्यवाद! हमारी टीम जल्द ही आपका भुगतान वेरिफ़ाई करके यहीं अपडेट देगी।",
    "pay_proof_more": "मिल गया, वेरिफ़िकेशन जारी है।",
    "pay_ask_screenshot": "कृपया पेमेंट का स्क्रीनशॉट यहाँ भेजें ताकि हम वेरिफ़ाई कर सकें। 📸",
    "pay_confirmed": "पेमेंट कन्फर्म हो गया ✅ आपका ऑर्डर #{order_number} कन्फर्म है।",
    "pay_rejected": "हम आपका पेमेंट कन्फर्म नहीं कर पाए{reason_part}। कृपया जाँचकर स्क्रीनशॉट दोबारा भेजें।",
    "pay_cancelled": "आपका ऑर्डर #{order_number} रद्द कर दिया गया है।{reason_part}",
    "order_status_summary": "🧾 ऑर्डर #{order_number}\n{items}\nकुल: {total}\nस्टेटस: {status}",
    "order_status_pay_reminder": "⏳ पेमेंट बाकी है: कृपया {amount} UPI ID {upi_id} पर भेजें और पेमेंट का स्क्रीनशॉट यहाँ भेजें ताकि हम ऑर्डर कन्फर्म कर सकें।",
    "order_status_id_not_found": "इस नंबर पर ऑर्डर #{order_number} नहीं मिला। कृपया ऑर्डर ID जाँचें, या अपना लेटेस्ट ऑर्डर देखने के लिए 'order status' भेजें।",
    "greeting": "{business} में आपका स्वागत है! आज क्या देखना चाहेंगे?",
    "greeting_new": "{business} में आपका स्वागत है! 👋 हमारा कैटलॉग यहाँ देखें: {catalogue_url}\nआज क्या देखना चाहेंगे?",
    "greeting_returning": "वापस आए {name}! 👋 हमारा नया कलेक्शन यहाँ है: {catalogue_url}\nआज क्या देखना चाहेंगे?",
    "greeting_resume_slot": "वापस आए! आप {product} देख रहे थे — {question}\nया नए प्रोडक्ट का नाम लिखें।",
    "greeting_resume_product": "वापस आए! आप {product} देख रहे थे। ऑर्डर करना हो तो बताइए, या नए प्रोडक्ट का नाम लिखें।",
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
    "rt_order_open_note": "{product} નો તમારો ઓર્ડર હજુ ચાલુ છે — ચાલુ રાખવા છેલ્લા સવાલનો જવાબ આપો, અથવા રોકવા 'cancel' લખો.",
    "rt_card": "{name} [{sku}] — {price}.{options_line}",
    # ── ROUTER_V2 (LLM intent router) replies: deterministic, engine-written ──
    "rt_clarify_two": "શું તમારો મતલબ {a} કે {b} છે? કૃપા કરીને પ્રોડક્ટનું નામ અથવા કોડ મોકલો.",
    "rt_clarify_open": "માફ કરજો, મને સમજાયું નહીં. તમે શું શોધી રહ્યા છો? પ્રોડક્ટનું નામ અથવા કોડ લખો, અથવા 'order status'.",
    "rt_not_found": "માફ કરજો, હમણાં આ માટે બરાબર મેળ મળ્યો નથી.",
    "rt_alternatives_header": "આ સૌથી મળતા આવતા વિકલ્પો છે:",
    "rt_search_header": "અમારી પાસે આ છે 👇",
    "rt_search_footer": "વિગતો જોવા નંબર અથવા પ્રોડક્ટ કોડ મોકલો.",
    "rt_browse_more": "પૂરું કલેક્શન અહીં જુઓ: {catalogue_url}",
    "rt_handoff": "અમારી ટીમ અહીં જ થોડી વારમાં જવાબ આપશે.",
    "rt_handoff_offer": "આ વિશે મારી પાસે પાક્કો જવાબ નથી. જો તમે ઇચ્છો કે અમારી ટીમ મદદ કરે, તો 'કોઈ સાથે વાત કરવી છે' લખો.",
    "rt_general_fallback": "જરૂર મદદ કરીશ! પ્રોડક્ટનું નામ અથવા કોડ લખો, અથવા 'order status'.",
    "rt_status_new": "મળી ગયો",
    "rt_status_pending_payment": "પેમેન્ટની રાહ",
    "rt_status_payment_submitted": "પેમેન્ટ વેરિફાય થઈ રહ્યું છે",
    "rt_status_confirmed": "કન્ફર્મ ✅",
    "rt_status_paid": "કન્ફર્મ ✅ (પેમેન્ટ મળ્યું)",
    "rt_status_processing": "તૈયાર થઈ રહ્યો છે",
    "rt_status_dispatched": "મોકલી દીધો 🚚",
    "rt_status_delivered": "ડિલિવર થઈ ગયો ✅",
    "rt_status_cancelled": "કેન્સલ થયો",
    "rt_order_line": "🧾 ઓર્ડર #{order_number} — {status}",
    "rt_eta_window": "📦 ડિલિવરીનો અંદાજ: {date_from} – {date_to}.",
    "rt_eta_awaiting": "📦 તમારું પેમેન્ટ વેરિફાય થયા પછી ડિલિવરી ({days}) શરૂ થશે.",
    "rt_eta_overdue": "📦 ડિલિવરીમાં સામાન્ય {days} કરતાં વધુ સમય લાગી રહ્યો છે. 'કોઈ સાથે વાત કરવી છે' લખો, અમારી ટીમ તપાસ કરશે.",
    "rt_tracking": "🚚 કુરિયર: {courier} · ટ્રેકિંગ નં.: {tracking}",
    "rt_delivered_on": "✅ {date} ના રોજ ડિલિવર થયો.",
    "rt_cancelled_line": "આ ઓર્ડર કેન્સલ થઈ ગયો છે.",
    "rt_pay_received": "💳 પેમેન્ટ મળ્યું ✅ ({date}).",
    "rt_pay_cod": "💵 કેશ ઓન ડિલિવરી — સામાન આવે ત્યારે {amount} આપો.",
    "rt_items_head": "🧾 ઓર્ડર #{order_number}\n{items}\nકુલ: {total}",
    "rt_days_range": "{lo}–{hi} કામકાજના દિવસો",
    "rt_days_fixed": "{n} કામકાજના દિવસો",
    "rt_variant_yes": "હા, {product} માં {option} ઉપલબ્ધ છે ✅",
    "rt_variant_no": "માફ કરજો, {product} માં {option} ઉપલબ્ધ નથી.{options_line}",
    "rt_opt_sizes": " ઉપલબ્ધ સાઇઝ: {options}.",
    "rt_opt_colors": " ઉપલબ્ધ રંગ: {options}.",
    "rt_opt_out": " હમણાં સ્ટોકમાં નથી.",
    "rt_order_cta": "ઓર્ડર કરવો છે?",
    "rt_cancel_ok": "ઓર્ડર કેન્સલ થઈ ગયો. ✅ કોઈ બીજું કામ હોય તો કહો!",
    "rt_cancel_verifying": "તમારો પેમેન્ટ સ્ક્રીનશૉટ વેરિફિકેશન માટે અમારી ટીમ પાસે છે, એટલે આ ઓર્ડર અહીં કેન્સલ થઈ શકતો નથી. 'કોઈ સાથે વાત કરવી છે' લખો, અમે મદદ કરીશું.",
    "rt_cancel_not_allowed": "ઓર્ડર #{order_number} હવે '{status}' છે, એટલે અહીં કેન્સલ થઈ શકતો નથી. 'કોઈ સાથે વાત કરવી છે' લખો, અમારી ટીમ મદદ કરશે.",
    "rt_cancel_none": "હમણાં કોઈ સક્રિય ઓર્ડર નથી જે કેન્સલ કરી શકાય. નવો ઓર્ડર શરૂ કરવા પ્રોડક્ટનું નામ અથવા કોડ લખો.",
    "rt_slot_updated": "અપડેટ થઈ ગયું ✅ {label}: {value}",
    "rt_label_color": "રંગ",
    "rt_label_size": "સાઇઝ",
    "rt_label_material": "મટીરિયલ",
    "rt_label_quantity": "જથ્થો",
    "rt_label_name": "નામ",
    "rt_label_address": "સરનામું",
    "rt_label_phone": "ફોન",
    "rt_label_payment_method": "પેમેન્ટ",
    "rt_slot_invalid": "માફ કરજો, આ ઉપલબ્ધ વિકલ્પોમાં નથી.",
    "rt_change_not_now": "આ ઓર્ડર થઈ ગયો છે, એટલે અહીં બદલી શકાતો નથી. 'કોઈ સાથે વાત કરવી છે' લખો, અમારી ટીમ મદદ કરશે.",
    "rt_change_no_order": "હમણાં કોઈ ઓર્ડર ચાલુ નથી જે બદલી શકાય. નવો ઓર્ડર શરૂ કરવા પ્રોડક્ટનું નામ અથવા કોડ લખો.",
    "rt_change_multi": "આ ઓર્ડરમાં ઘણી અલગ આઇટમ છે, જે અહીં એડિટ થઈ શકતી નથી. 'કોઈ સાથે વાત કરવી છે' લખો, અમારી ટીમ મદદ કરશે.",
    "rt_combo_oos": "{product} માં {option} ઉપલબ્ધ નથી.{options_line}",
    "pay_unavailable": "આભાર! આ ઓર્ડરની પેમેન્ટ વિગતો સેટ થઈ રહી છે — વિક્રેતા જલ્દી અહીં મેસેજ કરશે.",
    "llm_unavailable_rephrase": "માફ કરજો, શું તમે ફરીથી લખી શકો? અથવા પ્રોડક્ટ કોડ અથવા 'order status' લખો.",
    "pay_instruction": "🧾 ઓર્ડર #{order_number}\n{items}\nકુલ: {amount}\n\nUPI ID પર ચુકવણી કરો: {upi_id}{payee_line}\n(GPay / PhonePe / Paytm / કોઈપણ UPI એપ){extra}",
    "pay_send_screenshot": "ચુકવણી કર્યા પછી, કૃપા કરીને પેમેન્ટનો સ્ક્રીનશૉટ અહીં મોકલો.",
    "pay_proof_received": "આભાર! અમારી ટીમ તમારું પેમેન્ટ જલ્દી વેરિફાય કરીને અહીં જ અપડેટ આપશે.",
    "pay_proof_more": "મળી ગયું, વેરિફિકેશન ચાલુ છે.",
    "pay_ask_screenshot": "કૃપા કરીને પેમેન્ટનો સ્ક્રીનશૉટ અહીં મોકલો જેથી અમે વેરિફાય કરી શકીએ. 📸",
    "pay_confirmed": "પેમેન્ટ કન્ફર્મ થઈ ગયું ✅ તમારો ઓર્ડર #{order_number} કન્ફર્મ છે.",
    "pay_rejected": "અમે તમારું પેમેન્ટ કન્ફર્મ કરી શક્યા નથી{reason_part}. કૃપા કરીને ચકાસીને સ્ક્રીનશૉટ ફરીથી મોકલો.",
    "pay_cancelled": "તમારો ઓર્ડર #{order_number} રદ કરવામાં આવ્યો છે.{reason_part}",
    "order_status_summary": "🧾 ઓર્ડર #{order_number}\n{items}\nકુલ: {total}\nસ્ટેટસ: {status}",
    "order_status_pay_reminder": "⏳ પેમેન્ટ બાકી છે: કૃપા કરીને {amount} UPI ID {upi_id} પર મોકલો અને પેમેન્ટનો સ્ક્રીનશૉટ અહીં મોકલો જેથી અમે ઓર્ડર કન્ફર્મ કરી શકીએ.",
    "order_status_id_not_found": "આ નંબર પર ઓર્ડર #{order_number} મળ્યો નથી. કૃપા કરીને ઓર્ડર ID તપાસો, અથવા તમારો લેટેસ્ટ ઓર્ડર જોવા 'order status' મોકલો.",
    "greeting": "{business} માં આપનું સ્વાગત છે! આજ શું જોઈવાનું છે?",
    "greeting_new": "{business} માં આપનું સ્વાગત છે! 👋 અમારું કેટલોગ જુઓ: {catalogue_url}\nઆજ શું જોઈવાનું છે?",
    "greeting_returning": "પાછા આવ્યા {name}! 👋 અમારું નવું કલેક્શન જુઓ: {catalogue_url}\nઆજ શું જોઈવાનું છે?",
    "greeting_resume_slot": "પાછા આવ્યા! તમે {product} જોઈ રહ્યા હતા — {question}\nઅથવા નવા પ્રોડક્ટનું નામ લખો.",
    "greeting_resume_product": "પાછા આવ્યા! તમે {product} જોઈ રહ્યા હતા. ઓર્ડર કરવો હોય તો કહો, અથવા નવા પ્રોડક્ટનું નામ લખો.",
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
