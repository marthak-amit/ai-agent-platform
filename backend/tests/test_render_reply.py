"""
Regression coverage for the captured wrong-product bug (conv id=52):
last_shown_sku must be updated whenever ANY product is surfaced — including
products resolved by the LLM open_browsing path, not just the deterministic
product-card path. See backend/tests/replay/golden/fixtures/bug_choli_misrouted.json
and Section 1/3 of external_data/trim_and_unify_prompt.md.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import llm_intent, render_reply


def _product(sku, name, price):
    return SimpleNamespace(sku=sku, name=name, price=price)


class TestClassifyTurn:
    @pytest.mark.asyncio
    async def test_valid_json_is_parsed_on_first_try(self):
        raw = json.dumps({
            "intent": "ANSWER",
            "sku": "PR17761",
            "slots": {"color": "Red", "size": "XL", "quantity": None},
            "question_topic": None,
        })
        with patch("app.services.gemini_service.generate_reply", AsyncMock(return_value=raw)) as mocked:
            result = await llm_intent.classify_turn("yes, red XL choli", [], "system", None, "english")
        assert mocked.await_count == 1
        assert result.parsed_ok
        assert result.sku == "PR17761"
        assert result.color == "Red"
        assert result.size == "XL"

    @pytest.mark.asyncio
    async def test_invalid_prose_never_reaches_caller_as_text(self):
        """
        The captured bug's failure mode: the model returns free prose instead
        of JSON ("I'd be happy to help... Which item are you interested in?").
        classify_turn must never hand that prose back as if it were structured
        data — it must retry once, then return a PARSE_FAILED sentinel.
        """
        prose = "I'd be happy to help you find the right product! Which item are you interested in?"
        with patch("app.services.gemini_service.generate_reply", AsyncMock(return_value=prose)) as mocked:
            result = await llm_intent.classify_turn("yes I want to buy red choli xl size", [], "system", None, "english")
        assert mocked.await_count == 2  # one retry, per the spec
        assert not result.parsed_ok
        assert result.sku is None

    @pytest.mark.asyncio
    async def test_recovers_on_retry(self):
        good = json.dumps({"intent": "ANSWER", "sku": "PR17761", "slots": {}, "question_topic": None})
        with patch(
            "app.services.gemini_service.generate_reply",
            AsyncMock(side_effect=["not json", good]),
        ):
            result = await llm_intent.classify_turn("red choli xl", [], "system", None, "english")
        assert result.parsed_ok
        assert result.sku == "PR17761"


class TestRenderOpenBrowsingReply:
    def test_renders_from_db_product_not_llm_text(self):
        """
        The product name/price in the reply must come from the DB-fetched
        product object, never from anything the model wrote — this is the
        core Section 1 guarantee.
        """
        intent_result = llm_intent.IntentResult(intent="ASK_PRODUCT", sku="PR17761")
        product = _product("PR17761", "Traditional Choli", 3999)
        variant_info = {"available_colors": ["Red"], "available_sizes": ["XL"]}
        reply = render_reply.render_open_browsing_reply(intent_result, product, variant_info, None, "Riya Sarees")
        assert "PR17761" in reply
        assert "Traditional Choli" in reply
        assert "3,999" in reply

    def test_unresolved_product_falls_back_to_clarifying_question_not_prose(self):
        intent_result = llm_intent.IntentResult(intent="PARSE_FAILED")
        reply = render_reply.render_open_browsing_reply(intent_result, None, {}, None, "Riya Sarees")
        assert "Riya Sarees" in reply
        assert "Which item" in reply

    def test_side_question_answers_from_known_facts_table_then_resumes(self):
        intent_result = llm_intent.IntentResult(intent="SIDE_QUESTION", sku="PR17761", question_topic="delivery_time")
        product = _product("PR17761", "Traditional Choli", 3999)
        reply = render_reply.render_open_browsing_reply(intent_result, product, {}, None, "Riya Sarees")
        assert "3" in reply and "business days" in reply
        assert "PR17761" in reply


class TestSetLastShownSkuIsSingleWriter:
    @pytest.mark.asyncio
    async def test_set_last_shown_sku_delegates_to_update_order_field(self):
        from app.services import conversation_service

        with patch(
            "app.services.conversation_service.update_order_field", AsyncMock()
        ) as mocked:
            await conversation_service.set_last_shown_sku(db=None, conversation_id=52, sku="PR17761")
        mocked.assert_awaited_once_with(None, 52, "last_shown_sku", "PR17761")
