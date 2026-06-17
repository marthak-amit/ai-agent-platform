"""
DOC B: canonical order-stage transition table.

Every (current_stage, intent) → (next_stage, action) cell is explicit.
The LLM in order stages returns only the {intent, entities} struct (DOC A);
all customer-facing replies are produced by render_order_reply() in webhook.py.

Intent labels used here match classify_user_intent() and the synthetic labels
injected by the webhook before lookup (SLOTS_DONE, AFFIRM, DENY, PAID).
"""

from __future__ import annotations

# ─── Transition table ─────────────────────────────────────────────────────────
# (current_stage, intent) → (next_stage, action)
#
# Synthetic intents injected by the webhook before lookup:
#   SLOTS_DONE  — all slots filled, _next_slot is None
#   AFFIRM      — affirmative reply in awaiting_final_confirmation
#   DENY        — negative reply in awaiting_final_confirmation
#   PAID        — payment-confirmation word in payment stage
#
# "completed" is not in the table; its action is contextual on prev-stage
# and is resolved inline in the webhook.
TRANSITION_TABLE: dict[tuple[str, str], tuple[str, str]] = {
    # ─── order_collection ─────────────────────────────────────────────────────
    ("order_collection", "ANSWER"):          ("order_collection",           "ask_slot"),
    ("order_collection", "SLOTS_DONE"):      ("awaiting_final_confirmation", "show_summary"),
    ("order_collection", "CANCEL"):          ("greeting",                   "cancel"),
    ("order_collection", "DISCOUNT_QUERY"):  ("order_collection",           "discount_reask"),
    ("order_collection", "OFF_TOPIC"):       ("order_collection",           "offtopic_reask"),
    ("order_collection", "NEW_PRODUCT"):     ("order_collection",           "ask_slot"),
    ("order_collection", "NEW_PRODUCT_OOS"): ("order_collection",           "oos_block"),
    ("order_collection", "OTHER"):           ("order_collection",           "ask_slot"),
    # ─── awaiting_final_confirmation ──────────────────────────────────────────
    # SLOTS_DONE fires when the last slot is filled in the SAME turn the stage
    # advances to awaiting_final_confirmation (e.g. "yes" fills saved address →
    # UPI auto-fills → all slots done).  Show summary ONCE; never "already confirmed".
    ("awaiting_final_confirmation", "SLOTS_DONE"): ("awaiting_final_confirmation", "show_summary"),
    ("awaiting_final_confirmation", "AFFIRM"):  ("payment",                   "show_payment"),
    ("awaiting_final_confirmation", "DENY"):    ("greeting",                   "cancel"),
    ("awaiting_final_confirmation", "CANCEL"):  ("greeting",                   "cancel"),
    ("awaiting_final_confirmation", "OTHER"):   ("awaiting_final_confirmation", "reask_confirm"),
    # ─── payment ──────────────────────────────────────────────────────────────
    ("payment", "PAID"):   ("completed", "confirm_paid_upi"),
    ("payment", "CANCEL"): ("greeting",  "cancel"),
    ("payment", "OTHER"):  ("payment",   "reask_payment"),
}

# Per-state fallback when (stage, intent) is not in the table.
_STATE_DEFAULTS: dict[str, tuple[str, str]] = {
    "order_collection":            ("order_collection",           "ask_slot"),
    "awaiting_final_confirmation": ("awaiting_final_confirmation", "reask_confirm"),
    "payment":                     ("payment",                    "reask_payment"),
    "completed":                   ("completed",                  "already_confirmed"),
}


def resolve_transition(stage: str, intent: str) -> tuple[str, str]:
    """
    Look up (stage, intent) in TRANSITION_TABLE and return (next_stage, action).

    Falls back to _STATE_DEFAULTS[stage] when no exact match is found.
    Falls back to ("greeting", "cancel") when state is unrecognised.

    Args:
        stage:  Current conversation stage.
        intent: Intent label from classify_user_intent() or a synthetic label.

    Returns:
        (next_stage, action) tuple — both are non-empty strings.
    """
    if (stage, intent) in TRANSITION_TABLE:
        return TRANSITION_TABLE[(stage, intent)]
    return _STATE_DEFAULTS.get(stage, ("greeting", "cancel"))


class RenderError(Exception):
    """
    Raised by render_order_reply() when a required DB fact is absent at render time.

    The caller MUST NOT send the reply when this is raised; log and return an error
    response instead. Bad state must be unrepresentable in customer-visible output.
    """
