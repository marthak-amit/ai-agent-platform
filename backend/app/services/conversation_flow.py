"""
Conversation flow engine — stage detection and stage-specific prompt instructions.

Stages model the customer's journey from first contact to completed order.
detect_stage() uses the AI to classify where the conversation currently sits,
then get_stage_instructions() returns focused guidance for that stage.
"""

from __future__ import annotations

import re

# Words that must never be saved as a customer name.
# Includes command/reset words the test simulator sends, common short tokens,
# and anything that cannot be a real person's name.
COMMAND_WORDS: frozenset[str] = frozenset({
    "reset", "quit", "exit", "demo", "sales",
    "image", "help", "start", "stop", "test",
    "/reset", "/quit", "/demo", "/start", "/stop",
    "hi", "hello", "namaste", "haan", "ha", "nahi", "okay", "ok",
    "yes", "no", "paid", "done", "sent", "transferred", "completed",
    "cod", "upi", "gpay", "phonepay", "paytm", "phonepe",
    "s", "m", "l", "xl", "xxl", "xs", "xxxl",
    "red", "blue", "green", "pink", "navy", "yellow", "white", "black",
    "purple", "orange", "maroon", "gold", "silver", "beige", "brown",
})


def is_valid_name(text: str) -> bool:
    """
    Return True only when *text* looks like a genuine customer name.

    A valid name must have at least 2 characters, contain no digits,
    not be a command/reset word, and not start with '/'.
    """
    stripped = text.strip().lower()
    if len(stripped) < 2:
        return False
    if stripped in COMMAND_WORDS:
        return False
    if stripped.startswith("/"):
        return False
    if stripped.isdigit():
        return False
    return True


STAGES: dict[str, dict] = {
    "greeting": {
        "description": "Customer just started",
        "goal": "Warm welcome + understand need",
        "next": "product_inquiry",
    },
    "product_inquiry": {
        "description": "Customer asking about products",
        "goal": "Show relevant products, build interest",
        "next": "objection_handling OR qualification",
    },
    "qualification": {
        "description": "Customer showing interest",
        "goal": "Understand budget, quantity, timeline",
        "next": "offer_making",
    },
    "objection_handling": {
        "description": "Customer has concerns",
        "goal": "Address concern, rebuild interest",
        "next": "qualification OR offer_making",
    },
    "offer_making": {
        "description": "Ready to buy",
        "goal": "Present clear offer, create urgency",
        "next": "order_collection",
    },
    "order_collection": {
        "description": "Customer agreed to buy",
        "goal": "Collect name, address, quantity",
        "next": "payment",
    },
    "payment": {
        "description": "Order details collected",
        "goal": "Send payment QR, confirm order",
        "next": "completed",
    },
    "completed": {
        "description": "Order placed",
        "goal": "Thank customer, ask for review",
        "next": None,
    },
    "off_topic": {
        "description": "Customer went off topic",
        "goal": "Politely redirect to business",
        "next": "product_inquiry",
    },
}

_VALID_STAGES = set(STAGES.keys())

# Words that mean "I've paid" — when detected in payment stage, move to completed.
PAYMENT_CONFIRMATION_WORDS = {
    "paid", "done", "sent", "transferred", "ho gaya", "kar diya",
    "completed", "finished", "payment done", "bhej diya", "upi kiya",
    "gpay kiya", "phonepay kiya", "payment kiya", "kiya", "payment ho gaya",
    "bhugtan", "transfer", "payment kar diya",
    # Bare app names used as "I paid via X"
    "gpay", "phonepay", "paytm", "phonepe",
    # Additional confirmation phrases
    "payment kiya", "kar diya", "ho gaya", "bhej diya",
    "upi kiya", "payment ho gaya", "payment kar diya",
}

_STAGE_DETECT_PROMPT = """You are a sales conversation stage classifier.

Given the conversation history and the latest customer message, return EXACTLY ONE of these stage names:
greeting, product_inquiry, qualification, objection_handling, offer_making, order_collection, payment, completed, off_topic

Stage definitions:
- greeting: First message, no clear intent yet
- product_inquiry: Asking about products, prices, availability
- qualification: Showing interest, asking about quantity/timeline/specs
- objection_handling: Expressing hesitation, price concern, "sochna hai", "mehnga hai"
- offer_making: Showing clear buying intent, ready to decide
- order_collection: Actively providing order details (name, address, quantity)
- payment: Order confirmed, arranging payment method
- completed: Payment done, order placed
- off_topic: Asking about something unrelated to the business

Respond with ONLY the stage name. Nothing else."""


# Cues in the AGENT's last message that mean it just asked for an
# order-collection detail (quantity/name/address/payment/summary). If the
# customer is replying to one of these, the conversation is — by definition —
# in order_collection, regardless of what the AI classifier guesses. This
# stops the strict order_collection sequence from being skipped just because
# the LLM-based stage classifier mislabels the turn (e.g. as "product_inquiry").
_ORDER_FOLLOWUP_CUES = (
    "kitne piece", "kitne pieces", "ketla piece", "ketla pieces",
    "how many piece", "how many pieces", "quantity",
    "aapka naam", "aapka naam kya", "tamaru naam", "your name", "naam kya hai",
    "delivery address", "address kya hai", "address su che", "your address",
    "upi ya cod", "upi or cod", "ya cod", "ke cod", "cash on delivery",
    "payment kaisa", "payment kai rite", "how would you like to pay",
    "order summary", "confirm karein", "confirm?", "sab sahi hai",
)


def _is_order_followup(conversation_history: list[dict]) -> bool:
    """
    True if the agent's most recent message asked for an order-collection
    detail (quantity, name, address, payment, or showed the order summary),
    meaning the customer's reply is necessarily part of order collection.

    Args:
        conversation_history: List of {'role': str, 'content': str} dicts,
                              most recent last.

    Returns:
        True if the last assistant turn looks like an order-collection prompt.
    """
    for m in reversed(conversation_history):
        if m.get("role") in ("model", "assistant"):
            text = (m.get("content") or "").lower()
            return any(cue in text for cue in _ORDER_FOLLOWUP_CUES)
    return False


def detect_stage(
    conversation_history: list[dict],
    latest_message: str,
    stored_stage: str | None = None,
) -> str:
    """
    Classify the current conversation stage using keyword heuristics.

    Deterministic, zero-latency — no AI call. Runs an order-followup check
    first (if the agent's last message asked for name/address/quantity the
    stage is forced to 'order_collection'), then falls back to keyword matching
    on the latest customer message.

    Stage locking: once in 'payment' or 'completed', the stage never moves
    backward. Payment confirmation words ("paid", "ho gaya" etc.) in payment
    stage immediately advance to 'completed'.

    Args:
        conversation_history: List of {'role': str, 'content': str} dicts.
        latest_message:       The customer's most recent message text.
        stored_stage:         The stage persisted on the Conversation row from
                              the previous turn — used for forward-only locking.

    Returns:
        One of the STAGES keys.
    """
    # Stage is irreversible once completed.
    if stored_stage == "completed":
        return "completed"

    # In payment stage: check for confirmation words first.
    if stored_stage == "payment":
        msg_lower = latest_message.lower()
        if any(word in msg_lower for word in PAYMENT_CONFIRMATION_WORDS):
            return "completed"
        # Stay in payment unless customer explicitly changes stage (never go back).
        return "payment"

    if _is_order_followup(conversation_history):
        return "order_collection"

    msg = latest_message.lower()

    # Keyword-based stage detection — no AI call, zero latency.
    _ORDER_KW = [
        "order", "buy", "purchase", "confirm", "book", "address", "payment",
        "pay", "upi", "cod", "deliver", "delivery", "chahiye", "lena hai",
        "kharidna", "bhejo", "pakka", "le loon", "abhi lena",
    ]
    _OBJECTION_KW = [
        "sochna", "mehnga", "expensive", "costly", "budget", "nahi", "nope",
        "no thanks", "later", "kal", "baad mein", "not now", "abhi nahi",
        "nhi", "think",
    ]
    _PAYMENT_KW = [
        "upi", "gpay", "phonepe", "paytm", "qr", "payment done", "paid",
        "transferred", "bhej diya", "pay kar diya",
    ]
    _OFF_TOPIC_KW = [
        "cricket", "weather", "politics", "news", "joke", "recipe",
        "movie", "song", "ipl", "modi", "bjp", "congress", "election",
        "doctor", "hospital", "school", "covid",
    ]
    _QUALIFICATION_KW = [
        "how many", "kitne", "quantity", "bulk", "wholesale", "reseller",
        "timeline", "when", "kab", "spec", "size", "colour", "color",
    ]

    if any(kw in msg for kw in _PAYMENT_KW):
        return "payment"
    if any(kw in msg for kw in _ORDER_KW):
        return "offer_making"
    if any(kw in msg for kw in _OBJECTION_KW):
        return "objection_handling"
    if any(kw in msg for kw in _QUALIFICATION_KW):
        return "qualification"
    if any(kw in msg for kw in _OFF_TOPIC_KW):
        return "off_topic"

    # No history → greeting; otherwise assume product inquiry
    if not conversation_history:
        return "greeting"
    return "product_inquiry"


def extract_order_field(
    conversation,
    user_text: str,
    variant_info: dict | None = None,
) -> tuple[str, object] | None:
    """
    Heuristically determine which order-collection field the customer's latest
    reply most likely answers, by matching TEXT SHAPE against whichever fields
    are still missing on the conversation row.

    When variant_info is provided (from catalogue_service.get_product_variant_info),
    color and size extraction is attempted before name/address so the variant
    questions are resolved in order.

    Field types are checked independently (not strictly gated) so a single missed
    turn never permanently blocks progress. Each field is only written once.

    Args:
        conversation: Conversation ORM instance.
        user_text:    The customer's latest message text.
        variant_info: Optional dict from get_product_variant_info with keys
                      has_variants, needs_color, needs_size, available_colors,
                      available_sizes. Pass None for non-variant products.

    Returns:
        (field_name, value) tuple for conversation_service.update_order_field,
        or None if nothing could be confidently extracted.
    """
    text = user_text.strip()
    if not text:
        return None

    text_lower = text.lower()

    digit_match = re.fullmatch(r"\d{1,3}", text) or re.search(r"\b(\d{1,3})\b", text)
    words = text.split()
    looks_like_name = (
        not digit_match
        and 1 <= len(words) <= 4
        and all(w.replace(".", "").isalpha() for w in words)
        and is_valid_name(text)
    )

    # A bare number → quantity (only while quantity is still unknown)
    if conversation.pending_order_quantity is None and digit_match:
        return ("pending_order_quantity", int(digit_match.group(0)))

    # Variant extraction — only when product has variants
    vi = variant_info or {}
    if vi.get("has_variants"):
        # Color extraction — match against available colors (case-insensitive)
        if vi.get("needs_color") and not getattr(conversation, "selected_color", None):
            for color in vi.get("available_colors", []):
                if color.lower() in text_lower:
                    return ("selected_color", color)

        # Size extraction — match against available sizes (case-insensitive)
        if vi.get("needs_size") and not getattr(conversation, "selected_size", None):
            size_map = {s.lower(): s for s in vi.get("available_sizes", [])}
            # Also handle common abbreviations
            for token in words:
                canonical = size_map.get(token.lower())
                if canonical:
                    return ("selected_size", canonical)

    # A short alphabetic phrase → name (only while name is still unknown)
    # Double-check with is_valid_name to guard against command words slipping through.
    if not conversation.customer_name and looks_like_name and is_valid_name(text):
        return ("customer_name", text.title())

    # Anything longer and not name-shaped, once we have a name → address
    if not conversation.delivery_address and conversation.customer_name and len(text) >= 4:
        return ("delivery_address", text)

    return None


def _build_collected_note(collected: dict | None) -> str:
    """
    Build a short note summarising which order_collection fields are already
    saved on the conversation, so the agent does not re-ask them.

    Args:
        collected: Dict with optional keys 'quantity', 'color', 'size',
                   'name', 'address'.

    Returns:
        Multi-line note string, or "" if nothing has been collected yet.
    """
    if not collected:
        return ""
    lines = []
    if collected.get("quantity") is not None:
        lines.append(f"- Quantity: {collected['quantity']} pieces ✅ (already given — do NOT ask again)")
    else:
        lines.append(
            "⚠️  QUANTITY: NOT YET COLLECTED — your VERY NEXT question MUST be "
            "'How many pieces would you like?' NEVER assume, guess, or infer a number. "
            "quantity_collected is ONLY TRUE when the customer explicitly says a number in their message."
        )
    if collected.get("color"):
        lines.append(f"- Color: {collected['color']} ✅ (already given — do NOT ask again)")
    if collected.get("size"):
        lines.append(f"- Size: {collected['size']} ✅ (already given — do NOT ask again)")
    if collected.get("name"):
        lines.append(f"- Name: {collected['name']} ✅ (already given — do NOT ask again)")
    if collected.get("address"):
        lines.append(f"- Address: {collected['address']} ✅ (already given — do NOT ask again)")
    return (
        "\nORDER COLLECTION STATUS (read before asking anything):\n" + "\n".join(lines) + "\n"
    )


def get_stage_instructions(
    stage: str,
    business_type: str,
    products: list,
    collected: dict | None = None,
    accepts_cod: bool = False,
    upi_id: str | None = None,
    order_total: float = 0,
    order_product_name: str = "",
    order_qty: int = 0,
    variant_info: dict | None = None,
) -> str:
    """
    Return focused, stage-specific instructions to embed in the system prompt.

    Args:
        stage:         Current conversation stage key.
        business_type: Client's business type (e.g. 'textile').
        products:      List of product dicts for context.
        collected:     Optional dict with 'quantity', 'name', 'address' keys
                       already saved on the conversation (order_collection only).
        accepts_cod:   Whether this client accepts Cash on Delivery.
        upi_id:        Client's UPI handle to show in payment instructions.

    Returns:
        Multi-line instruction string.
    """
    if stage == "greeting":
        return """GREETING STAGE INSTRUCTIONS:
- Give warm welcome with business name
- Ask ONE open question to understand need
- Example: "Namaste! Aaj aap kya dekhna chahenge? 🙏"
- DO NOT list all products immediately
- Build rapport first"""

    if stage == "product_inquiry":
        return """PRODUCT INQUIRY STAGE INSTRUCTIONS:
- Show MAX 2-3 most relevant products
- For each product mention:
  → Name + key feature
  → Price (clearly with ₹)
  → EXACT stock count from the catalogue above (never invent or round it)
- Create mild urgency only if the catalogue actually shows low stock (≤5 pcs):
  English: "Only X pieces left — order now to secure yours!"
  Hindi/Hinglish: "Sirf X pieces bacha hai — jaldi order karein!"
  Gujarati: "Fakt X pieces bachi chhe — haji order karo!"
  ⚠️ Use ONLY the phrase matching the customer's language. NEVER use the Hindi phrase in an English reply.
- End with ONE closing question: "Would you like to place an order?" (or language equivalent)
- NEVER ask about color, size, or quantity here.
  Color/size/quantity are collected in order_collection ONLY.
  Mentioning them here confuses the sequence.
- NEVER dump entire catalogue"""

    if stage == "qualification":
        return """QUALIFICATION STAGE INSTRUCTIONS:
- Ask ONE qualifying question at a time:
  → Quantity: "Kitne pieces chahiye?"
  → Timeline: "Kab tak chahiye?"
  → Purpose: "Wedding ke liye hai ya daily wear?"
- Use answers to recommend specific product
- Confirm budget indirectly: "Aapka budget roughly kitne ka hai?"
  Only if price hesitation detected

HANDLING "yes" / "haan" / "okay" RESPONSES:
If customer replies with only "yes", "haan", "okay", or similar short agreement:
  → Confirm EXACTLY what they agreed to before proceeding.
  → Show a brief confirmation list, e.g.:
    "Great! So you want:
    1. Kurti Best — ₹1,030
    2. Banarasi Silk Saree — ₹2,450
    Total: ₹3,480
    Shall I proceed with this order?"
  → NEVER assume and jump straight to asking name/address."""

    if stage == "objection_handling":
        return """OBJECTION HANDLING INSTRUCTIONS:
CRITICAL: Reply in the SAME LANGUAGE as the customer (see LANGUAGE RULE at top of prompt).

If customer says price is too high:
→ English: "Quality comes with a fair price. We also have more affordable options — shall I show you?"
→ Hindi: "Haan ji, quality ke saath price thoda upar hota hai. Koi aur option dekhein?"

If customer says they'll think about it:
→ English: "Of course! Just to let you know, stock is limited. Shall I hold a piece for you?"
→ Hindi: "Bilkul ji! Ye piece limited stock mein hai. Aaj confirm karein toh hold kar lete hain."

If customer says they'll do it later:
→ English: "No problem! May I ask your name so I can follow up?"
→ Hindi: "Zaroor ji. Aapka naam kya hai toh kal remind kar sakta hoon?"

If customer says cheaper elsewhere:
→ English: "We understand. Our quality is guaranteed and delivery is fast. Give us a try?"
→ Hindi: "Hum samajhte hain ji. Quality guarantee hai aur delivery fast hai. Ek baar try karein?" """

    if stage == "offer_making":
        cod_line = (
            '- If customer hesitates on price: "COD bhi available hai — delivery pe payment"\n'
            if accepts_cod
            else "- NEVER mention COD. This business is UPI-only.\n"
        )
        return f"""OFFER MAKING INSTRUCTIONS:
CRITICAL: Reply in the SAME LANGUAGE as the customer (see LANGUAGE RULE at top of prompt).

- Make a CLEAR offer for the PRODUCT — price + delivery only.
  English example: "[Product Name] — ₹[Price]. Delivery in 3-5 days. Shall I place an order?"
  Hindi example: "[Product Name] — ₹[Price]. 3-5 din mein delivery. Order karna chahenge?"
- ⚠️ NEVER mention a quantity, color, or size in the offer.
  These are all collected in order_collection, never here.
- Create urgency only when true.
  English: "Order today for quick dispatch."
  Hindi: "Aaj order karein toh jaldi dispatch ho jayega."
{cod_line}- NEVER be pushy — be helpful
- If customer agrees / shows buying intent → move straight into order_collection
  and ask "How many pieces would you like?" as your very next message.
  Do NOT skip to color/size/name/address/payment.
- If rejected: offer alternative product"""

    if stage == "order_collection":
        collected_note = _build_collected_note(collected)
        vi = variant_info or {}
        has_variants = vi.get("has_variants", False)
        needs_color = vi.get("needs_color", False)
        needs_size = vi.get("needs_size", False)
        avail_colors = vi.get("available_colors", [])
        avail_sizes = vi.get("available_sizes", [])

        # Build variant steps only when product actually has them
        if has_variants and needs_color:
            color_list = ", ".join(avail_colors) if avail_colors else "see catalogue"
            variant_step_color = f"""
STEP V1 — COLOR (ask this AFTER quantity, if color is not yet collected):
  English:  "Which color would you like? Available: {color_list}"
  Hindi:    "Kaunsa color chahiye? Available: {color_list}"
  Gujarati: "Kayo color joiye? Available: {color_list}"
  NEVER suggest colors not in this list: {color_list}
  If customer picks an out-of-stock color, tell them and repeat the available list.
"""
        else:
            variant_step_color = "  (This product has no color variants — NEVER ask about color)\n"

        if has_variants and needs_size:
            size_list = ", ".join(avail_sizes) if avail_sizes else "see catalogue"
            variant_step_size = f"""
STEP V2 — SIZE (ask this AFTER color, if size is not yet collected):
  English:  "Which size? Available: {size_list}"
  Hindi:    "Kaunsa size chahiye? Available: {size_list}"
  Gujarati: "Kayu size joiye? Available: {size_list}"
  NEVER suggest sizes not in this list: {size_list}
  If customer picks an out-of-stock size, tell them and repeat the available list.
"""
        else:
            variant_step_size = "  (This product has no size variants — NEVER ask about size)\n"

        # Build the sequence description
        if has_variants and needs_color and needs_size:
            sequence_line = "STEP 1 → QUANTITY → STEP V1 → COLOR → STEP V2 → SIZE → STEP 2 → NAME → STEP 3 → ADDRESS → SUMMARY → CONFIRM"
        elif has_variants and needs_color:
            sequence_line = "STEP 1 → QUANTITY → STEP V1 → COLOR → STEP 2 → NAME → STEP 3 → ADDRESS → SUMMARY → CONFIRM"
        elif has_variants and needs_size:
            sequence_line = "STEP 1 → QUANTITY → STEP V2 → SIZE → STEP 2 → NAME → STEP 3 → ADDRESS → SUMMARY → CONFIRM"
        else:
            sequence_line = "STEP 1 → QUANTITY → STEP 2 → NAME → STEP 3 → ADDRESS → SUMMARY → CONFIRM"

        # Summary format depends on whether product has variants
        if has_variants:
            color_line = "  🎨 Color: [Color]\n" if needs_color else ""
            size_line = "  📐 Size: [Size]\n" if needs_size else ""
            summary_format = f"""SUMMARY FORMAT (with variants):
  ✅ Order Summary:
  ━━━━━━━━━━━━━━━━
  📦 [Product Name]
{color_line}{size_line}  🔢 Qty: [Qty] = ₹[Amount]
  👤 [Name]
  📍 [Address]
  ━━━━━━━━━━━━━━━━
  Confirm? (yes/haan/ha)"""
        else:
            summary_format = """SUMMARY FORMAT (no variants):
  ✅ Order Summary:
  ━━━━━━━━━━━━━━━━
  📦 [Product Name] × [Qty] = ₹[Amount]
  👤 [Name]
  📍 [Address]
  ━━━━━━━━━━━━━━━━
  Confirm? (yes/haan/ha)"""

        return f"""ORDER COLLECTION INSTRUCTIONS:

⚠️ MANDATORY SEQUENCE — MEMORISE THIS AND NEVER DEVIATE:
  {sequence_line}

VARIANT RULE — READ FIRST:
  has_variants = {has_variants}
  needs_color  = {needs_color}   available colors: {avail_colors or 'none'}
  needs_size   = {needs_size}    available sizes:  {avail_sizes or 'none'}
  → If has_variants is False: NEVER ask color or size. Jump straight to quantity.
  → If has_variants is True: ask only the variant questions where needed=True.

Check the ALREADY COLLECTED note below before asking anything.
Only ask the FIRST item in the sequence that is still missing.

╔══════════════════════════════════════════════════════════╗
║  STEP 1 IS ALWAYS QUANTITY — ASK THIS FIRST, NO MATTER  ║
║  WHAT. NEVER ASK COLOR OR SIZE BEFORE QUANTITY.          ║
╚══════════════════════════════════════════════════════════╝

STEP 1 — QUANTITY (ask this if quantity is NOT yet collected):
  English:  "How many pieces would you like?"
  Hindi:    "Kitne pieces chahiye?"
  Gujarati: "Ketla pieces joiye?"
  NEVER assume quantity = 1. NEVER say "I can offer you 1 piece".
  ⚠️ DO NOT ask color or size until quantity is collected.
{variant_step_color}{variant_step_size}
STEP 2 — NAME (ask this after quantity+variants, if name is NOT yet collected):
  English:  "May I have your name please?"
  Hindi:    "Aapka naam kya hai?"
  Gujarati: "Tamaru naam shu chhe?"
  ⚠️ NAME MUST come BEFORE ADDRESS. Never ask address before name.
  ⚠️ NAME VALIDATION — A valid customer name:
    - Is at least 2 characters
    - Is NOT a command word: reset, quit, exit, demo, sales, help, start, stop, test, image
    - Does NOT start with "/"
    - Is NOT purely numeric
    - Is NOT a color, size, or payment keyword
  If the customer types a command word (e.g. "reset") when you asked for their name:
    → Do NOT save it as a name. Ask again: "Sorry, could you share your name please?"

STEP 3 — ADDRESS (ask this if name IS collected but address is NOT):
  English:  "What is your delivery address?"
  Hindi:    "Delivery address kya hai?"
  Gujarati: "Delivery address shu chhe?"

ORDER SUMMARY — show once all required fields are collected:
  ⚠️ CRITICAL: NEVER write ₹[Amount] or ₹[amount] — use the REAL calculated amount.
  The CURRENT ORDER section above shows the exact ₹ total — use that number.

{summary_format}

CONFIRMATION — only after customer says yes/haan/ha to summary.

FORBIDDEN PHRASES — NEVER say any of these:
  ✗ "I can offer you 1 piece" / "ek piece de sakta hoon"
  ✗ Asking two questions in the same message
  ✗ Asking address before name
  ✗ Skipping the order summary before confirmation
  ✗ Saying "our team will call you" or "visit our website"
  ✗ Asking color/size when has_variants is False
  ✗ Mentioning colors/sizes not in the available lists above
{collected_note}
RULES:
- ONE question per message only. NEVER ask two things at once.
- Ask in the EXACT sequence above — do not skip or reorder.

QUANTITY VALIDATION:
- If customer requests more than available stock:
  → Tell them the maximum available and ask if they want that quantity.
  → NEVER confirm an impossible quantity."""

    if stage == "payment":
        if upi_id:
            upi_line = upi_id
            upi_pending = f"UPI ID: {upi_id}"
        else:
            upi_line = "(UPI ID not configured — tell customer to contact us)"
            upi_pending = "(UPI ID not configured — tell customer to contact us)"

        amount_display = f"₹{int(order_total):,}" if order_total > 0 else "₹[CHECK ORDER SUMMARY ABOVE FOR AMOUNT]"
        cod_accept_note = (
            "This business accepts COD — confirm COD order immediately when chosen."
            if accepts_cod
            else "This business does NOT accept COD. If customer asks for COD, say: 'We only accept UPI payments. Please pay via GPay, PhonePe, or Paytm.'"
        )

        return f"""PAYMENT STAGE INSTRUCTIONS:

╔══════════════════════════════════════════════════════════╗
║  AMOUNT TO COLLECT: {amount_display:<40}║
║  UPI ID: {upi_line:<51}║
╚══════════════════════════════════════════════════════════╝

CRITICAL: NEVER write ₹[amount] or ₹[Amount] — that is WRONG.
Always use the real amount: {amount_display}

CRITICAL: Do NOT ask "Would you like to proceed?" more than once.
Once in payment stage → share UPI ID immediately, do NOT ask again.

DEFAULT ACTION (UPI — most common):
→ Send payment instructions RIGHT NOW without waiting for customer to choose:
  Show order summary first, then:
  "Please pay {amount_display} via UPI:
  UPI ID: {upi_line}
  Send via GPay, PhonePe, or Paytm.
  Reply PAID when done. ✅"

{cod_accept_note}

When customer says PAID / done / sent / transferred / ho gaya:
→ Confirm immediately: "Payment received! ✅ Order confirmed. Delivery in 3-5 days. Thank you!"
→ Stage moves to completed.

PENDING PAYMENT RULE:
If customer asks ANYTHING off-topic while UPI payment is pending:
→ Answer in ONE sentence, then IMMEDIATELY redirect:
  "[Answer]. Please complete payment first — UPI ID: {upi_line} ({amount_display})"
→ Do NOT show new products until payment is confirmed.

NEVER loop on payment question.
NEVER say "Our team will share UPI ID shortly" — share it NOW.
NEVER say ₹[amount] — use {amount_display}."""

    if stage == "completed":
        return """COMPLETED STAGE INSTRUCTIONS:
ORDER IS ALREADY PLACED AND CONFIRMED.
- Thank customer warmly and confirm delivery in 3-5 BUSINESS DAYS (never say "7 days").
- DO NOT try to sell more products unless customer specifically asks.
- DO NOT push upsell.

If customer asks a question after order confirmation:
  → Answer briefly and remind them their order is confirmed.
  → English: "Your order is confirmed! ✅ Delivery in 3-5 business days. Anything else I can help with?"
  → Hindi/Hinglish: "Aapka order confirm ho gaya! ✅ 3-5 din mein delivery. Koi aur help chahiye?"
  → NEVER say "7 days". Always say "3-5 business days".

If customer asks about delivery status:
  → "Your order is confirmed. We'll update you when dispatched."

If customer asks about a DIFFERENT product after ordering:
  → Acknowledge their confirmed order first, then naturally help with the new inquiry.
  → English example: "Your [confirmed product] order is confirmed ✅ Looking for something else? [New Product] is ₹[price] — want to order?"
  → DO NOT say "You've ordered [product] earlier" in a confusing way. Be natural and helpful.

If customer says goodbye or they don't want anything:
  → Be warm, never pushy
  → Share the catalogue link (available in rule #14 above)"""

    if stage == "off_topic":
        return """OFF TOPIC INSTRUCTIONS:
Customer asked something unrelated to business. Politely redirect:

"Ye toh main nahi bata sakta, lekin [Business] ke baare mein koi bhi sawal ho toh zaroor poochhen! 😊
Kya main aapko koi product dikhaaun?"

Redirect examples:
- General knowledge → redirect to products
- News/politics → redirect to products
- Personal advice → redirect to products
- Competitor info → "Hum sirf apne products ke baare mein baat kar sakte hain"

GOODBYE / NOT INTERESTED HANDLING:
If customer says "no thanks", "not interested", "bye", "goodbye", "later", "theek hai", "nahi chahiye":
  → Be warm and understanding, never pushy
  → Share the catalogue link (see rule #14 in strict rules)
  → Leave the door open for future
  → English example: "No problem! Feel free to browse our collection anytime. We're here whenever you need us 😊"
  → Hinglish example: "Bilkul, koi baat nahi! Jab bhi chahiye, hum yahan hain 😊" """

    return ""
