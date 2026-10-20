"""Per-turn TURN trace (app/services/turn_trace.py): collection, formatting, enable switch."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from app.services import turn_trace
from app.services.order_pipeline import ButtonSpec, PipelineResult


def _emit(name: str, msg: str, level: int = logging.INFO) -> None:
    """Log *msg* from an ``app.*`` logger the way the pipeline does (the step prefix is this file's module name)."""
    logging.getLogger(f"app.services.{name}").log(level, msg)


def test_clip_flattens_newlines_and_truncates():
    """Newlines become ⏎ and long text ends with an ellipsis at the limit."""
    assert turn_trace._clip("a\n\nb", 50) == "a ⏎ b"
    clipped = turn_trace._clip("x" * 500, 20)
    assert len(clipped) == 20 and clipped.endswith("…")


def test_install_is_idempotent():
    """Calling install() repeatedly attaches the collector to the app logger once."""
    turn_trace.install()
    turn_trace.install()
    assert logging.getLogger("app").handlers.count(turn_trace._handler) == 1


def test_handler_collects_only_inside_a_turn_and_skips_noise():
    """Lines are collected between start() and log_turn(); transcript / slot-debug noise and out-of-turn lines are not."""
    logging.getLogger("app").setLevel(logging.INFO)
    _emit("router_actions", "before the turn")           # no turn running -> ignored
    turn_trace.start()
    _emit("router_actions", "ROUTER conv=1 action=- fast_path=exact_sku")
    _emit("order_pipeline", "CONV-TRANSCRIPT conv=1\nUSER: hi")
    _emit("order_pipeline", "SLOT-DEBUG conv=1 stage=x")
    _emit("order_pipeline", "something broke", logging.WARNING)
    steps = turn_trace._steps.get()
    turn_trace._steps.set(None)
    assert steps == [
        "test_turn_trace: ROUTER conv=1 action=- fast_path=exact_sku",
        "WARNING! test_turn_trace: something broke",
    ]


def test_handler_caps_the_number_of_steps():
    """A runaway turn cannot grow the trace beyond MAX_STEPS."""
    logging.getLogger("app").setLevel(logging.INFO)
    turn_trace.start()
    for i in range(turn_trace.MAX_STEPS + 10):
        _emit("order_pipeline", f"step {i}")
    steps = turn_trace._steps.get()
    turn_trace._steps.set(None)
    assert len(steps) == turn_trace.MAX_STEPS


def test_start_returns_a_timestamp_and_opens_an_empty_trace():
    """start() resets the step list and hands back a perf-counter timestamp."""
    started = turn_trace.start()
    assert isinstance(started, float) and turn_trace._steps.get() == []
    turn_trace._steps.set(None)


def test_describe_reply_shows_text_buttons_images_and_flags():
    """The reply line carries the text plus buttons / list / image counts and send flags."""
    result = PipelineResult(
        text="Quantity?\nPlease", buttons=[ButtonSpec(id="a", title="Yes"), ButtonSpec(id="b", title="No")],
        images=[("u", "c")], status="render_error",
    )
    text = turn_trace.describe_reply(result)
    assert "'Quantity? ⏎ Please'" in text and "[buttons: Yes | No]" in text and "[images: 1]" in text and "[status=render_error]" in text
    assert "(no reply sent)" in turn_trace.describe_reply(PipelineResult(text=None, skip_send=True))
    assert turn_trace.describe_reply(None) == "(no result)"


def test_format_turn_block_layout():
    """USER / REPLY / WHY lines, stage transition and one bullet per step."""
    block = turn_trace.format_turn(
        60, "order_collection", "awaiting_order_switch_confirm", "PR42546", PipelineResult(text="hello"),
        ["router_actions: ROUTER fast_path=exact_sku"], 123,
    )
    lines = block.split("\n")
    assert lines[0] == "TURN conv=60 stage=order_collection→awaiting_order_switch_confirm 123ms"
    assert lines[1] == "  USER : 'PR42546'" and lines[2] == "  REPLY: 'hello'"
    assert lines[3] == "  WHY  :" and lines[4] == "    · router_actions: ROUTER fast_path=exact_sku"
    assert "(no decision lines were logged)" in turn_trace.format_turn(1, "a", "a", "x", None, [], 0)


def test_log_turn_logs_when_enabled_and_stays_silent_when_disabled(caplog):
    """log_turn prints the block (and stops collecting); TURN_TRACE_ENABLED=false prints nothing."""
    logging.getLogger("app").setLevel(logging.INFO)
    conv = SimpleNamespace(id=7, current_stage="greeting")
    with caplog.at_level(logging.INFO, logger="app.services.turn_trace"):
        started = turn_trace.start()
        _emit("order_pipeline", "ROUTE conv=7 route=TEMPLATE reason=greeting")
        turn_trace.log_turn(started, enabled=True, conv=conv, stage_before="greeting", user_text="hi", result=PipelineResult(text="Hello"))
        assert turn_trace._steps.get() is None
        assert "TURN conv=7 stage=greeting→greeting" in caplog.text and "USER : 'hi'" in caplog.text
        assert "· test_turn_trace: ROUTE conv=7 route=TEMPLATE reason=greeting" in caplog.text
        caplog.clear()
        started = turn_trace.start()
        turn_trace.log_turn(started, enabled=False, conv=conv, stage_before="greeting", user_text="hi", result=None)
        assert "TURN conv" not in caplog.text
