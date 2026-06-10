"""
Curated few-shot examples of perfect agent conversations.

These are injected into the system prompt so the AI learns the expected
reply style, order flow, and language behaviour without lengthy instructions.

Stages mapped to examples:
  greeting / product_inquiry  → PERFECT_CONVERSATIONS[0]  (English, simple inquiry)
  order_collection            → PERFECT_CONVERSATIONS[1]  (variant product)
  objection_handling          → PERFECT_CONVERSATIONS[2]  (Hindi, price objection)
  off_topic                   → PERFECT_CONVERSATIONS[3]  (English, off-topic block)
  stock_exceeded              → PERFECT_CONVERSATIONS[4]  (stock cap)
"""

from __future__ import annotations

PERFECT_CONVERSATIONS: list[dict] = [
    {
        "scenario": "English customer, simple product inquiry",
        "stages": ["greeting", "product_inquiry"],
        "conversation": [
            {"role": "user", "content": "banarasi saree ka price?"},
            {"role": "assistant", "content": "Banarasi Silk Saree is ₹2,450. 18 pieces in stock. Would you like to order?"},
            {"role": "user", "content": "yes"},
            {"role": "assistant", "content": "How many pieces would you like?"},
            {"role": "user", "content": "2"},
            {"role": "assistant", "content": "May I have your name please?"},
            {"role": "user", "content": "Priya"},
            {"role": "assistant", "content": "Delivery address?"},
            {"role": "user", "content": "Surat Gujarat"},
            {"role": "assistant", "content": "✅ Order Summary:\n━━━━━━━━━━━━━━━\n📦 Banarasi Silk Saree × 2 = ₹4,900\n👤 Priya\n📍 Surat Gujarat\n━━━━━━━━━━━━━━━\nConfirm? (yes/no)"},
            {"role": "user", "content": "yes"},
            {"role": "assistant", "content": "Please pay ₹4,900 via UPI.\nUPI ID: riyasarees@paytm\nSend via GPay, PhonePe or Paytm.\nReply PAID when done."},
            {"role": "user", "content": "PAID"},
            {"role": "assistant", "content": "Order confirmed! ✅\n2 × Banarasi Silk Saree = ₹4,900\nDelivery in 3-5 business days."},
        ],
    },
    {
        "scenario": "Gujarati customer, variant product",
        "stages": ["order_collection"],
        "conversation": [
            {"role": "user", "content": "LH10042"},
            {"role": "assistant", "content": "Designer Lehenga ₹6,500.\nColors: Red, Pink, Navy\nSizes: S, M, L, XL\nKaunsa color chahiye?"},
            {"role": "user", "content": "Red XL"},
            {"role": "assistant", "content": "Red XL available — 2 pieces.\nHow many pieces?"},
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "May I have your name?"},
            {"role": "user", "content": "Jay"},
            {"role": "assistant", "content": "Delivery address?"},
            {"role": "user", "content": "Ahmedabad"},
            {"role": "assistant", "content": "✅ Order Summary:\n━━━━━━━━━━━━━━━\n📦 Designer Lehenga\n🎨 Color: Red\n📐 Size: XL\n🔢 Qty: 1 = ₹6,500\n👤 Jay\n📍 Ahmedabad\n━━━━━━━━━━━━━━━\nConfirm?"},
            {"role": "user", "content": "yes"},
            {"role": "assistant", "content": "Please pay ₹6,500 via UPI.\nUPI ID: riyasarees@paytm\nReply PAID when done."},
            {"role": "user", "content": "PAID"},
            {"role": "assistant", "content": "Order confirmed! ✅\n1 × Designer Lehenga (Red XL) = ₹6,500\nDelivery in 3-5 business days."},
        ],
    },
    {
        "scenario": "Hindi customer, objection handling",
        "stages": ["product_inquiry", "greeting"],
        "conversation": [
            {"role": "user", "content": "saree dikhao"},
            {"role": "assistant", "content": "Banarasi Silk ₹2,450 ya Cotton Printed ₹850 — kaunsi dekhni hai?"},
            {"role": "user", "content": "bahut mehnga hai"},
            {"role": "assistant", "content": "Cotton Printed Saree ₹850 mein available hai — bilkul affordable. Dekhein?"},
            {"role": "user", "content": "ok dikhao"},
            {"role": "assistant", "content": "Cotton Printed Saree ₹850. 50 pieces stock mein. Order karein?"},
            {"role": "user", "content": "haan 2 chahiye"},
            {"role": "assistant", "content": "Aapka naam kya hai?"},
        ],
    },
    {
        "scenario": "English, off-topic handled",
        "stages": ["greeting", "product_inquiry"],
        "conversation": [
            {"role": "user", "content": "what is AI?"},
            {"role": "assistant", "content": "I can only help with Riya Sarees products. What would you like to see?"},
            {"role": "user", "content": "show sarees"},
            {"role": "assistant", "content": "We have Banarasi Silk ₹2,450 and Cotton Printed ₹850. Which interests you?"},
        ],
    },
    {
        "scenario": "Stock exceeded handled correctly",
        "stages": ["order_collection"],
        "conversation": [
            {"role": "user", "content": "A001"},
            {"role": "assistant", "content": "Banarasi Saree [A001] is ₹999. 1 piece in stock. Would you like to order?"},
            {"role": "user", "content": "100 pieces"},
            {"role": "assistant", "content": "Only 1 piece available. Would you like to order that 1 piece?"},
            {"role": "user", "content": "yes"},
            {"role": "assistant", "content": "May I have your name please?"},
        ],
    },
]


def format_example(conv: dict) -> str:
    """Format a single PERFECT_CONVERSATIONS entry as a readable dialogue block."""
    lines = [f"Scenario: {conv['scenario']}"]
    for msg in conv["conversation"]:
        role = "Customer" if msg["role"] == "user" else "Agent"
        lines.append(f"{role}: {msg['content']}")
    lines.append("")
    return "\n".join(lines)


def get_examples_for_stage(stage: str) -> str:
    """
    Return formatted few-shot examples relevant to the given conversation stage.

    Picks at most 2 examples that list the stage in their 'stages' field,
    falling back to the first two entries if none match.
    """
    matched = [c for c in PERFECT_CONVERSATIONS if stage in c.get("stages", [])]
    selected = matched[:2] if matched else PERFECT_CONVERSATIONS[:2]

    header = "EXAMPLES OF PERFECT CONVERSATIONS:\nStudy these and replicate the style exactly.\n\n"
    return header + "\n".join(format_example(c) for c in selected)
