"""
Characterization test for the WhatsApp adapter's pid-missing degradation path.

app/routers/_whatsapp_adapter.py:send_pipeline_result() only sends interactive
buttons/list_options when a phone_number_id (`pid`) is available — `if
result.buttons and pid:` / `if result.list_options and pid:`. When pid is
falsy, both gates fail through to the final plain-text branch, so the
customer gets a normal text message instead of the interactive prompt.

This equivalence (mirrors the original inline webhook.py
`elif button_type == X and pid` behavior) was previously only verified by
reading the code — no test exercised pid=None. These are pure unit tests
against the adapter directly (no DB/HTTP needed): send_button_message and
send_list_message must never be called when pid is falsy, and
send_text_message must receive the same body text that would have been the
interactive prompt's body.
"""

from __future__ import annotations

import pytest

from app.routers._whatsapp_adapter import send_pipeline_result
from app.services.order_pipeline import ButtonSpec, ListOptionSpec, PipelineResult


class _FakeConv:
    id = 1


async def _fail_if_called(*args, **kwargs):
    raise AssertionError("interactive send must not be called when pid is missing")


@pytest.mark.asyncio
async def test_buttons_degrade_to_text_when_pid_missing(monkeypatch):
    sent_text: list[tuple[str, str]] = []

    async def _capture_text(to_phone_number, message_text):
        sent_text.append((to_phone_number, message_text))

    monkeypatch.setattr("app.services.whatsapp_service._raw_send_text_message", _capture_text)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_button_message", _fail_if_called)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_list_message", _fail_if_called)

    result = PipelineResult(
        text="Pick a payment method",
        buttons=[ButtonSpec(id="upi", title="💳 Pay via UPI")],
    )
    await send_pipeline_result(
        result, db=None, conv=_FakeConv(), sender_phone="911234567890", pid=None,
    )

    assert sent_text == [("911234567890", "Pick a payment method")]


@pytest.mark.asyncio
async def test_list_options_degrade_to_text_when_pid_missing(monkeypatch):
    sent_text: list[tuple[str, str]] = []

    async def _capture_text(to_phone_number, message_text):
        sent_text.append((to_phone_number, message_text))

    monkeypatch.setattr("app.services.whatsapp_service._raw_send_text_message", _capture_text)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_button_message", _fail_if_called)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_list_message", _fail_if_called)

    result = PipelineResult(
        text="Choose an option",
        list_options=[ListOptionSpec(id="SR001", title="Product One", description="₹999")],
        list_header="Choose an option",
        list_button_text="View options",
    )
    await send_pipeline_result(
        result, db=None, conv=_FakeConv(), sender_phone="911234567890", pid=None,
    )

    assert sent_text == [("911234567890", "Choose an option")]


@pytest.mark.asyncio
async def test_buttons_sent_normally_when_pid_present(monkeypatch):
    """Sanity check: the same PipelineResult DOES send interactive buttons when pid is present."""
    button_calls = []

    async def _capture_button(to_phone_number, body_text, buttons, phone_number_id=None):
        button_calls.append((to_phone_number, body_text, buttons, phone_number_id))
        return True

    async def _fail_text(*args, **kwargs):
        raise AssertionError("plain text should not be sent when buttons send succeeds")

    monkeypatch.setattr("app.services.whatsapp_service._raw_send_button_message", _capture_button)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_text_message", _fail_text)

    result = PipelineResult(
        text="Pick a payment method",
        buttons=[ButtonSpec(id="upi", title="💳 Pay via UPI")],
    )
    await send_pipeline_result(
        result, db=None, conv=_FakeConv(), sender_phone="911234567890", pid="111000111",
    )

    assert len(button_calls) == 1
    assert button_calls[0][0] == "911234567890"
    assert button_calls[0][1] == "Pick a payment method"
    assert button_calls[0][3] == "111000111"
