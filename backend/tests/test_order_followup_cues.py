"""_is_order_followup must not fire on look-alike words inside the clarify replies."""
from app.services import conversation_flow as cf
from app.services.language_templates import TEMPLATES


def _assistant(text: str) -> list[dict]:
    """One-message history whose last turn is the assistant's."""
    return [{"role": "assistant", "content": text}]


def test_has_whole_cue_ignores_cue_glued_to_letters():
    """'ya cod' inside 'ya code' is not the UPI/COD question."""
    assert not cf._has_whole_cue("product ka naam ya code likhein", ("ya cod",))
    assert cf._has_whole_cue("payment? upi ya cod", ("ya cod",))
    assert cf._has_whole_cue("upi ya cod?", ("ya cod",))


def test_clarify_open_replies_are_not_order_followups():
    """The fixed 'what are you looking for?' reply never puts the next message into order_collection."""
    for lang, templates in TEMPLATES.items():
        text = templates.get("rt_clarify_open")
        if text:
            assert not cf._is_order_followup(_assistant(text)), lang


def test_real_order_questions_still_detected():
    """Genuine slot questions keep forcing order_collection."""
    assert cf._is_order_followup(_assistant("Kitne pieces chahiye?"))
    assert cf._is_order_followup(_assistant("Payment kaise karoge? UPI ya COD"))
    assert cf._is_order_followup(_assistant("Your name?"))
