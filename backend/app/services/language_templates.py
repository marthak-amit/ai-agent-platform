"""
Language-isolated response templates for all supported languages.

All templates are keyed by a short action name. Use get_template() to
render a template for a given language with named keyword arguments.
NO template from one language may bleed into another — that is the entire
point of this file.
"""

from __future__ import annotations

import re


def format_price(p: float | int) -> str:
    """Format a price as integer rupees with thousands separator: ₹2,450."""
    return f"₹{int(round(p)):,}"


ENGLISH_TEMPLATES: dict[str, str] = {
    "would_you_like_to_order_switch": "Would you like to order {name}? (Yes / No)",
    "followup_default": "Hello! 🙏 You showed interest in our products. Your order is still pending — can we help you with anything? We're here! 😊",
    "ostatus_new": "New",
    "ostatus_pending_payment": "Pending payment",
    "ostatus_payment_submitted": "Payment under verification",
    "ostatus_confirmed": "Confirmed",
    "ostatus_paid": "Paid",
    "ostatus_processing": "Processing",
    "ostatus_dispatched": "Dispatched",
    "ostatus_delivered": "Delivered",
    "ostatus_cancelled": "Cancelled",
    "variant_slot_rejected": "Sorry, we don't have {value} in {slot}. Available {slot}: {opts}",
    "variant_slot_rejected_noecho": "Sorry, that isn't one of the available {slot} options: {opts}",
    "card_colors": "Available colors: {colors}",
    "card_sizes": "Available sizes: {sizes}",
    "card_intro": "Yes, we have this available:",
    "afc_no_match": "Couldn't find '{text}' — reply 1️⃣/2️⃣/3️⃣, or send the product name/code you want instead.",
    "switching_to": "Sure, switching to {product}. 👍",
    "injection_refusal": "I can only help with {business} products and orders. How can I assist you?",
    "off_topic_boundary": "I can only help with orders from {business}. Tap a product or type 'cancel' to start over.",
    "attr_size": "size",
    "attr_colour": "colour",
    "attr_material": "material",
    "saved_address_reprompt": "Please reply *yes* to use this address, or *change* to enter a new one.",
    "team_will_assist": "Our team will assist you shortly. 👋",
    "ask_new_address": "Sure — what's the new delivery address? Please send house/area, city and pincode.",
    "rt_clarify_slot": "Sorry, I didn't quite get that.",
    "catalogue_browse_more": "🛍️ Browse more: {url}",
    "catalogue_cross_sell": "🛍️ You might also like {name} ({price}) — {url}",
    "btn_pay_upi": "💳 Pay via UPI",
    "btn_pay_cod": "🚚 Cash on Delivery",
    "btn_pay_bank": "🏦 Bank Transfer",
    "btn_confirm_pay": "Confirm & Pay",
    "btn_cancel": "Cancel",
    "btn_yes": "Yes",
    "btn_no": "No",
    "btn_change_address": "Change",
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
    "switch_offer": "Would you like to order {product} instead of {pinned}? (Yes / No)",
    "rt_no_photo": "Sorry, we don't have a photo of {product} yet.",
    "rt_switched_to": "Okay, switching to this product:",
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
    "billing_assistant_unavailable": "Thanks for your message! Our store assistant is temporarily unavailable. The seller will contact you shortly.",
    # ── Manual UPI payment verification (deterministic; engine acts, LLM never writes these) ──
    "pay_instruction": "🧾 Order #{order_number}\n{items}\nTotal: {amount}\n\nPay to UPI ID: {upi_id}{payee_line}\n(GPay / PhonePe / Paytm / any UPI app){extra}",
    "pay_steps": "What to do next:\n1️⃣ Pay {amount} to the UPI ID above (or tap the link below).\n2️⃣ Send the payment screenshot here in this chat.\n3️⃣ Our team will verify your payment and confirm your order right here.",
    "pay_send_screenshot": "After payment, please send the payment screenshot here.",
    "pay_proof_received": "Thanks! Our team will verify your payment shortly and update you here.",
    "pay_proof_more": "Received, verification in progress.",
    "pay_auto_attached": "Thanks! I've attached your payment screenshot to order #{order_number} ({amount}) — our team will verify it shortly. Wrong order?",
    "btn_proof_change": "Change order",
    "pay_image_unreadable": "That picture looks blank or too dark to read 📷 Please send your payment screenshot again.",
    "pay_proof_change_none": "I couldn't find a screenshot to move. Please send the payment screenshot again.",
    "pay_awaiting_verification": "Your payment screenshot for order #{order_number} is received ✅ Our team is verifying it and will update you here as soon as it is approved. No need to pay again.",
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
    "ask_color_single": "This comes only in {value}. Would you like {value}? (Yes / No)",
    "ask_size_single": "This comes only in size {value}. Would you like size {value}? (Yes / No)",
    "ask_material_single": "This comes only in {value}. Would you like {value}? (Yes / No)",
    "single_option_declined": "No problem! Send another product name or code, or type 'catalogue' to browse.",
    "ask_variant_mode": "Got it, {color_size}. Same for all {n}, or different for each? (Same/Different)",
    "ask_cart_item_color": "Item {item_num} — which color? Available: {colors}",
    "ask_cart_item_size": "Item {item_num} — which size? Available: {sizes}",
    "ask_cart_item_qty": "How many of {color_size}? ({remaining} left to assign)",
    "ask_cart_breakdown": "And the remaining {remaining} — give me the breakdown, e.g. '30 blue M, 37 green S'",
    "ask_cart_breakdown_confirm": "That's {breakdown}, correct?",
    "cart_breakdown_mismatch": "Total in your breakdown is {sum}, you said {remaining} remaining. Please recheck.",
    "cart_breakdown_invalid_variant": "'{invalid}' isn't a variant we carry. Valid colors: {colors}. Valid sizes: {sizes}.",
    "cart_split_mismatch": "That adds up to {sum} pcs ({breakdown}), but your order is for {qty} pcs. Please send the split again so it totals {qty} — or tell me the new quantity.",
    "cart_split_unavailable": "{variant} isn't available right now. Available sizes: {options}. Please send the split again.",
    "cart_split_stock": "Only {stock} pcs of {variant} left. Please send the split again.",
    "product_decline": "No problem 👍 Want to see other products? {catalogue_url}",
    "slot_invalid_escape": "I couldn't match that to this product's options. Browse the full catalogue: {catalogue_url} — or type 'talk to someone' and our team will help.",
    "pay_upi_link": "📲 Pay in your UPI app: {upi_link}",
    "fallback_browse": "Here's our catalogue: {catalogue_url}\nSend a product name or code and I'll share the details.",
    "fallback_order_active": "Let's continue with your order 🙂",
    "fallback_pay_active": "Your order #{order_number} is waiting for payment. Please pay {amount} to UPI ID {upi_id} and send the payment screenshot here.",
    "fallback_pay_review": "Your order #{order_number} is with our team for payment verification. We'll update you here.",
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
    "summary_upi_only_note": "We currently accept payments via UPI only.",
    "order_summary": (
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
        "2️⃣ Cancel"
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
    "would_you_like_to_order_switch": "Kya aap {name} order karna chahenge? (Haan / Nahi)",
    "followup_default": "Namaste! 🙏 Aapne hamare products mein interest dikhaya tha. Aapka order complete karna baaki hai — kya aapko koi help chahiye? Hum yahan hain! 😊",
    "ostatus_new": "Naya",
    "ostatus_pending_payment": "Payment baaki",
    "ostatus_payment_submitted": "Payment verify ho raha hai",
    "ostatus_confirmed": "Confirm",
    "ostatus_paid": "Payment ho gaya",
    "ostatus_processing": "Process ho raha hai",
    "ostatus_dispatched": "Dispatch ho gaya",
    "ostatus_delivered": "Deliver ho gaya",
    "ostatus_cancelled": "Cancel",
    "variant_slot_rejected": "Maafi, {value} {slot} mein available nahi hai. Available {slot}: {opts}",
    "variant_slot_rejected_noecho": "Maafi, yeh available {slot} options mein nahi hai: {opts}",
    "card_colors": "Available colors: {colors}",
    "card_sizes": "Available sizes: {sizes}",
    "card_intro": "Haan, yeh available hai:",
    "afc_no_match": "'{text}' nahi mila — 1️⃣/2️⃣/3️⃣ reply karein, ya jo product chahiye uska naam/code bhejein.",
    "switching_to": "Theek hai, {product} par switch kar rahe hain. 👍",
    "injection_refusal": "Main sirf {business} ke products aur orders mein madad kar sakta hoon. Main aapki kya madad karoon?",
    "off_topic_boundary": "Main sirf {business} ke orders mein madad kar sakta hoon. Koi product chunein ya shuru se karne ke liye 'cancel' likhein.",
    "attr_size": "size",
    "attr_colour": "color",
    "attr_material": "material",
    "saved_address_reprompt": "Is address par bhejne ke liye *haan* likhein, ya naya address dene ke liye *change* likhein.",
    "team_will_assist": "Hamara team aapki madad karega. 👋",
    "ask_new_address": "Zaroor — naya delivery address kya hai? Ghar/area, city aur pincode bhejein.",
    "rt_clarify_slot": "Maaf kijiye, main samajh nahi paya.",
    "catalogue_browse_more": "🛍️ Aur dekhein: {url}",
    "catalogue_cross_sell": "🛍️ Aapko {name} ({price}) bhi pasand aa sakta hai — {url}",
    "btn_pay_upi": "💳 UPI se pay karein",
    "btn_pay_cod": "🚚 Cash on Delivery",
    "btn_pay_bank": "🏦 Bank Transfer",
    "btn_confirm_pay": "Confirm & Pay",
    "btn_cancel": "Cancel",
    "btn_yes": "Haan",
    "btn_no": "Nahi",
    "btn_change_address": "Badlo",
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
    "switch_offer": "Kya aap {pinned} ki jagah {product} order karna chahenge? (Haan / Nahi)",
    "rt_no_photo": "Maaf kijiye, {product} ki photo abhi available nahi hai.",
    "rt_switched_to": "Theek hai, ab ye product:",
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
    "billing_assistant_unavailable": "Aapke message ke liye dhanyavaad! Hamara store assistant abhi kuch samay ke liye uplabdh nahi hai. Seller jaldi hi aapse sampark karenge.",
    "pay_instruction": "🧾 Order #{order_number}\n{items}\nTotal: {amount}\n\nUPI ID par payment karein: {upi_id}{payee_line}\n(GPay / PhonePe / Paytm / koi bhi UPI app){extra}",
    "pay_steps": "Ab aage kya karna hai:\n1️⃣ Upar diye UPI ID par {amount} pay karein (ya neeche ka link tap karein).\n2️⃣ Payment ka screenshot isi chat mein bhejein.\n3️⃣ Hamari team payment verify karke yahin aapka order confirm kar degi.",
    "pay_send_screenshot": "Payment karne ke baad, kripya payment ka screenshot yahan bhejein.",
    "pay_proof_received": "Shukriya! Hamari team aapka payment jaldi verify karke yahin update degi.",
    "pay_proof_more": "Mil gaya, verification chal raha hai.",
    "pay_auto_attached": "Shukriya! Maine aapka payment screenshot order #{order_number} ({amount}) se jod diya hai — hamari team jaldi verify karegi. Galat order?",
    "btn_proof_change": "Order badlein",
    "pay_image_unreadable": "Yeh photo khali ya bahut andheri lag rahi hai, padhi nahi ja rahi 📷 Kripya payment ka screenshot dobara bhejein.",
    "pay_proof_change_none": "Mujhe badalne ke liye koi screenshot nahi mila. Kripya payment ka screenshot dobara bhejein.",
    "pay_awaiting_verification": "Order #{order_number} ka payment screenshot mil gaya ✅ Hamari team verify kar rahi hai, approve hote hi yahin update milega. Dobara payment karne ki zaroorat nahi.",
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
    "ask_color_single": "Yeh sirf {value} color mein hai. Kya aapko {value} chahiye? (Haan / Nahi)",
    "ask_size_single": "Yeh sirf {value} size mein hai. Kya aapko {value} size chahiye? (Haan / Nahi)",
    "ask_material_single": "Yeh sirf {value} material mein hai. Kya aapko {value} chahiye? (Haan / Nahi)",
    "single_option_declined": "Koi baat nahi! Doosre product ka naam ya code bhejein, ya 'catalogue' likhein.",
    "ask_variant_mode": "Theek hai, {color_size}. Sabhi {n} same chahiye, ya alag alag? (Same/Different)",
    "ask_cart_item_color": "Item {item_num} — kaunsa color? Available: {colors}",
    "ask_cart_item_size": "Item {item_num} — kaunsa size? Available: {sizes}",
    "ask_cart_item_qty": "{color_size} ke kitne? ({remaining} baaki hain)",
    "ask_cart_breakdown": "Aur baaki {remaining} ka breakdown batao — jaise '30 blue M, 37 green S'",
    "ask_cart_breakdown_confirm": "Yeh hai {breakdown}, sahi hai?",
    "cart_breakdown_mismatch": "Aapke breakdown ka total {sum} hai, aapne {remaining} bataya tha. Please recheck karein.",
    "cart_breakdown_invalid_variant": "'{invalid}' hamare paas nahi hai. Valid colors: {colors}. Valid sizes: {sizes}.",
    "cart_split_mismatch": "Ye total {sum} pcs hai ({breakdown}), lekin aapka order {qty} pcs ka hai. Split dobara bhejein jo {qty} bane — ya naya quantity batayein.",
    "cart_split_unavailable": "{variant} abhi available nahi hai. Available sizes: {options}. Split dobara bhejein.",
    "cart_split_stock": "{variant} ke sirf {stock} pcs bache hain. Split dobara bhejein.",
    "product_decline": "Koi baat nahi 👍 Dusre products dekhna chahenge? {catalogue_url}",
    "slot_invalid_escape": "Ye is product ke options se match nahi hua. Poora catalogue dekhein: {catalogue_url} — ya 'talk to someone' likhein, hamari team madad karegi.",
    "pay_upi_link": "📲 UPI app mein pay karein: {upi_link}",
    "fallback_browse": "Ye raha hamara catalogue: {catalogue_url}\nProduct ka naam ya code bhejein, main details bata dunga.",
    "fallback_order_active": "Chaliye aapka order aage badhate hain 🙂",
    "fallback_pay_active": "Aapka order #{order_number} payment ka wait kar raha hai. Kripya {amount} UPI ID {upi_id} par pay karein aur payment screenshot yahin bhejein.",
    "fallback_pay_review": "Aapka order #{order_number} payment verification ke liye hamari team ke paas hai. Yahin update milega.",
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
    "summary_upi_only_note": "Hum sirf UPI se payment accept karte hain.",
    "order_summary": (
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
        "2️⃣ Cancel"
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
    "would_you_like_to_order_switch": "Shu tame {name} order karva mangso cho? (Haa / Na)",
    "followup_default": "Namaste! 🙏 Tame amara products ma interest batavyo hato. Tamaro order complete karvano baki chhe — shu tamne koi madad joiye? Ame ahi chhie! 😊",
    "ostatus_new": "Navo",
    "ostatus_pending_payment": "Payment baki",
    "ostatus_payment_submitted": "Payment verify thai rahyu chhe",
    "ostatus_confirmed": "Confirm",
    "ostatus_paid": "Payment thai gayu",
    "ostatus_processing": "Process thai rahyu chhe",
    "ostatus_dispatched": "Dispatch thai gayu",
    "ostatus_delivered": "Deliver thai gayu",
    "ostatus_cancelled": "Cancel",
    "variant_slot_rejected": "Maaf karo, {slot} ma {value} available nathi. Available {slot}: {opts}",
    "variant_slot_rejected_noecho": "Maaf karo, aa available {slot} options ma nathi: {opts}",
    "card_colors": "Available colors: {colors}",
    "card_sizes": "Available sizes: {sizes}",
    "card_intro": "Ha, aa available chhe:",
    "afc_no_match": "'{text}' malyu nahi — 1️⃣/2️⃣/3️⃣ lakho, athva je product joiye teno naam/code moklo.",
    "switching_to": "Saru, {product} par switch kari rahya chhie. 👍",
    "injection_refusal": "Hu fakt {business} na products ane orders ma madad kari shaku chhu. Hu tamari shu madad karu?",
    "off_topic_boundary": "Hu fakt {business} na orders ma madad kari shaku chhu. Koi product pasand karo athva fari sharu karva 'cancel' lakho.",
    "attr_size": "size",
    "attr_colour": "color",
    "attr_material": "material",
    "saved_address_reprompt": "Aa address par moklva *ha* lakho, athva navu address aapva *change* lakho.",
    "team_will_assist": "Amari team tamne madad karse. 👋",
    "ask_new_address": "Jarur — navu delivery address shu chhe? Ghar/vistar, shahar ane pincode moklo.",
    "rt_clarify_slot": "Maaf karjo, mane samjayu nahi.",
    "catalogue_browse_more": "🛍️ Vadhu juo: {url}",
    "catalogue_cross_sell": "🛍️ Tamne {name} ({price}) pan gamshe — {url}",
    "btn_pay_upi": "💳 UPI thi pay karo",
    "btn_pay_cod": "🚚 Cash on Delivery",
    "btn_pay_bank": "🏦 Bank Transfer",
    "btn_confirm_pay": "Confirm & Pay",
    "btn_cancel": "Cancel",
    "btn_yes": "Ha",
    "btn_no": "Na",
    "btn_change_address": "Badlo",
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
    "switch_offer": "Shu tame {pinned} ni jagya e {product} order karva mangso? (Haa / Na)",
    "rt_no_photo": "Maaf karjo, {product} no photo hamna available nathi.",
    "rt_switched_to": "Barabar, have aa product:",
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
    "billing_assistant_unavailable": "Tamara message badal aabhar! Amaro store assistant haal thodi vaar mate uplabdh nathi. Seller tamne jaldi sampark karshe.",
    "pay_instruction": "🧾 Order #{order_number}\n{items}\nKul: {amount}\n\nUPI ID par payment karo: {upi_id}{payee_line}\n(GPay / PhonePe / Paytm / koi pan UPI app){extra}",
    "pay_steps": "Have shu karvanu chhe:\n1️⃣ Upar aapela UPI ID par {amount} pay karo (athva niche ni link tap karo).\n2️⃣ Payment no screenshot aa j chat ma moklo.\n3️⃣ Amari team payment verify kari ahi j tamaro order confirm karse.",
    "pay_send_screenshot": "Payment karya pachhi, krupa kari ne payment no screenshot ahi moklo.",
    "pay_proof_received": "Aabhar! Amari team tamaru payment jaldi verify kari ne ahi j update api dese.",
    "pay_proof_more": "Mali gayu, verification chaalu chhe.",
    "pay_auto_attached": "Aabhar! Me tamaro payment screenshot order #{order_number} ({amount}) sathe jodi didho chhe — amari team jaldi verify karse. Khotu order?",
    "btn_proof_change": "Order badlo",
    "pay_image_unreadable": "Aa photo khali athva khub andhari lage chhe, vanchi shakati nathi 📷 Krupa kari ne payment no screenshot farithi moklo.",
    "pay_proof_change_none": "Mane badalva mate koi screenshot na malyo. Krupa kari ne payment no screenshot farithi moklo.",
    "pay_awaiting_verification": "Order #{order_number} no payment screenshot mali gayo ✅ Amari team verify kari rahi chhe, approve thata j ahi update malshe. Pharithi payment karvani jarur nathi.",
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
    "ask_color_single": "Aa fakt {value} color ma chhe. Shu tamne {value} joiye? (Haa / Naa)",
    "ask_size_single": "Aa fakt {value} size ma chhe. Shu tamne {value} size joiye? (Haa / Naa)",
    "ask_material_single": "Aa fakt {value} material ma chhe. Shu tamne {value} joiye? (Haa / Naa)",
    "single_option_declined": "Koi vaandho nahi! Biji product nu naam ke code moklo, ke 'catalogue' lakho.",
    "ask_variant_mode": "Bhalu, {color_size}. Badha {n} same joiye, ke alag alag? (Same/Different)",
    "ask_cart_item_color": "Item {item_num} — kayo color? Available: {colors}",
    "ask_cart_item_size": "Item {item_num} — kayo size? Available: {sizes}",
    "ask_cart_item_qty": "{color_size} na ketla? ({remaining} baaki chhe)",
    "ask_cart_breakdown": "Ane baaki {remaining} nu breakdown aapo — jem ke '30 blue M, 37 green S'",
    "ask_cart_breakdown_confirm": "Aa che {breakdown}, barabar chhe?",
    "cart_breakdown_mismatch": "Tamara breakdown no total {sum} chhe, tame {remaining} kahyu hatu. Please recheck karo.",
    "cart_breakdown_invalid_variant": "'{invalid}' amara paase nathi. Valid colors: {colors}. Valid sizes: {sizes}.",
    "cart_split_mismatch": "Aa total {sum} pcs chhe ({breakdown}), pan tamaro order {qty} pcs no chhe. Split pharithi moklo jethi {qty} thay — athva navi quantity kaho.",
    "cart_split_unavailable": "{variant} atyare available nathi. Available sizes: {options}. Split pharithi moklo.",
    "cart_split_stock": "{variant} na fakt {stock} pcs baki chhe. Split pharithi moklo.",
    "product_decline": "Koi vandho nahi 👍 Biji products jova chho? {catalogue_url}",
    "slot_invalid_escape": "Aa aa product na options sathe match na thayu. Puru catalogue juo: {catalogue_url} — athva 'talk to someone' lakho, amari team madad karse.",
    "pay_upi_link": "📲 UPI app ma pay karo: {upi_link}",
    "fallback_browse": "Aa chhe amaru catalogue: {catalogue_url}\nProduct nu naam ke code moklo, hu details kahish.",
    "fallback_order_active": "Chalo tamaro order aagal vadharie 🙂",
    "fallback_pay_active": "Tamaro order #{order_number} payment ni rah jove chhe. Krupa kari {amount} UPI ID {upi_id} par pay karo ane payment screenshot ahi moklo.",
    "fallback_pay_review": "Tamaro order #{order_number} payment verification mate amari team pase chhe. Ahi update malshe.",
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
    "summary_upi_only_note": "Hum sirf UPI thi payment accept karie chhe.",
    "order_summary": (
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
        "2️⃣ Cancel"
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
    "would_you_like_to_order_switch": "क्या आप {name} ऑर्डर करना चाहेंगे? (हाँ / नहीं)",
    "followup_default": "नमस्ते! 🙏 आपने हमारे प्रोडक्ट्स में रुचि दिखाई थी। आपका ऑर्डर पूरा करना बाकी है — क्या आपको कोई मदद चाहिए? हम यहाँ हैं! 😊",
    "ostatus_new": "नया",
    "ostatus_pending_payment": "भुगतान बाकी",
    "ostatus_payment_submitted": "भुगतान की जाँच हो रही है",
    "ostatus_confirmed": "कन्फर्म",
    "ostatus_paid": "भुगतान हो गया",
    "ostatus_processing": "प्रोसेस हो रहा है",
    "ostatus_dispatched": "भेज दिया गया",
    "ostatus_delivered": "डिलीवर हो गया",
    "ostatus_cancelled": "रद्द",
    "variant_slot_rejected": "माफी, {slot} में {value} उपलब्ध नहीं है। उपलब्ध {slot}: {opts}",
    "variant_slot_rejected_noecho": "माफी, यह उपलब्ध {slot} विकल्पों में नहीं है: {opts}",
    "card_colors": "उपलब्ध रंग: {colors}",
    "card_sizes": "उपलब्ध साइज़: {sizes}",
    "card_intro": "जी हाँ, यह उपलब्ध है:",
    "afc_no_match": "'{text}' नहीं मिला — 1️⃣/2️⃣/3️⃣ लिखें, या जो प्रोडक्ट चाहिए उसका नाम/कोड भेजें।",
    "switching_to": "ठीक है, {product} पर स्विच कर रहे हैं। 👍",
    "injection_refusal": "मैं सिर्फ़ {business} के प्रोडक्ट्स और ऑर्डर में मदद कर सकता हूँ। मैं आपकी क्या मदद करूँ?",
    "off_topic_boundary": "मैं सिर्फ़ {business} के ऑर्डर में मदद कर सकता हूँ। कोई प्रोडक्ट चुनें या दोबारा शुरू करने के लिए 'रद्द करें' लिखें।",
    "attr_size": "साइज़",
    "attr_colour": "रंग",
    "attr_material": "मटेरियल",
    "saved_address_reprompt": "इस पते पर भेजने के लिए *हाँ* लिखें, या नया पता देने के लिए *बदलें* लिखें।",
    "team_will_assist": "हमारी टीम आपकी मदद करेगी। 👋",
    "ask_new_address": "ज़रूर — नया डिलीवरी पता क्या है? घर/इलाका, शहर और पिनकोड भेजें।",
    "rt_clarify_slot": "क्षमा करें, मैं समझ नहीं पाया।",
    "catalogue_browse_more": "🛍️ और देखें: {url}",
    "catalogue_cross_sell": "🛍️ आपको {name} ({price}) भी पसंद आ सकता है — {url}",
    "btn_pay_upi": "💳 UPI से भुगतान",
    "btn_pay_cod": "🚚 कैश ऑन डिलीवरी",
    "btn_pay_bank": "🏦 बैंक ट्रांसफर",
    "btn_confirm_pay": "कन्फर्म और भुगतान",
    "btn_cancel": "रद्द करें",
    "btn_yes": "हाँ",
    "btn_no": "नहीं",
    "btn_change_address": "बदलें",
    "rt_order_open_note": "{product} का आपका ऑर्डर अभी खुला है — जारी रखने के लिए आख़िरी सवाल का जवाब दें, या रोकने के लिए 'रद्द करें' लिखें।",
    "rt_card": "{name} [{sku}] — {price}।{options_line}",
    # ── ROUTER_V2 (LLM intent router) replies: deterministic, engine-written ──
    "rt_clarify_two": "क्या आपका मतलब {a} या {b} है? कृपया प्रोडक्ट का नाम या कोड भेजें।",
    "rt_clarify_open": "क्षमा करें, मैं समझ नहीं पाया। आप क्या ढूँढ रहे हैं? प्रोडक्ट का नाम या कोड लिखें, या 'ऑर्डर स्टेटस'।",
    "rt_not_found": "क्षमा करें, अभी इसके लिए सटीक मैच नहीं मिला।",
    "rt_alternatives_header": "ये सबसे मिलते-जुलते विकल्प हैं:",
    "rt_search_header": "हमारे पास ये है 👇",
    "rt_search_footer": "विवरण देखने के लिए नंबर या प्रोडक्ट कोड भेजें।",
    "rt_browse_more": "पूरा कलेक्शन यहाँ देखें: {catalogue_url}",
    "rt_handoff": "हमारी टीम यहीं जल्द ही जवाब देगी।",
    "rt_handoff_offer": "इसका पक्का जवाब मेरे पास नहीं है। अगर आप चाहते हैं कि हमारी टीम मदद करे, तो 'किसी से बात करनी है' लिखें।",
    "rt_general_fallback": "ज़रूर मदद करूँगा! प्रोडक्ट का नाम या कोड लिखें, या 'ऑर्डर स्टेटस'।",
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
    "switch_offer": "क्या आप {pinned} की जगह {product} ऑर्डर करना चाहेंगे? (हाँ / नहीं)",
    "rt_no_photo": "क्षमा करें, {product} की फ़ोटो अभी उपलब्ध नहीं है।",
    "rt_switched_to": "ठीक है, अब ये प्रोडक्ट:",
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
    "llm_unavailable_rephrase": "क्षमा करें, क्या आप दोबारा लिख सकते हैं? या प्रोडक्ट कोड या 'ऑर्डर स्टेटस' टाइप करें।",
    "billing_assistant_unavailable": "आपके संदेश के लिए धन्यवाद! हमारा स्टोर असिस्टेंट अभी कुछ समय के लिए उपलब्ध नहीं है। विक्रेता जल्द ही आपसे संपर्क करेंगे।",
    "pay_instruction": "🧾 ऑर्डर #{order_number}\n{items}\nकुल: {amount}\n\nUPI ID पर भुगतान करें: {upi_id}{payee_line}\n(GPay / PhonePe / Paytm / कोई भी UPI ऐप){extra}",
    "pay_steps": "आगे क्या करना है:\n1️⃣ ऊपर दिए UPI ID पर {amount} पे करें (या नीचे का लिंक टैप करें)।\n2️⃣ पेमेंट का स्क्रीनशॉट इसी चैट में भेजें।\n3️⃣ हमारी टीम पेमेंट वेरीफाई करके यहीं आपका ऑर्डर कन्फर्म करेगी।",
    "pay_send_screenshot": "भुगतान के बाद, कृपया पेमेंट का स्क्रीनशॉट यहाँ भेजें।",
    "pay_proof_received": "धन्यवाद! हमारी टीम जल्द ही आपका भुगतान वेरिफ़ाई करके यहीं अपडेट देगी।",
    "pay_proof_more": "मिल गया, वेरिफ़िकेशन जारी है।",
    "pay_auto_attached": "धन्यवाद! मैंने आपका पेमेंट स्क्रीनशॉट ऑर्डर #{order_number} ({amount}) से जोड़ दिया है — हमारी टीम जल्द वेरिफ़ाई करेगी। गलत ऑर्डर?",
    "btn_proof_change": "ऑर्डर बदलें",
    "pay_image_unreadable": "यह फ़ोटो खाली या बहुत अंधेरी लग रही है, पढ़ी नहीं जा रही 📷 कृपया पेमेंट का स्क्रीनशॉट दोबारा भेजें।",
    "pay_proof_change_none": "मुझे बदलने के लिए कोई स्क्रीनशॉट नहीं मिला। कृपया पेमेंट का स्क्रीनशॉट दोबारा भेजें।",
    "pay_awaiting_verification": "ऑर्डर #{order_number} का पेमेंट स्क्रीनशॉट मिल गया ✅ हमारी टीम वेरिफ़ाई कर रही है, अप्रूव होते ही यहीं अपडेट मिलेगा। दोबारा भुगतान करने की ज़रूरत नहीं।",
    "pay_ask_screenshot": "कृपया पेमेंट का स्क्रीनशॉट यहाँ भेजें ताकि हम वेरिफ़ाई कर सकें। 📸",
    "pay_confirmed": "पेमेंट कन्फर्म हो गया ✅ आपका ऑर्डर #{order_number} कन्फर्म है।",
    "pay_rejected": "हम आपका पेमेंट कन्फर्म नहीं कर पाए{reason_part}। कृपया जाँचकर स्क्रीनशॉट दोबारा भेजें।",
    "pay_cancelled": "आपका ऑर्डर #{order_number} रद्द कर दिया गया है।{reason_part}",
    "order_status_summary": "🧾 ऑर्डर #{order_number}\n{items}\nकुल: {total}\nस्टेटस: {status}",
    "order_status_pay_reminder": "⏳ पेमेंट बाकी है: कृपया {amount} UPI ID {upi_id} पर भेजें और पेमेंट का स्क्रीनशॉट यहाँ भेजें ताकि हम ऑर्डर कन्फर्म कर सकें।",
    "order_status_id_not_found": "इस नंबर पर ऑर्डर #{order_number} नहीं मिला। कृपया ऑर्डर ID जाँचें, या अपना लेटेस्ट ऑर्डर देखने के लिए 'ऑर्डर स्टेटस' भेजें।",
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
    "ask_color_single": "यह सिर्फ़ {value} कलर में है। क्या आपको {value} चाहिए? (हाँ / नहीं)",
    "ask_size_single": "यह सिर्फ़ {value} साइज़ में है। क्या आपको {value} साइज़ चाहिए? (हाँ / नहीं)",
    "ask_material_single": "यह सिर्फ़ {value} मटेरियल में है। क्या आपको {value} चाहिए? (हाँ / नहीं)",
    "single_option_declined": "कोई बात नहीं! दूसरे प्रोडक्ट का नाम या कोड भेजें, या 'catalogue' लिखें।",
    "ask_variant_mode": "ठीक है, {color_size}। सभी {n} एक जैसे चाहिए, या अलग-अलग? (एक जैसे/अलग-अलग)",
    "ask_cart_item_color": "आइटम {item_num} — कौनसा कलर? उपलब्ध: {colors}",
    "ask_cart_item_size": "आइटम {item_num} — कौनसा साइज़? उपलब्ध: {sizes}",
    "ask_cart_item_qty": "{color_size} के कितने? ({remaining} बाकी हैं)",
    "ask_cart_breakdown": "और बाकी {remaining} का ब्रेकडाउन बताओ — जैसे '30 नीला M, 37 हरा S'",
    "ask_cart_breakdown_confirm": "यह है {breakdown}, सही है?",
    "cart_breakdown_mismatch": "आपके ब्रेकडाउन का टोटल {sum} है, आपने {remaining} बताया था। कृपया दोबारा चेक करें।",
    "cart_breakdown_invalid_variant": "'{invalid}' हमारे पास नहीं है। मान्य कलर: {colors}। मान्य साइज़: {sizes}।",
    "out_of_stock_combo": "{combo} सोल्ड आउट है। कोई और {last_attr}?",
    "cross_sell": "आपने {name} (₹{price}) भी देखा था। इसे भी ऐड करें? (हाँ/नहीं)",
    "ask_payment_method": "आप UPI से देंगे{cod_option}?",
    "confirm_payment_upi": "पेमेंट: UPI ✅ कन्फर्म करें? (हाँ)",
    "ask_payment_upi_cod": "पेमेंट? UPI / COD",
    "confirm_saved_address": "{address} पर डिलीवर करें? (हाँ/बदलें)",
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
    "summary_upi_only_note": "हम सिर्फ UPI से पेमेंट स्वीकार करते हैं।",
    "order_summary": (
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
        "✅ ऑर्डर सारांश\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 डिलीवरी {delivery_time} में\n"
        "━━━━━━━━━━━━━━━\n"
        "रिप्लाई करें:\n"
        "1️⃣ कन्फर्म करें और भुगतान करें\n"
        "2️⃣ {cs_name} ({cs_price}) भी ऐड करें\n"
        "3️⃣ रद्द करें"
    ),
    "order_summary_variant_crosssell": (
        "✅ ऑर्डर सारांश\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} {variant} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 डिलीवरी {delivery_time} में\n"
        "━━━━━━━━━━━━━━━\n"
        "रिप्लाई करें:\n"
        "1️⃣ कन्फर्म करें और भुगतान करें\n"
        "2️⃣ {cs_name} ({cs_price}) भी ऐड करें\n"
        "3️⃣ रद्द करें"
    ),
    "cart_order_summary": (
        "✅ ऑर्डर सारांश\n"
        "━━━━━━━━━━━━━━━\n"
        "{items_block}\n"
        "कुल: {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 डिलीवरी {delivery_time} में\n"
        "━━━━━━━━━━━━━━━\n"
        "रिप्लाई करें:\n"
        "1️⃣ कन्फर्म करें और भुगतान करें\n"
        "2️⃣ रद्द करें"
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
        "कुल: {total}\n"
        "🚚 डिलीवरी {delivery_time} में\n\n"
        "शॉपिंग करने के लिए शुक्रिया! 😊\n"
        "{catalogue_line}"
    ),
    "order_confirmed_paid_cart": (
        "🎉 आपका ऑर्डर प्लेस हो गया! ✅\n"
        "{items_block}\n"
        "कुल: {total}\n"
        "🚚 डिलीवरी {delivery_time} में\n\n"
        "शॉपिंग करने के लिए शुक्रिया! 😊\n"
        "{catalogue_line}"
    ),
    "already_confirmed": "आपका ऑर्डर कन्फर्म हो चुका है ✅ कोई और मदद चाहिए?",
    "interrupt_switch": (
        "आपका एक ऑर्डर चल रहा है: {current_qty}x {current_product}{current_total_text}।\n"
        "{new_product} [{new_sku}] ₹{new_price} में है।\n"
        "'जारी रखें' लिखें अपना मौजूदा ऑर्डर पूरा करने के लिए, "
        "या 'नया ऑर्डर' लिखें {new_product} का नया ऑर्डर शुरू करने के लिए "
        "(मौजूदा प्रगति डिलीट हो जाएगी)।"
    ),
    "switch_reask": (
        "{current_product} का ऑर्डर चल रहा है।\n"
        "'जारी रखें' लिखें पूरा करने के लिए, या 'नया ऑर्डर' लिखें नया ऑर्डर करने के लिए।"
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
    "pinned_availability_guard": "{name} [{sku}] — ₹{price} में उपलब्ध है।{color_part}{size_part} ऑर्डर के लिए हाँ, ब्राउज़िंग जारी रखने के लिए नहीं लिखें। क्या आप ऑर्डर करना चाहेंगे?",
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
    "would_you_like_to_order_switch": "શું તમે {name} ઓર્ડર કરવા માંગો છો? (હા / ના)",
    "followup_default": "નમસ્તે! 🙏 તમે અમારા પ્રોડક્ટ્સમાં રસ બતાવ્યો હતો. તમારો ઓર્ડર પૂરો કરવાનો બાકી છે — શું તમને કોઈ મદદ જોઈએ? અમે અહીં છીએ! 😊",
    "ostatus_new": "નવો",
    "ostatus_pending_payment": "ચુકવણી બાકી",
    "ostatus_payment_submitted": "ચુકવણીની ચકાસણી ચાલુ છે",
    "ostatus_confirmed": "કન્ફર્મ",
    "ostatus_paid": "ચુકવણી થઈ ગઈ",
    "ostatus_processing": "પ્રોસેસ થઈ રહ્યો છે",
    "ostatus_dispatched": "રવાના કર્યો",
    "ostatus_delivered": "ડિલિવર થયો",
    "ostatus_cancelled": "રદ",
    "variant_slot_rejected": "માફ કરશો, {slot} માં {value} ઉપલબ્ધ નથી. ઉપલબ્ધ {slot}: {opts}",
    "variant_slot_rejected_noecho": "માફ કરશો, આ ઉપલબ્ધ {slot} વિકલ્પોમાં નથી: {opts}",
    "card_colors": "ઉપલબ્ધ રંગ: {colors}",
    "card_sizes": "ઉપલબ્ધ સાઈઝ: {sizes}",
    "card_intro": "હા, આ ઉપલબ્ધ છે:",
    "afc_no_match": "'{text}' મળ્યું નથી — 1️⃣/2️⃣/3️⃣ લખો, અથવા જે પ્રોડક્ટ જોઈએ તેનું નામ/કોડ મોકલો.",
    "switching_to": "સારું, {product} પર સ્વિચ કરી રહ્યા છીએ. 👍",
    "injection_refusal": "હું ફક્ત {business} ના પ્રોડક્ટ્સ અને ઓર્ડરમાં મદદ કરી શકું છું. હું તમારી શું મદદ કરું?",
    "off_topic_boundary": "હું ફક્ત {business} ના ઓર્ડરમાં મદદ કરી શકું છું. કોઈ પ્રોડક્ટ પસંદ કરો અથવા ફરી શરૂ કરવા 'રદ કરો' લખો.",
    "attr_size": "સાઈઝ",
    "attr_colour": "રંગ",
    "attr_material": "મટીરીયલ",
    "saved_address_reprompt": "આ સરનામે મોકલવા *હા* લખો, અથવા નવું સરનામું આપવા *બદલો* લખો.",
    "team_will_assist": "અમારી ટીમ તમને મદદ કરશે. 👋",
    "ask_new_address": "જરૂર — નવું ડિલિવરી સરનામું શું છે? ઘર/વિસ્તાર, શહેર અને પિનકોડ મોકલો.",
    "rt_clarify_slot": "માફ કરજો, મને સમજાયું નહીં.",
    "catalogue_browse_more": "🛍️ વધુ જુઓ: {url}",
    "catalogue_cross_sell": "🛍️ તમને {name} ({price}) પણ ગમશે — {url}",
    "btn_pay_upi": "💳 UPI થી ચૂકવો",
    "btn_pay_cod": "🚚 કેશ ઓન ડિલિવરી",
    "btn_pay_bank": "🏦 બેંક ટ્રાન્સફર",
    "btn_confirm_pay": "કન્ફર્મ અને ચુકવણી",
    "btn_cancel": "રદ કરો",
    "btn_yes": "હા",
    "btn_no": "ના",
    "btn_change_address": "બદલો",
    "rt_order_open_note": "{product} નો તમારો ઓર્ડર હજુ ચાલુ છે — ચાલુ રાખવા છેલ્લા સવાલનો જવાબ આપો, અથવા રોકવા 'રદ કરો' લખો.",
    "rt_card": "{name} [{sku}] — {price}.{options_line}",
    # ── ROUTER_V2 (LLM intent router) replies: deterministic, engine-written ──
    "rt_clarify_two": "શું તમારો મતલબ {a} કે {b} છે? કૃપા કરીને પ્રોડક્ટનું નામ અથવા કોડ મોકલો.",
    "rt_clarify_open": "માફ કરજો, મને સમજાયું નહીં. તમે શું શોધી રહ્યા છો? પ્રોડક્ટનું નામ અથવા કોડ લખો, અથવા 'ઓર્ડર સ્ટેટસ'.",
    "rt_not_found": "માફ કરજો, હમણાં આ માટે બરાબર મેળ મળ્યો નથી.",
    "rt_alternatives_header": "આ સૌથી મળતા આવતા વિકલ્પો છે:",
    "rt_search_header": "અમારી પાસે આ છે 👇",
    "rt_search_footer": "વિગતો જોવા નંબર અથવા પ્રોડક્ટ કોડ મોકલો.",
    "rt_browse_more": "પૂરું કલેક્શન અહીં જુઓ: {catalogue_url}",
    "rt_handoff": "અમારી ટીમ અહીં જ થોડી વારમાં જવાબ આપશે.",
    "rt_handoff_offer": "આ વિશે મારી પાસે પાક્કો જવાબ નથી. જો તમે ઇચ્છો કે અમારી ટીમ મદદ કરે, તો 'કોઈ સાથે વાત કરવી છે' લખો.",
    "rt_general_fallback": "જરૂર મદદ કરીશ! પ્રોડક્ટનું નામ અથવા કોડ લખો, અથવા 'ઓર્ડર સ્ટેટસ'.",
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
    "switch_offer": "શું તમે {pinned} ની જગ્યાએ {product} ઓર્ડર કરવા માંગો છો? (હા / ના)",
    "rt_no_photo": "માફ કરજો, {product} નો ફોટો હમણાં ઉપલબ્ધ નથી.",
    "rt_switched_to": "ઠીક છે, હવે આ પ્રોડક્ટ:",
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
    "llm_unavailable_rephrase": "માફ કરજો, શું તમે ફરીથી લખી શકો? અથવા પ્રોડક્ટ કોડ અથવા 'ઓર્ડર સ્ટેટસ' લખો.",
    "billing_assistant_unavailable": "તમારા સંદેશ બદલ આભાર! અમારો સ્ટોર આસિસ્ટન્ટ હાલ થોડા સમય માટે ઉપલબ્ધ નથી. વિક્રેતા ટૂંક સમયમાં તમારો સંપર્ક કરશે.",
    "pay_instruction": "🧾 ઓર્ડર #{order_number}\n{items}\nકુલ: {amount}\n\nUPI ID પર ચુકવણી કરો: {upi_id}{payee_line}\n(GPay / PhonePe / Paytm / કોઈપણ UPI એપ){extra}",
    "pay_steps": "હવે શું કરવાનું છે:\n1️⃣ ઉપર આપેલા UPI ID પર {amount} પે કરો (અથવા નીચેની લિંક ટેપ કરો).\n2️⃣ પેમેન્ટનો સ્ક્રીનશોટ આ જ ચેટમાં મોકલો.\n3️⃣ અમારી ટીમ પેમેન્ટ વેરિફાય કરીને અહીં જ તમારો ઓર્ડર કન્ફર્મ કરશે.",
    "pay_send_screenshot": "ચુકવણી કર્યા પછી, કૃપા કરીને પેમેન્ટનો સ્ક્રીનશૉટ અહીં મોકલો.",
    "pay_proof_received": "આભાર! અમારી ટીમ તમારું પેમેન્ટ જલ્દી વેરિફાય કરીને અહીં જ અપડેટ આપશે.",
    "pay_proof_more": "મળી ગયું, વેરિફિકેશન ચાલુ છે.",
    "pay_auto_attached": "આભાર! મેં તમારો પેમેન્ટ સ્ક્રીનશોટ ઓર્ડર #{order_number} ({amount}) સાથે જોડી દીધો છે — અમારી ટીમ જલ્દી વેરિફાય કરશે. ખોટો ઓર્ડર?",
    "btn_proof_change": "ઓર્ડર બદલો",
    "pay_image_unreadable": "આ ફોટો ખાલી અથવા બહુ અંધારો લાગે છે, વાંચી શકાતો નથી 📷 કૃપા કરીને પેમેન્ટનો સ્ક્રીનશોટ ફરીથી મોકલો.",
    "pay_proof_change_none": "મને બદલવા માટે કોઈ સ્ક્રીનશોટ મળ્યો નહીં. કૃપા કરીને પેમેન્ટનો સ્ક્રીનશોટ ફરીથી મોકલો.",
    "pay_awaiting_verification": "ઓર્ડર #{order_number} નો પેમેન્ટ સ્ક્રીનશૉટ મળી ગયો ✅ અમારી ટીમ વેરિફાય કરી રહી છે, અપ્રૂવ થતાં જ અહીં અપડેટ મળશે. ફરીથી ચુકવણી કરવાની જરૂર નથી.",
    "pay_ask_screenshot": "કૃપા કરીને પેમેન્ટનો સ્ક્રીનશૉટ અહીં મોકલો જેથી અમે વેરિફાય કરી શકીએ. 📸",
    "pay_confirmed": "પેમેન્ટ કન્ફર્મ થઈ ગયું ✅ તમારો ઓર્ડર #{order_number} કન્ફર્મ છે.",
    "pay_rejected": "અમે તમારું પેમેન્ટ કન્ફર્મ કરી શક્યા નથી{reason_part}. કૃપા કરીને ચકાસીને સ્ક્રીનશૉટ ફરીથી મોકલો.",
    "pay_cancelled": "તમારો ઓર્ડર #{order_number} રદ કરવામાં આવ્યો છે.{reason_part}",
    "order_status_summary": "🧾 ઓર્ડર #{order_number}\n{items}\nકુલ: {total}\nસ્ટેટસ: {status}",
    "order_status_pay_reminder": "⏳ પેમેન્ટ બાકી છે: કૃપા કરીને {amount} UPI ID {upi_id} પર મોકલો અને પેમેન્ટનો સ્ક્રીનશૉટ અહીં મોકલો જેથી અમે ઓર્ડર કન્ફર્મ કરી શકીએ.",
    "order_status_id_not_found": "આ નંબર પર ઓર્ડર #{order_number} મળ્યો નથી. કૃપા કરીને ઓર્ડર ID તપાસો, અથવા તમારો લેટેસ્ટ ઓર્ડર જોવા 'ઓર્ડર સ્ટેટસ' મોકલો.",
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
    "ask_color_single": "આ ફક્ત {value} કલરમાં છે. શું તમને {value} જોઈએ? (હા / ના)",
    "ask_size_single": "આ ફક્ત {value} સાઈઝમાં છે. શું તમને {value} સાઈઝ જોઈએ? (હા / ના)",
    "ask_material_single": "આ ફક્ત {value} મટીરીયલમાં છે. શું તમને {value} જોઈએ? (હા / ના)",
    "single_option_declined": "કોઈ વાંધો નથી! બીજા પ્રોડક્ટનું નામ કે કોડ મોકલો, અથવા 'catalogue' લખો.",
    "ask_variant_mode": "ભલું, {color_size}. બધા {n} સરખા જોઈએ, કે અલગ અલગ? (સરખા/અલગ)",
    "ask_cart_item_color": "આઇટમ {item_num} — કયો કલર? ઉપલબ્ધ: {colors}",
    "ask_cart_item_size": "આઇટમ {item_num} — કઈ સાઈઝ? ઉપલબ્ધ: {sizes}",
    "ask_cart_item_qty": "{color_size} ના કેટલા? ({remaining} બાકી છે)",
    "ask_cart_breakdown": "અને બાકી {remaining} નું બ્રેકડાઉન આપો — જેમ કે '30 વાદળી M, 37 લીલો S'",
    "ask_cart_breakdown_confirm": "આ છે {breakdown}, બરાબર છે?",
    "cart_breakdown_mismatch": "તમારા બ્રેકડાઉનનો ટોટલ {sum} છે, તમે {remaining} કહ્યું હતું. કૃપા કરી ફરી ચેક કરો.",
    "cart_breakdown_invalid_variant": "'{invalid}' અમારી પાસે નથી. માન્ય કલર: {colors}. માન્ય સાઈઝ: {sizes}.",
    "out_of_stock_combo": "{combo} સોલ્ડ આઉટ છે. બીજો {last_attr}?",
    "cross_sell": "તમે {name} (₹{price}) પણ જોયું હતું. તેને પણ ઉમેરવું છે? (હા/ના)",
    "ask_payment_method": "કેમ ભરવું છે — UPI{cod_option}?",
    "confirm_payment_upi": "પેમેન્ટ: UPI ✅ કન્ફર્મ કરો? (હા)",
    "ask_payment_upi_cod": "પેમેન્ટ? UPI / COD",
    "confirm_saved_address": "{address} પર ડિલિવર કરવું? (હા/બદલો)",
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
    "summary_upi_only_note": "અમે ફક્ત UPI થી પેમેન્ટ સ્વીકારીએ છીએ.",
    "order_summary": (
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
        "✅ ઓર્ડર સારાંશ\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 ડિલિવરી {delivery_time} માં\n"
        "━━━━━━━━━━━━━━━\n"
        "રિપ્લાય કરો:\n"
        "1️⃣ કન્ફર્મ કરો અને ચુકવણી કરો\n"
        "2️⃣ {cs_name} ({cs_price}) પણ ઉમેરો\n"
        "3️⃣ રદ કરો"
    ),
    "order_summary_variant_crosssell": (
        "✅ ઓર્ડર સારાંશ\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 {product} {variant} × {qty} = {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 ડિલિવરી {delivery_time} માં\n"
        "━━━━━━━━━━━━━━━\n"
        "રિપ્લાય કરો:\n"
        "1️⃣ કન્ફર્મ કરો અને ચુકવણી કરો\n"
        "2️⃣ {cs_name} ({cs_price}) પણ ઉમેરો\n"
        "3️⃣ રદ કરો"
    ),
    "cart_order_summary": (
        "✅ ઓર્ડર સારાંશ\n"
        "━━━━━━━━━━━━━━━\n"
        "{items_block}\n"
        "કુલ: {total}\n"
        "👤 {name}\n"
        "📍 {address}\n"
        "💳 {payment}\n"
        "🚚 ડિલિવરી {delivery_time} માં\n"
        "━━━━━━━━━━━━━━━\n"
        "રિપ્લાય કરો:\n"
        "1️⃣ કન્ફર્મ કરો અને ચુકવણી કરો\n"
        "2️⃣ રદ કરો"
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
        "કુલ: {total}\n"
        "🚚 ડિલિવરી {delivery_time} માં\n\n"
        "શોપિંગ કરવા બદલ આભાર! 😊\n"
        "{catalogue_line}"
    ),
    "order_confirmed_paid_cart": (
        "🎉 તમારો ઓર્ડર પ્લેસ થઈ ગયો! ✅\n"
        "{items_block}\n"
        "કુલ: {total}\n"
        "🚚 ડિલિવરી {delivery_time} માં\n\n"
        "શોપિંગ કરવા બદલ આભાર! 😊\n"
        "{catalogue_line}"
    ),
    "already_confirmed": "તમારો ઓર્ડર કન્ફર્મ થઈ ગયો છે ✅ કોઈ મદદ જોઈએ?",
    "interrupt_switch": (
        "તમારો એક ઓર્ડર ચાલી રહ્યો છે: {current_qty}x {current_product}{current_total_text}.\n"
        "{new_product} [{new_sku}] ₹{new_price} માં છે.\n"
        "'ચાલુ રાખો' લખો તમારો હાલનો ઓર્ડર પૂરો કરવા, "
        "અથવા 'નવો ઓર્ડર' લખો {new_product} માટે નવો ઓર્ડર શરૂ કરવા "
        "(હાલની પ્રગતિ ડિલીટ થશે)."
    ),
    "switch_reask": (
        "{current_product} નો ઓર્ડર ચાલી રહ્યો છે.\n"
        "'ચાલુ રાખો' લખો પૂરો કરવા, અથવા 'નવો ઓર્ડર' લખો નવો ઓર્ડર કરવા."
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
    "pinned_availability_guard": "{name} [{sku}] — ₹{price} માં ઉપલબ્ધ છે.{color_part}{size_part} ઓર્ડર માટે હા, બ્રાઉઝિંગ ચાલુ રાખવા ના લખો. શું તમે ઓર્ડર કરવા માંગો છો?",
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

# ── Side questions / social messages (app/services/side_questions.py) ──────────────────────────────────────
_SQ_EN = {
    "sq_ack": "Sure! 🙂",
    "sq_decline_order": "No problem! 🙂 If you'd like to stop, reply 'cancel'. Or just answer below to continue.",
    "sq_change_product": "Sure! You can browse our catalogue here: {catalogue_url}\nSend me the product name or code you'd like instead — or carry on with your current order.",
    "sq_why_details": "We need these details to deliver your order and prepare your invoice. 🙂",
    "sq_thanks": "You're welcome! 😊",
    "sq_positive": "Glad you like it! 😊",
    "sq_bye": "Thank you! 🙏 Message us here whenever you're ready.",
    "sq_think_later": "Sure, take your time! 🙏 Message here whenever you're ready and we'll continue.",
    "sq_cod_yes": "Yes, Cash on Delivery (COD) is available. 🙂",
    "sq_cod_yes_limit": "Yes, Cash on Delivery (COD) is available for orders up to ₹{limit}. 🙂",
    "sq_cod_no": "Sorry, we don't offer Cash on Delivery — payment is by UPI.",
    "sq_pay_upi_cod": "You can pay by UPI or Cash on Delivery (COD).",
    "sq_pay_upi": "You can pay by UPI.",
    "sq_pay_cod": "You can pay by Cash on Delivery (COD).",
    "sq_delivery_time": "Delivery usually takes {delivery_time} after your order is confirmed.",
    "sq_location": "Our address: {address}",
}
_SQ_HI = {
    "sq_ack": "Ji! 🙂",
    "sq_decline_order": "Koi baat nahi! 🙂 Agar rukna chahte hain to 'cancel' likhiye. Warna neeche ka jawab dekar aage badhiye.",
    "sq_change_product": "Zaroor! Hamara catalogue yahan dekhiye: {catalogue_url}\nJo product chahiye uska naam ya code bhejiye — ya apna current order jaari rakhiye.",
    "sq_why_details": "Ye details aapka order deliver karne aur invoice banane ke liye chahiye. 🙂",
    "sq_thanks": "Aapka swagat hai! 😊",
    "sq_positive": "Aapko pasand aaya, khushi hui! 😊",
    "sq_bye": "Dhanyavaad! 🙏 Jab bhi ready hon, yahin message kar dijiye.",
    "sq_think_later": "Zaroor, aaram se soch lijiye! 🙏 Jab ready hon yahin message kijiye, hum wahin se aage badhenge.",
    "sq_cod_yes": "Ji haan, Cash on Delivery (COD) available hai. 🙂",
    "sq_cod_yes_limit": "Ji haan, ₹{limit} tak ke orders par Cash on Delivery (COD) available hai. 🙂",
    "sq_cod_no": "Maaf kijiye, hamare yahan Cash on Delivery nahi hai — payment UPI se hota hai.",
    "sq_pay_upi_cod": "Aap UPI ya Cash on Delivery (COD) se payment kar sakte hain.",
    "sq_pay_upi": "Aap UPI se payment kar sakte hain.",
    "sq_pay_cod": "Aap Cash on Delivery (COD) se payment kar sakte hain.",
    "sq_delivery_time": "Order confirm hone ke baad delivery mein aam taur par {delivery_time} lagte hain.",
    "sq_location": "Hamara pata: {address}",
}
_SQ_GU = {
    "sq_ack": "Ji! 🙂",
    "sq_decline_order": "Koi vaat nahi! 🙂 Jo rokvu hoy to 'cancel' lakho. Nahi to niche no jawab aapi ne aagal vadho.",
    "sq_change_product": "Chokkas! Amaru catalogue ahi juo: {catalogue_url}\nJe product joiye teno naam athva code moklo — athva tamaro current order chalu rakho.",
    "sq_why_details": "Aa mahiti tamaro order deliver karva ane invoice banavva mate joiye chhe. 🙂",
    "sq_thanks": "Tamaru swagat chhe! 😊",
    "sq_positive": "Tamne gamyu e jaani khushi thai! 😊",
    "sq_bye": "Aabhar! 🙏 Jyare ready ho tyare ahi message karjo.",
    "sq_think_later": "Chokkas, nirante vichari lo! 🙏 Jyare ready ho tyare ahi message karjo, apne tyathi aagal vadhishu.",
    "sq_cod_yes": "Ha, Cash on Delivery (COD) available chhe. 🙂",
    "sq_cod_yes_limit": "Ha, ₹{limit} sudhina orders par Cash on Delivery (COD) available chhe. 🙂",
    "sq_cod_no": "Maaf karjo, amari paase Cash on Delivery nathi — payment UPI thi thay chhe.",
    "sq_pay_upi_cod": "Tame UPI athva Cash on Delivery (COD) thi payment kari shako cho.",
    "sq_pay_upi": "Tame UPI thi payment kari shako cho.",
    "sq_pay_cod": "Tame Cash on Delivery (COD) thi payment kari shako cho.",
    "sq_delivery_time": "Order confirm thaya pachi delivery ma saamanya rite {delivery_time} lage chhe.",
    "sq_location": "Amaru sarnamu: {address}",
}
_SQ_HI_DEV = {
    "sq_ack": "जी! 🙂",
    "sq_decline_order": "कोई बात नहीं! 🙂 अगर रुकना चाहते हैं तो 'रद्द' लिखिए। वरना नीचे का जवाब देकर आगे बढ़िए।",
    "sq_change_product": "ज़रूर! हमारा कैटलॉग यहाँ देखिए: {catalogue_url}\nजो प्रोडक्ट चाहिए उसका नाम या कोड भेजिए — या अपना मौजूदा ऑर्डर जारी रखिए।",
    "sq_why_details": "ये जानकारी आपका ऑर्डर डिलीवर करने और इनवॉइस बनाने के लिए चाहिए। 🙂",
    "sq_thanks": "आपका स्वागत है! 😊",
    "sq_positive": "आपको पसंद आया, खुशी हुई! 😊",
    "sq_bye": "धन्यवाद! 🙏 जब भी तैयार हों, यहीं मैसेज कर दीजिए।",
    "sq_think_later": "ज़रूर, आराम से सोच लीजिए! 🙏 जब तैयार हों यहीं मैसेज कीजिए, हम वहीं से आगे बढ़ेंगे।",
    "sq_cod_yes": "जी हाँ, कैश ऑन डिलीवरी (COD) उपलब्ध है। 🙂",
    "sq_cod_yes_limit": "जी हाँ, ₹{limit} तक के ऑर्डर पर कैश ऑन डिलीवरी (COD) उपलब्ध है। 🙂",
    "sq_cod_no": "माफ़ कीजिए, हमारे यहाँ कैश ऑन डिलीवरी नहीं है — पेमेंट UPI से होता है।",
    "sq_pay_upi_cod": "आप UPI या कैश ऑन डिलीवरी (COD) से पेमेंट कर सकते हैं।",
    "sq_pay_upi": "आप UPI से पेमेंट कर सकते हैं।",
    "sq_pay_cod": "आप कैश ऑन डिलीवरी (COD) से पेमेंट कर सकते हैं।",
    "sq_delivery_time": "ऑर्डर कन्फर्म होने के बाद डिलीवरी में आमतौर पर {delivery_time} लगते हैं।",
    "sq_location": "हमारा पता: {address}",
}
_SQ_GU_SCRIPT = {
    "sq_ack": "જી! 🙂",
    "sq_decline_order": "કોઈ વાત નહીં! 🙂 જો અટકાવવું હોય તો 'રદ' લખો. નહિતર નીચેનો જવાબ આપીને આગળ વધો.",
    "sq_change_product": "ચોક્કસ! અમારું કેટલોગ અહીં જુઓ: {catalogue_url}\nજે પ્રોડક્ટ જોઈએ તેનું નામ અથવા કોડ મોકલો — અથવા તમારો હાલનો ઓર્ડર ચાલુ રાખો.",
    "sq_why_details": "આ માહિતી તમારો ઓર્ડર ડિલિવર કરવા અને ઇન્વોઇસ બનાવવા માટે જોઈએ છે. 🙂",
    "sq_thanks": "તમારું સ્વાગત છે! 😊",
    "sq_positive": "તમને ગમ્યું એ જાણીને આનંદ થયો! 😊",
    "sq_bye": "આભાર! 🙏 જ્યારે તૈયાર હો ત્યારે અહીં મેસેજ કરજો.",
    "sq_think_later": "ચોક્કસ, નિરાંતે વિચારી લો! 🙏 જ્યારે તૈયાર હો ત્યારે અહીં મેસેજ કરજો, આપણે ત્યાંથી આગળ વધીશું.",
    "sq_cod_yes": "હા, કેશ ઓન ડિલિવરી (COD) ઉપલબ્ધ છે. 🙂",
    "sq_cod_yes_limit": "હા, ₹{limit} સુધીના ઓર્ડર પર કેશ ઓન ડિલિવરી (COD) ઉપલબ્ધ છે. 🙂",
    "sq_cod_no": "માફ કરશો, અમારી પાસે કેશ ઓન ડિલિવરી નથી — પેમેન્ટ UPI થી થાય છે.",
    "sq_pay_upi_cod": "તમે UPI અથવા કેશ ઓન ડિલિવરી (COD) થી પેમેન્ટ કરી શકો છો.",
    "sq_pay_upi": "તમે UPI થી પેમેન્ટ કરી શકો છો.",
    "sq_pay_cod": "તમે કેશ ઓન ડિલિવરી (COD) થી પેમેન્ટ કરી શકો છો.",
    "sq_delivery_time": "ઓર્ડર કન્ફર્મ થયા પછી ડિલિવરીમાં સામાન્ય રીતે {delivery_time} લાગે છે.",
    "sq_location": "અમારું સરનામું: {address}",
}
ENGLISH_TEMPLATES.update(_SQ_EN)
HINDI_TEMPLATES.update(_SQ_HI)
GUJARATI_TEMPLATES.update(_SQ_GU)
HINDI_DEVANAGARI_TEMPLATES.update(_SQ_HI_DEV)
GUJARATI_SCRIPT_TEMPLATES.update(_SQ_GU_SCRIPT)

# ── Out-of-scope boundary (router action `out_of_scope`) ─────────────────────
# Fixed replies — the model never writes text for a topic that is not the business's. Placeholders:
# {business}, {catalogue_url}. `_midorder` is only a prefix: the open order question is re-asked after it.
# `_repeat` is sent once the customer has gone off-topic `off_topic_threshold` times in a row and offers a human.
_OOS_EN = {
    "rt_out_of_scope": (
        "I can only help with {business}: products or services, orders, delivery and payment 🙂 "
        "You can browse here: {catalogue_url}\nWhat would you like to see?"
    ),
    "rt_out_of_scope_midorder": "I can only help with your order and {business} 🙂",
    "rt_out_of_scope_repeat": (
        "I can only help with {business}: products or services, orders, delivery and payment. "
        "For anything else, type 'talk to someone' and our team will reply."
    ),
}
_OOS_HI = {
    "rt_out_of_scope": (
        "Main sirf {business} mein madad kar sakta hoon: products ya services, orders, delivery aur payment 🙂 "
        "Catalogue yahan dekhein: {catalogue_url}\nAap kya dekhna chahenge?"
    ),
    "rt_out_of_scope_midorder": "Main sirf aapke order aur {business} mein madad kar sakta hoon 🙂",
    "rt_out_of_scope_repeat": (
        "Main sirf {business} mein madad kar sakta hoon: products ya services, orders, delivery aur payment. "
        "Kisi aur baat ke liye 'talk to someone' likhein, hamari team jawab degi."
    ),
}
_OOS_GU = {
    "rt_out_of_scope": (
        "Hu fakt {business} ma madad kari shaku chhu: products athva services, orders, delivery ane payment 🙂 "
        "Catalogue ahi juo: {catalogue_url}\nTame shu joso?"
    ),
    "rt_out_of_scope_midorder": "Hu fakt tamara order ane {business} ma madad kari shaku chhu 🙂",
    "rt_out_of_scope_repeat": (
        "Hu fakt {business} ma madad kari shaku chhu: products athva services, orders, delivery ane payment. "
        "Biji koi vaat mate 'talk to someone' lakho, amari team jawab aapshe."
    ),
}
_OOS_HI_DEV = {
    "rt_out_of_scope": (
        "मैं सिर्फ़ {business} में मदद कर सकता हूँ: प्रोडक्ट्स या सेवाएँ, ऑर्डर, डिलीवरी और पेमेंट 🙂 "
        "कैटलॉग यहाँ देखें: {catalogue_url}\nआप क्या देखना चाहेंगे?"
    ),
    "rt_out_of_scope_midorder": "मैं सिर्फ़ आपके ऑर्डर और {business} में मदद कर सकता हूँ 🙂",
    "rt_out_of_scope_repeat": (
        "मैं सिर्फ़ {business} में मदद कर सकता हूँ: प्रोडक्ट्स या सेवाएँ, ऑर्डर, डिलीवरी और पेमेंट। "
        "किसी और बात के लिए 'किसी से बात करनी है' लिखें, हमारी टीम जवाब देगी।"
    ),
}
_OOS_GU_SCRIPT = {
    "rt_out_of_scope": (
        "હું ફક્ત {business} માં મદદ કરી શકું છું: પ્રોડક્ટ્સ અથવા સેવાઓ, ઓર્ડર, ડિલિવરી અને પેમેન્ટ 🙂 "
        "કેટલોગ અહીં જુઓ: {catalogue_url}\nતમે શું જોશો?"
    ),
    "rt_out_of_scope_midorder": "હું ફક્ત તમારા ઓર્ડર અને {business} માં મદદ કરી શકું છું 🙂",
    "rt_out_of_scope_repeat": (
        "હું ફક્ત {business} માં મદદ કરી શકું છું: પ્રોડક્ટ્સ અથવા સેવાઓ, ઓર્ડર, ડિલિવરી અને પેમેન્ટ. "
        "બીજી કોઈ વાત માટે 'કોઈ સાથે વાત કરવી છે' લખો, અમારી ટીમ જવાબ આપશે."
    ),
}
ENGLISH_TEMPLATES.update(_OOS_EN)
HINDI_TEMPLATES.update(_OOS_HI)
GUJARATI_TEMPLATES.update(_OOS_GU)
HINDI_DEVANAGARI_TEMPLATES.update(_OOS_HI_DEV)
GUJARATI_SCRIPT_TEMPLATES.update(_OOS_GU_SCRIPT)

# ── Search miss: out of stock / not sold ─────────────────────────────────────
# rt_search_oos names a catalogue product (never the customer's text); rt_not_stocked is generic.
_NF_EN = {
    "rt_search_oos": "Sorry, {product} is currently out of stock.",
    "rt_not_stocked": "Sorry, that item is currently not available in our store.",
}
_NF_HI = {
    "rt_search_oos": "Maaf kijiye, {product} abhi stock mein nahi hai.",
    "rt_not_stocked": "Maaf kijiye, yeh item abhi hamare store mein available nahi hai.",
}
_NF_GU = {
    "rt_search_oos": "Maaf karjo, {product} hamna stock ma nathi.",
    "rt_not_stocked": "Maaf karjo, aa item hamna amara store ma available nathi.",
}
_NF_HI_DEV = {
    "rt_search_oos": "क्षमा करें, {product} अभी स्टॉक में नहीं है।",
    "rt_not_stocked": "क्षमा करें, यह आइटम अभी हमारे स्टोर में उपलब्ध नहीं है।",
}
_NF_GU_SCRIPT = {
    "rt_search_oos": "માફ કરજો, {product} હમણાં સ્ટોકમાં નથી.",
    "rt_not_stocked": "માફ કરજો, આ આઇટમ હમણાં અમારા સ્ટોરમાં ઉપલબ્ધ નથી.",
}
ENGLISH_TEMPLATES.update(_NF_EN)
HINDI_TEMPLATES.update(_NF_HI)
GUJARATI_TEMPLATES.update(_NF_GU)
HINDI_DEVANAGARI_TEMPLATES.update(_NF_HI_DEV)
GUJARATI_SCRIPT_TEMPLATES.update(_NF_GU_SCRIPT)

# ── Irrelevant-message guard (app/services/spam_guard.py) ────────────────────
# rt_junk_nudge: a fixed reply to an emoji-only / laughter message (no LLM). rt_spam_final: the ONE last notice before
# the bot goes quiet for {minutes} minutes. Placeholders: {business}, {catalogue_url}, {minutes}.
_SPAM_EN = {
    "rt_junk_nudge": "I didn't understand that 🙂 Please type what you're looking for — a product name or code. Browse here: {catalogue_url}",
    "rt_spam_final": (
        "I can only help with {business}: products or services, orders, delivery and payment. "
        "I'll pause replies for about {minutes} minutes. Meanwhile send a product code or type 'talk to someone' to reach our team. "
        "Browse here: {catalogue_url}"
    ),
}
_SPAM_HI = {
    "rt_junk_nudge": "Yeh mujhe samajh nahi aaya 🙂 Aap kya dhoondh rahe hain — product ka naam ya code likhein. Catalogue yahan dekhein: {catalogue_url}",
    "rt_spam_final": (
        "Main sirf {business} mein madad kar sakta hoon: products ya services, orders, delivery aur payment. "
        "Main kuch der ({minutes} minute) jawab nahi dunga. Tab tak product code bhejein ya 'talk to someone' likhkar hamari team se baat karein. "
        "Catalogue yahan dekhein: {catalogue_url}"
    ),
}
_SPAM_GU = {
    "rt_junk_nudge": "Aa mane samjayu nahi 🙂 Tame shu shodho chho — product nu naam ke code lakho. Catalogue ahi juo: {catalogue_url}",
    "rt_spam_final": (
        "Hu fakt {business} ma madad kari shaku chhu: products athva services, orders, delivery ane payment. "
        "Hu thodi vaar ({minutes} minute) jawab nahi aapu. Tya sudhi product code moklo athva 'talk to someone' lakhi amari team sathe vaat karo. "
        "Catalogue ahi juo: {catalogue_url}"
    ),
}
_SPAM_HI_DEV = {
    "rt_junk_nudge": "यह मुझे समझ नहीं आया 🙂 आप क्या ढूँढ रहे हैं — प्रोडक्ट का नाम या कोड लिखें। कैटलॉग यहाँ देखें: {catalogue_url}",
    "rt_spam_final": (
        "मैं सिर्फ़ {business} में मदद कर सकता हूँ: प्रोडक्ट्स या सेवाएँ, ऑर्डर, डिलीवरी और पेमेंट। "
        "मैं कुछ देर ({minutes} मिनट) जवाब नहीं दूँगा। तब तक प्रोडक्ट कोड भेजें या 'किसी से बात करनी है' लिखकर हमारी टीम से बात करें। "
        "कैटलॉग यहाँ देखें: {catalogue_url}"
    ),
}
_SPAM_GU_SCRIPT = {
    "rt_junk_nudge": "આ મને સમજાયું નહીં 🙂 તમે શું શોધી રહ્યા છો — પ્રોડક્ટનું નામ અથવા કોડ લખો. કેટલોગ અહીં જુઓ: {catalogue_url}",
    "rt_spam_final": (
        "હું ફક્ત {business} માં મદદ કરી શકું છું: પ્રોડક્ટ્સ અથવા સેવાઓ, ઓર્ડર, ડિલિવરી અને પેમેન્ટ. "
        "હું થોડી વાર ({minutes} મિનિટ) જવાબ નહીં આપું. ત્યાં સુધી પ્રોડક્ટ કોડ મોકલો અથવા 'કોઈ સાથે વાત કરવી છે' લખીને અમારી ટીમ સાથે વાત કરો. "
        "કેટલોગ અહીં જુઓ: {catalogue_url}"
    ),
}
ENGLISH_TEMPLATES.update(_SPAM_EN)
HINDI_TEMPLATES.update(_SPAM_HI)
GUJARATI_TEMPLATES.update(_SPAM_GU)
HINDI_DEVANAGARI_TEMPLATES.update(_SPAM_HI_DEV)
GUJARATI_SCRIPT_TEMPLATES.update(_SPAM_GU_SCRIPT)

# ── Chat-flow hardening (order_recovery.py, router_actions.py, side_questions.py) ─────────────────────────
# Cancel confirmation, "confused", draft restore, "more like this", 3-way product switch, discount follow-ups.
_FLOW_EN = {
    "pay_proof_received_high_value": "Thanks! This is a high-value order, so our team will check your payment manually before confirming it. We'll update you here.",
    "rt_eta_single": "📦 Expected delivery: {date}.",
    "summary_upi_only_label": "UPI only",
    "ask_cart_colors": "Which colours would you like for your {n} pieces, and how many of each? Reply like '1 Red, 1 Pink' (available: {colors})",
    "rt_cancel_confirm": "Cancel this order? (Yes / No)",
    "btn_cancel_yes": "Yes, cancel",
    "btn_cancel_no": "No, keep it",
    "rt_cancel_kept": "Okay, your order is still on. 👍",
    "rt_confused_prefix": "Sorry for the confusion — let me ask it more simply.",
    "rt_confused_split": "Sorry for the confusion — let's keep it simple: the same option for every piece.",
    "rt_confused_payment": "Sorry for the confusion! To finish your order: pay to the UPI ID I sent, then send the payment screenshot here. Type 'talk to someone' if you need help.",
    "rt_confused_idle": "Sorry for the confusion! At {business} I can show you products, take an order or check an order's status. Browse here: {catalogue_url}\nWhat would you like to do?",
    "rt_restored": "Restored your order: {summary}",
    "rt_restore_unavailable": "Sorry, that item isn't available any more, so I couldn't restore your order. Browse here: {catalogue_url}",
    "rt_restore_expired": "That cancelled order is too old to restore. Browse here: {catalogue_url} and I'll start a new one with you.",
    "rt_restore_offer": "Want your cancelled order back? Reply 'continue' to restore it.",
    "rt_menu_fallback": "I can show you products, take an order or check an order's status. Browse here: {catalogue_url}\nWhat would you like to do?",
    "rt_pick_header": "Please reply with the number of your choice:",
    "rt_more_header": "Here are some more options 👇",
    "rt_more_cheaper_header": "Here are similar options at a lower price 👇",
    "rt_more_none": "That's everything I have like that right now. You can browse the full catalogue here: {catalogue_url}",
    "rt_more_cheaper_none": "I don't have anything similar at a lower price right now. You can browse everything here: {catalogue_url}",
    "switch_offer3": "You're ordering {pinned}. Switch to {product}, add both, or keep {pinned}?",
    "btn_switch": "Switch",
    "btn_add_both": "Add both",
    "btn_keep_current": "Keep current",
    "switch_kept": "Okay, keeping {pinned}. 👍",
    "switch_add_both_later": "Okay, I'll add both to this order. Let's finish {pinned} first — {product} comes next.",
    "switch_add_both_now": "Added to your order. Now let's set up {product}:",
    "sq_discount": "Our prices are fixed and reflect guaranteed quality.",
    "sq_discount_repeat": "I understand, and I'm sorry — the price is the same for everyone, so I can't reduce it.",
    "btn_add_item": "Add {name}",
}
_FLOW_HI = {
    "pay_proof_received_high_value": "Shukriya! Ye high-value order hai, isliye hamari team confirm karne se pehle aapka payment khud check karegi. Yahin update dengi.",
    "rt_eta_single": "📦 Delivery ka anumaan: {date}.",
    "summary_upi_only_label": "sirf UPI",
    "ask_cart_colors": "Aapko apne {n} pieces ke liye kaun se colours chahiye, aur har colour ke kitne? Aise likhein '1 Red, 1 Pink' (available: {colors})",
    "rt_cancel_confirm": "Is order ko cancel karna hai? (Haan / Nahi)",
    "btn_cancel_yes": "Haan, cancel",
    "btn_cancel_no": "Nahi, rakhein",
    "rt_cancel_kept": "Theek hai, aapka order chalu hai. 👍",
    "rt_confused_prefix": "Confusion ke liye maaf kijiye — main aasaan tarike se poochta hoon.",
    "rt_confused_split": "Confusion ke liye maaf kijiye — chaliye simple rakhte hain: har piece ke liye same option.",
    "rt_confused_payment": "Confusion ke liye maaf kijiye! Order poora karne ke liye: bheje gaye UPI ID par payment karein, phir payment ka screenshot yahin bhejein. Madad chahiye to 'talk to someone' likhein.",
    "rt_confused_idle": "Confusion ke liye maaf kijiye! {business} mein main products dikha sakta hoon, order le sakta hoon ya order status bata sakta hoon. Yahan dekhein: {catalogue_url}\nAap kya karna chahenge?",
    "rt_restored": "Aapka order wapas laga diya: {summary}",
    "rt_restore_unavailable": "Maaf kijiye, ye item ab available nahi hai, isliye order wapas nahi laga paya. Yahan dekhein: {catalogue_url}",
    "rt_restore_expired": "Wo cancel kiya hua order ab bahut purana ho gaya hai. Yahan dekhein: {catalogue_url}, hum naya order shuru kar denge.",
    "rt_restore_offer": "Cancel kiya hua order wapas chahiye? 'continue' likhein.",
    "rt_menu_fallback": "Main products dikha sakta hoon, order le sakta hoon ya order status bata sakta hoon. Yahan dekhein: {catalogue_url}\nAap kya karna chahenge?",
    "rt_pick_header": "Kripya apne choice ka number likhein:",
    "rt_more_header": "Ye kuch aur options hain 👇",
    "rt_more_cheaper_header": "Ye kam daam ke milte-julte options hain 👇",
    "rt_more_none": "Abhi isse milta-julta aur kuch nahi hai. Poora catalogue yahan dekhein: {catalogue_url}",
    "rt_more_cheaper_none": "Abhi kam daam mein isse milta-julta kuch nahi hai. Sab kuch yahan dekhein: {catalogue_url}",
    "switch_offer3": "Aap {pinned} order kar rahe hain. {product} par switch karein, dono add karein, ya {pinned} hi rakhein?",
    "btn_switch": "Switch karein",
    "btn_add_both": "Dono add karein",
    "btn_keep_current": "Abhi wala rakhein",
    "switch_kept": "Theek hai, {pinned} hi rakhte hain. 👍",
    "switch_add_both_later": "Theek hai, dono is order mein add karte hain. Pehle {pinned} poora karte hain — phir {product}.",
    "switch_add_both_now": "Order mein add ho gaya. Ab {product} set karte hain:",
    "sq_discount": "Hamare prices fixed hain aur guaranteed quality ke hisaab se hain.",
    "sq_discount_repeat": "Main samajhta hoon, maaf kijiye — price sabke liye same hai, isliye kam nahi kar sakta.",
    "btn_add_item": "{name} add karein",
}
_FLOW_GU = {
    "pay_proof_received_high_value": "Aabhar! Aa high-value order chhe, etle amari team confirm karta pehla tamaru payment jaate check karse. Ahi j update apishu.",
    "rt_eta_single": "📦 Delivery no anumaan: {date}.",
    "summary_upi_only_label": "fakt UPI",
    "ask_cart_colors": "Tamne tamara {n} pieces mate kya colours joiye ane darek na ketla? Aam lakho '1 Red, 1 Pink' (available: {colors})",
    "rt_cancel_confirm": "Shu aa order cancel karvo chhe? (Ha / Na)",
    "btn_cancel_yes": "Ha, cancel",
    "btn_cancel_no": "Na, rakho",
    "rt_cancel_kept": "Saru, tamaro order chalu chhe. 👍",
    "rt_confused_prefix": "Confusion mate maaf karjo — hu saral rite puchhu chhu.",
    "rt_confused_split": "Confusion mate maaf karjo — chalo saral rakhiye: darek piece mate same option.",
    "rt_confused_payment": "Confusion mate maaf karjo! Order puro karva: moklela UPI ID par payment karo, pachhi payment no screenshot ahi moklo. Madad joiye to 'talk to someone' lakho.",
    "rt_confused_idle": "Confusion mate maaf karjo! {business} ma hu products batavi shaku, order lai shaku ke order status kahi shaku. Ahi juo: {catalogue_url}\nTame shu karva mango chho?",
    "rt_restored": "Tamaro order pacho lagavi didho: {summary}",
    "rt_restore_unavailable": "Maaf karjo, aa item have available nathi, etle order pacho lagavi shakyo nahi. Ahi juo: {catalogue_url}",
    "rt_restore_expired": "Aa cancel karelo order have bahu juno thai gayo chhe. Ahi juo: {catalogue_url}, ame navo order sharu kari daishu.",
    "rt_restore_offer": "Cancel karelo order pacho joiye chhe? 'continue' lakho.",
    "rt_menu_fallback": "Hu products batavi shaku, order lai shaku ke order status kahi shaku. Ahi juo: {catalogue_url}\nTame shu karva mango chho?",
    "rt_pick_header": "Krupa kari ne tamari pasand no number lakho:",
    "rt_more_header": "Aa biji kehvi options chhe 👇",
    "rt_more_cheaper_header": "Aa ochha bhav na malta-aavta options chhe 👇",
    "rt_more_none": "Hamna aana jevu biju kai nathi. Puru catalogue ahi juo: {catalogue_url}",
    "rt_more_cheaper_none": "Hamna ochha bhav ma aana jevu kai nathi. Badhu ahi juo: {catalogue_url}",
    "switch_offer3": "Tame {pinned} order kari rahya chho. {product} par switch karo, banne add karo, ke {pinned} j rakho?",
    "btn_switch": "Switch karo",
    "btn_add_both": "Banne add karo",
    "btn_keep_current": "Aa j rakho",
    "switch_kept": "Saru, {pinned} j rakhiye chhie. 👍",
    "switch_add_both_later": "Saru, banne aa order ma add karishu. Pehla {pinned} puru karie — pachhi {product}.",
    "switch_add_both_now": "Order ma add thai gayu. Have {product} set karie:",
    "sq_discount": "Amara bhav fix chhe ane guaranteed quality mujab chhe.",
    "sq_discount_repeat": "Hu samajhu chhu, maaf karjo — bhav badha mate same chhe, etle ghatadi shakato nathi.",
    "btn_add_item": "{name} add karo",
}
_FLOW_HI_DEV = {
    "pay_proof_received_high_value": "धन्यवाद! यह हाई-वैल्यू ऑर्डर है, इसलिए हमारी टीम कन्फ़र्म करने से पहले आपका पेमेंट खुद जाँचेगी। यहीं अपडेट देंगे।",
    "rt_eta_single": "📦 डिलीवरी का अनुमान: {date}।",
    "summary_upi_only_label": "सिर्फ UPI",
    "ask_cart_colors": "आपको अपने {n} पीस के लिए कौन से रंग चाहिए, और हर रंग के कितने? ऐसे लिखें '1 Red, 1 Pink' (उपलब्ध: {colors})",
    "rt_cancel_confirm": "क्या यह ऑर्डर कैंसल करना है? (हाँ / नहीं)",
    "btn_cancel_yes": "हाँ, कैंसल",
    "btn_cancel_no": "नहीं, रखें",
    "rt_cancel_kept": "ठीक है, आपका ऑर्डर चालू है। 👍",
    "rt_confused_prefix": "कन्फ्यूज़न के लिए माफ़ कीजिए — मैं आसान तरीके से पूछता हूँ।",
    "rt_confused_split": "कन्फ्यूज़न के लिए माफ़ कीजिए — चलिए सरल रखते हैं: हर पीस के लिए एक ही विकल्प।",
    "rt_confused_payment": "कन्फ्यूज़न के लिए माफ़ कीजिए! ऑर्डर पूरा करने के लिए: भेजे गए UPI ID पर पेमेंट करें, फिर पेमेंट का स्क्रीनशॉट यहीं भेजें। मदद चाहिए तो 'talk to someone' लिखें।",
    "rt_confused_idle": "कन्फ्यूज़न के लिए माफ़ कीजिए! {business} में मैं प्रोडक्ट दिखा सकता हूँ, ऑर्डर ले सकता हूँ या ऑर्डर स्टेटस बता सकता हूँ। यहाँ देखें: {catalogue_url}\nआप क्या करना चाहेंगे?",
    "rt_restored": "आपका ऑर्डर वापस लगा दिया: {summary}",
    "rt_restore_unavailable": "क्षमा करें, यह आइटम अब उपलब्ध नहीं है, इसलिए ऑर्डर वापस नहीं लगा सका। यहाँ देखें: {catalogue_url}",
    "rt_restore_expired": "वह कैंसल किया हुआ ऑर्डर अब बहुत पुराना हो गया है। यहाँ देखें: {catalogue_url}, हम नया ऑर्डर शुरू कर देंगे।",
    "rt_restore_offer": "कैंसल किया हुआ ऑर्डर वापस चाहिए? 'continue' लिखें।",
    "rt_menu_fallback": "मैं प्रोडक्ट दिखा सकता हूँ, ऑर्डर ले सकता हूँ या ऑर्डर स्टेटस बता सकता हूँ। यहाँ देखें: {catalogue_url}\nआप क्या करना चाहेंगे?",
    "rt_pick_header": "कृपया अपनी पसंद का नंबर लिखें:",
    "rt_more_header": "ये कुछ और विकल्प हैं 👇",
    "rt_more_cheaper_header": "ये कम दाम के मिलते-जुलते विकल्प हैं 👇",
    "rt_more_none": "अभी इसके जैसा और कुछ नहीं है। पूरा कैटलॉग यहाँ देखें: {catalogue_url}",
    "rt_more_cheaper_none": "अभी कम दाम में इसके जैसा कुछ नहीं है। सब कुछ यहाँ देखें: {catalogue_url}",
    "switch_offer3": "आप {pinned} ऑर्डर कर रहे हैं। {product} पर स्विच करें, दोनों जोड़ें, या {pinned} ही रखें?",
    "btn_switch": "स्विच करें",
    "btn_add_both": "दोनों जोड़ें",
    "btn_keep_current": "अभी वाला रखें",
    "switch_kept": "ठीक है, {pinned} ही रखते हैं। 👍",
    "switch_add_both_later": "ठीक है, दोनों इस ऑर्डर में जोड़ते हैं। पहले {pinned} पूरा करते हैं — फिर {product}।",
    "switch_add_both_now": "ऑर्डर में जुड़ गया। अब {product} सेट करते हैं:",
    "sq_discount": "हमारे दाम तय हैं और बेहतरीन क्वालिटी के अनुसार हैं।",
    "sq_discount_repeat": "मैं समझता हूँ, माफ़ कीजिए — दाम सबके लिए एक जैसा है, इसलिए कम नहीं कर सकता।",
    "btn_add_item": "{name} जोड़ें",
}
_FLOW_GU_SCRIPT = {
    "pay_proof_received_high_value": "આભાર! આ હાઈ-વેલ્યુ ઓર્ડર છે, એટલે અમારી ટીમ કન્ફર્મ કરતાં પહેલાં તમારું પેમેન્ટ જાતે ચકાસશે. અહીં જ અપડેટ આપીશું.",
    "rt_eta_single": "📦 ડિલિવરીનો અંદાજ: {date}.",
    "summary_upi_only_label": "ફક્ત UPI",
    "ask_cart_colors": "તમને તમારા {n} પીસ માટે કયા રંગ જોઈએ, અને દરેકના કેટલા? આમ લખો '1 Red, 1 Pink' (ઉપલબ્ધ: {colors})",
    "rt_cancel_confirm": "શું આ ઓર્ડર કેન્સલ કરવો છે? (હા / ના)",
    "btn_cancel_yes": "હા, કેન્સલ",
    "btn_cancel_no": "ના, રાખો",
    "rt_cancel_kept": "સારું, તમારો ઓર્ડર ચાલુ છે. 👍",
    "rt_confused_prefix": "કન્ફ્યુઝન માટે માફ કરજો — હું સરળ રીતે પૂછું છું.",
    "rt_confused_split": "કન્ફ્યુઝન માટે માફ કરજો — ચાલો સરળ રાખીએ: દરેક પીસ માટે એક જ વિકલ્પ.",
    "rt_confused_payment": "કન્ફ્યુઝન માટે માફ કરજો! ઓર્ડર પૂરો કરવા: મોકલેલા UPI ID પર પેમેન્ટ કરો, પછી પેમેન્ટનો સ્ક્રીનશોટ અહીં મોકલો. મદદ જોઈએ તો 'talk to someone' લખો.",
    "rt_confused_idle": "કન્ફ્યુઝન માટે માફ કરજો! {business} માં હું પ્રોડક્ટ બતાવી શકું, ઓર્ડર લઈ શકું કે ઓર્ડર સ્ટેટસ કહી શકું. અહીં જુઓ: {catalogue_url}\nતમે શું કરવા માંગો છો?",
    "rt_restored": "તમારો ઓર્ડર પાછો લગાવી દીધો: {summary}",
    "rt_restore_unavailable": "માફ કરજો, આ આઇટમ હવે ઉપલબ્ધ નથી, એટલે ઓર્ડર પાછો લગાવી શક્યો નહીં. અહીં જુઓ: {catalogue_url}",
    "rt_restore_expired": "તે કેન્સલ કરેલો ઓર્ડર હવે બહુ જૂનો થઈ ગયો છે. અહીં જુઓ: {catalogue_url}, અમે નવો ઓર્ડર શરૂ કરી દઈશું.",
    "rt_restore_offer": "કેન્સલ કરેલો ઓર્ડર પાછો જોઈએ છે? 'continue' લખો.",
    "rt_menu_fallback": "હું પ્રોડક્ટ બતાવી શકું, ઓર્ડર લઈ શકું કે ઓર્ડર સ્ટેટસ કહી શકું. અહીં જુઓ: {catalogue_url}\nતમે શું કરવા માંગો છો?",
    "rt_pick_header": "કૃપા કરીને તમારી પસંદનો નંબર લખો:",
    "rt_more_header": "આ બીજા કેટલાક વિકલ્પો છે 👇",
    "rt_more_cheaper_header": "આ ઓછા ભાવના મળતા-આવતા વિકલ્પો છે 👇",
    "rt_more_none": "હમણાં આના જેવું બીજું કંઈ નથી. પૂરો કેટલોગ અહીં જુઓ: {catalogue_url}",
    "rt_more_cheaper_none": "હમણાં ઓછા ભાવમાં આના જેવું કંઈ નથી. બધું અહીં જુઓ: {catalogue_url}",
    "switch_offer3": "તમે {pinned} ઓર્ડર કરી રહ્યા છો. {product} પર સ્વિચ કરો, બંને ઉમેરો, કે {pinned} જ રાખો?",
    "btn_switch": "સ્વિચ કરો",
    "btn_add_both": "બંને ઉમેરો",
    "btn_keep_current": "આ જ રાખો",
    "switch_kept": "સારું, {pinned} જ રાખીએ છીએ. 👍",
    "switch_add_both_later": "સારું, બંને આ ઓર્ડરમાં ઉમેરીશું. પહેલા {pinned} પૂરું કરીએ — પછી {product}.",
    "switch_add_both_now": "ઓર્ડરમાં ઉમેરાઈ ગયું. હવે {product} સેટ કરીએ:",
    "sq_discount": "અમારા ભાવ નિશ્ચિત છે અને ગેરંટીવાળી ક્વોલિટી મુજબ છે.",
    "sq_discount_repeat": "હું સમજું છું, માફ કરજો — ભાવ બધા માટે સરખો છે, એટલે ઘટાડી શકતો નથી.",
    "btn_add_item": "{name} ઉમેરો",
}
ENGLISH_TEMPLATES.update(_FLOW_EN)
HINDI_TEMPLATES.update(_FLOW_HI)
GUJARATI_TEMPLATES.update(_FLOW_GU)
HINDI_DEVANAGARI_TEMPLATES.update(_FLOW_HI_DEV)
GUJARATI_SCRIPT_TEMPLATES.update(_FLOW_GU_SCRIPT)

# ── payment-proof classification (an image sent while an order awaits payment is classified, not assumed) ──
_PROOF_EN = {
    "pay_ask_image_kind": "Is this image your payment screenshot, or a product photo?",
    "btn_proof_payment": "Payment screenshot",
    "btn_proof_product": "Product photo",
    "pay_which_order": "Which order is this payment for?",
    "pay_proof_received_order": "Thanks! Payment screenshot received for order #{order_number}. Our team will verify it shortly and update you here.",
    "pay_proof_withdrawn": "Okay — I've taken that image out of your payment for order #{order_number}. It is still waiting for your payment screenshot.",
    "pay_product_photo_ask": "Nice! Want me to find something similar? Share a product code or tell me what you're looking for."
}
_PROOF_HI = {
    "pay_ask_image_kind": "Ye image payment ka screenshot hai ya product ki photo?",
    "btn_proof_payment": "Payment screenshot",
    "btn_proof_product": "Product photo",
    "pay_which_order": "Ye payment kaunse order ka hai?",
    "pay_proof_received_order": "Shukriya! Order #{order_number} ka payment screenshot mil gaya. Hamari team jaldi verify karke yahin update degi.",
    "pay_proof_withdrawn": "Theek hai — maine order #{order_number} ke payment se wo image hata di. Ab bhi payment screenshot ka intezaar hai.",
    "pay_product_photo_ask": "Achhi photo! Kya main isse milta-julta kuch dhoondhu? Product code bhejein ya batayein ki aap kya dhoondh rahe hain."
}
_PROOF_GU = {
    "pay_ask_image_kind": "Aa image payment no screenshot chhe ke product no photo?",
    "btn_proof_payment": "Payment screenshot",
    "btn_proof_product": "Product photo",
    "pay_which_order": "Aa payment kaya order nu chhe?",
    "pay_proof_received_order": "Aabhar! Order #{order_number} no payment screenshot mali gayo. Amari team jaldi verify kari ne ahi j update api dese.",
    "pay_proof_withdrawn": "Saru — me order #{order_number} na payment mathi te image kadhi nakhi. Haji payment screenshot ni raah chhe.",
    "pay_product_photo_ask": "Saras photo! Shu hu aani jevu kai shodhu? Product code moklo ke tame shu shodho chho te kaho."
}
_PROOF_HI_DEV = {
    "pay_ask_image_kind": "क्या यह इमेज पेमेंट का स्क्रीनशॉट है या प्रोडक्ट की फ़ोटो?",
    "btn_proof_payment": "पेमेंट स्क्रीनशॉट",
    "btn_proof_product": "प्रोडक्ट फ़ोटो",
    "pay_which_order": "यह पेमेंट किस ऑर्डर का है?",
    "pay_proof_received_order": "धन्यवाद! ऑर्डर #{order_number} का पेमेंट स्क्रीनशॉट मिल गया। हमारी टीम जल्द ही वेरिफ़ाई करके यहीं अपडेट देगी।",
    "pay_proof_withdrawn": "ठीक है — मैंने ऑर्डर #{order_number} के पेमेंट से वह इमेज हटा दी है। अभी भी पेमेंट स्क्रीनशॉट का इंतज़ार है।",
    "pay_product_photo_ask": "बढ़िया फ़ोटो! क्या मैं इससे मिलता-जुलता कुछ ढूँढूँ? प्रोडक्ट कोड भेजें या बताएँ कि आप क्या ढूँढ रहे हैं।"
}
_PROOF_GU_SCRIPT = {
    "pay_ask_image_kind": "શું આ ઇમેજ પેમેન્ટનો સ્ક્રીનશૉટ છે કે પ્રોડક્ટનો ફોટો?",
    "btn_proof_payment": "પેમેન્ટ સ્ક્રીનશૉટ",
    "btn_proof_product": "પ્રોડક્ટ ફોટો",
    "pay_which_order": "આ પેમેન્ટ કયા ઓર્ડરનું છે?",
    "pay_proof_received_order": "આભાર! ઓર્ડર #{order_number} નો પેમેન્ટ સ્ક્રીનશૉટ મળી ગયો. અમારી ટીમ જલ્દી વેરિફાય કરીને અહીં જ અપડેટ આપશે.",
    "pay_proof_withdrawn": "સારું — મેં ઓર્ડર #{order_number} ના પેમેન્ટમાંથી તે ઇમેજ કાઢી નાખી છે. હજી પેમેન્ટ સ્ક્રીનશૉટની રાહ છે.",
    "pay_product_photo_ask": "સરસ ફોટો! શું હું આના જેવું કંઈ શોધું? પ્રોડક્ટ કોડ મોકલો અથવા તમે શું શોધો છો તે કહો."
}
ENGLISH_TEMPLATES.update(_PROOF_EN)
HINDI_TEMPLATES.update(_PROOF_HI)
GUJARATI_TEMPLATES.update(_PROOF_GU)
HINDI_DEVANAGARI_TEMPLATES.update(_PROOF_HI_DEV)
GUJARATI_SCRIPT_TEMPLATES.update(_PROOF_GU_SCRIPT)

ENGLISH_TEMPLATES["pay_denial_nothing"] = "Okay — no payment screenshot is attached to order #{order_number} right now. Send it once you've paid, or tell me what you're looking for."
HINDI_TEMPLATES["pay_denial_nothing"] = "Theek hai — abhi order #{order_number} par koi payment screenshot juda nahi hai. Payment karke bhej dein, ya batayein ki aap kya dhoondh rahe hain."
GUJARATI_TEMPLATES["pay_denial_nothing"] = "Saru — hamna order #{order_number} par koi payment screenshot jodayelo nathi. Payment kari ne moklo, ke tame shu shodho chho te kaho."
HINDI_DEVANAGARI_TEMPLATES["pay_denial_nothing"] = "ठीक है — अभी ऑर्डर #{order_number} पर कोई पेमेंट स्क्रीनशॉट जुड़ा नहीं है। पेमेंट करके भेज दें, या बताएँ कि आप क्या ढूँढ रहे हैं।"
GUJARATI_SCRIPT_TEMPLATES["pay_denial_nothing"] = "સારું — હમણાં ઓર્ડર #{order_number} પર કોઈ પેમેન્ટ સ્ક્રીનશૉટ જોડાયેલો નથી. પેમેન્ટ કરીને મોકલો, અથવા તમે શું શોધો છો તે કહો."

# ── payment-image round (v1.6): stale picker, payment status, unsupported message type ──
_PAY16 = {
    "english": {
        "pay_stale_pick": "Your last screenshot is linked to {linked}. Please send a separate screenshot for {wanted} 📸",
        "pay_already_linked": "That screenshot is already linked to {order}.",
        "pay_status_header": "Here is where your payments stand:",
        "pay_status_pending": "• {order} ({amount}) — not paid yet. Please send the payment screenshot 📸",
        "pay_status_submitted": "• {order} ({amount}) — screenshot received, waiting for verification",
        "pay_status_paid": "• {order} ({amount}) — payment confirmed ✅",
        "pay_status_none": "I can't see any recent order with a payment to check. Tell me what you're looking for and I'll help.",
        "pay_pick_order_button": "Choose order",
        "unsupported_message": "Sorry, I can't open that type of message. Please send text, a photo or a voice note.",
    },
    "hindi_roman": {
        "pay_stale_pick": "Aapka aakhri screenshot {linked} se juda hai. {wanted} ke liye alag screenshot bhejein 📸",
        "pay_already_linked": "Ye screenshot pehle se {order} se juda hai.",
        "pay_status_header": "Aapke payments ka haal ye hai:",
        "pay_status_pending": "• {order} ({amount}) — payment abhi nahi hua. Kripya payment ka screenshot bhejein 📸",
        "pay_status_submitted": "• {order} ({amount}) — screenshot mil gaya, verification baaki hai",
        "pay_status_paid": "• {order} ({amount}) — payment confirm ho gaya ✅",
        "pay_status_none": "Mujhe check karne layak koi recent order nahi dikh raha. Batayein aap kya dhoondh rahe hain.",
        "pay_pick_order_button": "Order chunein",
        "unsupported_message": "Maaf kijiye, main is tarah ka message nahi khol sakta. Kripya text, photo ya voice note bhejein.",
    },
    "gujarati_roman": {
        "pay_stale_pick": "Tamaro chhello screenshot {linked} sathe jodayelo chhe. {wanted} mate alag screenshot moklo 📸",
        "pay_already_linked": "Aa screenshot pehla thi j {order} sathe jodayelo chhe.",
        "pay_status_header": "Tamara payments ni sthiti:",
        "pay_status_pending": "• {order} ({amount}) — payment haju thayu nathi. Krupa kari ne payment no screenshot moklo 📸",
        "pay_status_submitted": "• {order} ({amount}) — screenshot mali gayo, verification baki chhe",
        "pay_status_paid": "• {order} ({amount}) — payment confirm thai gayu ✅",
        "pay_status_none": "Mane check karva jevo koi taajo order dekhato nathi. Tame shu shodho chho te kaho.",
        "pay_pick_order_button": "Order pasand karo",
        "unsupported_message": "Maaf karjo, hu aa prakar no message kholi shakto nathi. Krupa kari ne text, photo ke voice note moklo.",
    },
    "hindi_devanagari": {
        "pay_stale_pick": "आपका आख़िरी स्क्रीनशॉट {linked} से जुड़ा है। {wanted} के लिए अलग स्क्रीनशॉट भेजें 📸",
        "pay_already_linked": "यह स्क्रीनशॉट पहले से {order} से जुड़ा है।",
        "pay_status_header": "आपके पेमेंट की स्थिति:",
        "pay_status_pending": "• {order} ({amount}) — पेमेंट अभी नहीं हुआ। कृपया पेमेंट का स्क्रीनशॉट भेजें 📸",
        "pay_status_submitted": "• {order} ({amount}) — स्क्रीनशॉट मिल गया, वेरिफ़िकेशन बाक़ी है",
        "pay_status_paid": "• {order} ({amount}) — पेमेंट कन्फ़र्म हो गया ✅",
        "pay_status_none": "मुझे जाँचने लायक कोई हालिया ऑर्डर नहीं दिख रहा। बताएँ आप क्या ढूँढ रहे हैं।",
        "pay_pick_order_button": "ऑर्डर चुनें",
        "unsupported_message": "माफ़ कीजिए, मैं इस तरह का मैसेज नहीं खोल सकता। कृपया टेक्स्ट, फ़ोटो या वॉइस नोट भेजें।",
    },
    "gujarati_script": {
        "pay_stale_pick": "તમારો છેલ્લો સ્ક્રીનશૉટ {linked} સાથે જોડાયેલો છે. {wanted} માટે અલગ સ્ક્રીનશૉટ મોકલો 📸",
        "pay_already_linked": "આ સ્ક્રીનશૉટ પહેલેથી {order} સાથે જોડાયેલો છે.",
        "pay_status_header": "તમારા પેમેન્ટની સ્થિતિ:",
        "pay_status_pending": "• {order} ({amount}) — પેમેન્ટ હજી થયું નથી. કૃપા કરીને પેમેન્ટનો સ્ક્રીનશૉટ મોકલો 📸",
        "pay_status_submitted": "• {order} ({amount}) — સ્ક્રીનશૉટ મળી ગયો, વેરિફિકેશન બાકી છે",
        "pay_status_paid": "• {order} ({amount}) — પેમેન્ટ કન્ફર્મ થઈ ગયું ✅",
        "pay_status_none": "મને તપાસવા જેવો કોઈ તાજો ઓર્ડર દેખાતો નથી. તમે શું શોધો છો તે કહો.",
        "pay_pick_order_button": "ઓર્ડર પસંદ કરો",
        "unsupported_message": "માફ કરજો, હું આ પ્રકારનો મેસેજ ખોલી શકતો નથી. કૃપા કરીને ટેક્સ્ટ, ફોટો અથવા વૉઇસ નોટ મોકલો.",
    },
}
ENGLISH_TEMPLATES.update(_PAY16["english"])
HINDI_TEMPLATES.update(_PAY16["hindi_roman"])
GUJARATI_TEMPLATES.update(_PAY16["gujarati_roman"])
HINDI_DEVANAGARI_TEMPLATES.update(_PAY16["hindi_devanagari"])
GUJARATI_SCRIPT_TEMPLATES.update(_PAY16["gujarati_script"])


TEMPLATES: dict[str, dict[str, str]] = {
    "english": ENGLISH_TEMPLATES,
    "hindi_roman": HINDI_TEMPLATES,
    "hindi_devanagari": HINDI_DEVANAGARI_TEMPLATES,
    "hinglish": HINDI_TEMPLATES,
    "gujarati_roman": GUJARATI_TEMPLATES,
    "gujarati_script": GUJARATI_SCRIPT_TEMPLATES,
}


_BUSINESS_DAYS_RE = re.compile(r"^(\d+)(?:\s*[–-]\s*(\d+))? business days?$")


def _localize_delivery_phrase(value: str, templates: dict[str, str]) -> str:
    """'3–7 business days' / '1 business day' (built in English by get_delivery_time_str) → the template language."""
    match = _BUSINESS_DAYS_RE.match(value.strip())
    if not match:
        return value
    lo, hi = match.group(1), match.group(2)
    if hi and hi != lo:
        pattern = templates.get("rt_days_range")
        return pattern.format(lo=lo, hi=hi) if pattern else value
    pattern = templates.get("rt_days_fixed")
    return pattern.format(n=lo) if pattern else value


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
    if templates is not ENGLISH_TEMPLATES:
        kwargs = {k: _localize_delivery_phrase(v, templates) if isinstance(v, str) else v for k, v in kwargs.items()}
    try:
        return template.format(**kwargs)
    except KeyError:
        return template
