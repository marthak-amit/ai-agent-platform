"""The mid-order greeting-resume templates exist and render in every supported language."""

from __future__ import annotations

import pytest

from app.services.language_templates import TEMPLATES, get_template


@pytest.mark.parametrize("lang", sorted(TEMPLATES))
def test_greeting_resume_slot_renders_product_and_question(lang):
    """Each language has greeting_resume_slot with both placeholders filled."""
    out = get_template(lang, "greeting_resume_slot", product="Silk Saree", question="Colour? Red / Blue")
    assert "Silk Saree" in out and "Colour? Red / Blue" in out and "{" not in out


@pytest.mark.parametrize("lang", sorted(TEMPLATES))
def test_greeting_resume_product_renders_product(lang):
    """Each language has greeting_resume_product (used when no slot is pending)."""
    out = get_template(lang, "greeting_resume_product", product="Silk Saree")
    assert "Silk Saree" in out and "{" not in out


def test_non_english_resume_templates_are_actually_translated():
    """No non-English dict silently falls back to the English wording."""
    english = get_template("english", "greeting_resume_slot", product="P", question="Q")
    for lang in TEMPLATES:
        if lang != "english":
            assert get_template(lang, "greeting_resume_slot", product="P", question="Q") != english
