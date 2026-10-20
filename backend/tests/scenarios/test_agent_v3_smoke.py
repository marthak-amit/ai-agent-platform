"""Smoke: the v3 path answers a greeting (fast path, no planner) and a scripted browse."""

from __future__ import annotations

import pytest

from tests.scenarios.harness import Customer, ScriptedPlanner, capture_structured, joined, seed_shop


@pytest.fixture
def planner(monkeypatch):
    """Scripted planner installed as agent_planner.call_model."""
    fake = ScriptedPlanner()
    monkeypatch.setattr("app.services.agent_planner.call_model", fake)
    return fake


async def test_greeting_is_a_fast_path_and_browse_lists_real_products(replay_http, replay_session, planner, monkeypatch):
    """'hi' → greeting without the planner; 'show me products' (scripted browse_catalog) → real products + catalogue link."""
    sent = capture_structured(monkeypatch)
    client, phone, pnid = await seed_shop(replay_session)
    me = Customer(replay_http, replay_session, sent, client, phone, pnid)

    out = await me.say("hi")
    assert "Welcome to Riya Boutique" in joined(out) and not planner.calls

    planner.script([{"actions": [{"type": "browse_catalog", "category": None}], "reply_intent": "none"}])
    out = await me.say("show me what you have")
    text = joined(out)
    assert "Cotton Kurti" in text and "₹499" in text and "riya-01" in text, text
    assert "Silk Scarf" not in text            # sold out → not listed
