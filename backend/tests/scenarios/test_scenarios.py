"""
YAML scenario suite for agent v3 (``tests/scenarios/*.yaml``).

Each scenario is a short conversation: customer turns (typed text or a button tap), the planner output to script for the
turn (``plan`` + ``repair`` rounds), and what must hold afterwards. The runner drives the REAL webhook → agent v3 →
Postgres; only the LLM is replaced.

    name / description
    shop:   default | upi_only | cod_only         (see harness.SHOPS)
    setup:  {orders: [{number, status, total}]}   (orders already open for this customer)
    turns:
      - user: "2 red 1 green"           # or  tap: v3_confirm
        plan:   {actions: [...], reply_intent: none}        # planner round 1 (omit for ₹0 fast paths)
        repair: [{actions: [...]}]                           # planner rounds after validation errors
        expect:
          actions_contains / actions_absent: [add_line, ...] # actions the engine executed this turn
          fast_path: sku | null                              # which ₹0 path answered (null = planner)
          planner_calls: 0                                   # LLM calls this turn (scripted mode)
          repairs: 1
          issues_contain: [QTY_MISMATCH]
          reply_contains / reply_not_contains: ["Which colour"]
          buttons_contain: [v3_confirm]
          ends_with_question: true
          stage: confirming                                  # v3 cart stage
          conv_stage: payment                                # legacy conversations.current_stage
          cart: {lines: [{sku: KU10001, color: Red, qty: 2}], customer: {name: Asha}, payment_method: COD, empty: true}
          orders: 1
          order: {status: paid, payment_method: COD, total: 998, lines: [{sku: KU10001, color: Red, qty: 2}]}
          handoff: true
          log_contains: ["tok=4000/1000/200"]                # raw AGENT trace line (tokens in/cached/out, cost)
          turn_contains: ["COST turn=", "TIME total="]       # the TURN block (cost line, timing spans)
          translate_calls: 0                                 # LLM localizer calls so far in this conversation (llm_usage, purpose translate)
        a turn may also carry   ts: before_last_list | after_last_list   (sender-side Meta timestamp relative to the last list we sent)
        and                     image: true                              (the customer sends a photo)
          images: 3                                          # product photos sent this turn

``"$line[0]"`` inside a plan is replaced by the id of the cart's first line. Set RUN_LIVE_LLM=1 to ignore the scripted plans
and let the real planner answer (measures planner quality; the 95 % gate then tolerates a few misses).
"""

from __future__ import annotations

import logging
import os
import re
import time

import pytest
import yaml

from tests.scenarios.harness import (
    SCENARIO_DIR, Customer, ProviderError, ScriptedPlanner, capture_structured, joined, orders_of, parse_agent_logs, seed_shop,
)

LIVE = os.environ.get("RUN_LIVE_LLM") == "1"
# the seeded shop's own words (product names / categories / colours / sizes): never "foreign" (see lint.py)
_SHOP_WORDS = frozenset(w for text in (
    "Cotton Kurti Lehenga Handmade Tote Bag Banarasi Silk Saree Scarf Leather Wallet Travel Mug Zari Choli Set Block Print Accessories Home Bags "
    "Red Green Blue Pink Maroon Gold Black Brown White Cream Orange One Size Riya Boutique"
).lower().split() for w in [text])
PASS_RATE_GATE = float(os.environ.get("AGENT_SCENARIO_MIN_PASS_RATE", "0.95"))
STRICT = not LIVE                      # scripted runs are deterministic: every scenario must pass
_RESULTS: dict[str, bool] = {}


def _load() -> dict[str, dict]:
    """All scenario documents, keyed by file name."""
    out = {}
    for path in sorted(SCENARIO_DIR.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        out[path.stem] = doc
    return out


SCENARIOS = _load()

# Reusable openings: a customer who builds a complete 2 × Red Cotton Kurti order up to the summary.
_ADD_RED = {"user": "I want 2 red cotton kurtis", "plan": {"actions": [{"type": "add_line", "sku": "KU10001", "variant": {"color": "Red"}, "qty": 2}], "reply_intent": "none"},
            "expect": {"cart": {"lines": [{"sku": "KU10001", "color": "Red", "qty": 2}]}, "reply_contains": ["your name"], "ends_with_question": True}}
_NAME = {"user": "Asha Patel", "plan": {"actions": [{"type": "set_customer", "name": "Asha Patel"}], "reply_intent": "none"},
         "expect": {"cart": {"customer": {"name": "Asha Patel"}}, "reply_contains": ["address"]}}
_ADDRESS = {"user": "12 MG Road, Adajan, Surat 395009",
            "plan": {"actions": [{"type": "set_customer", "address": "12 MG Road, Adajan, Surat 395009"}], "reply_intent": "none"}, "expect": {}}
MACROS: dict[str, list[dict]] = {
    # both COD and UPI offered → the payment question, answered with the COD button → summary
    "complete_cart_cod": [_ADD_RED, _NAME, {**_ADDRESS, "expect": {"reply_contains": ["How would you like to pay"], "buttons_contain": ["v3_pay_cod", "v3_pay_upi"]}},
                          {"tap": "v3_pay_cod", "expect": {"stage": "confirming", "reply_contains": ["Order summary", "Cash on Delivery", "Handmade Tote Bag (₹250)", "Total: ₹998"], "buttons_contain": ["v3_confirm", "v3_upsell_tb30001"], "reply_not_contains": ["Banarasi", "Lehenga"]}}],
    # a shop with one payment method never asks → the address answer completes the cart → summary
    "complete_cart_single_payment": [_ADD_RED, _NAME, {**_ADDRESS, "expect": {"stage": "confirming", "reply_contains": ["Order summary"]}}],
}


def _expand(turns: list[dict]) -> list[dict]:
    """Replace {macro: name} entries by the macro's turns."""
    out: list[dict] = []
    for t in turns:
        out.extend(MACROS[t["macro"]] if "macro" in t else [t])
    return out


def _subst(obj, line_ids: list[str]):
    """Replace "$line[N]" with the N-th cart line id."""
    if isinstance(obj, str) and obj.startswith("$line["):
        idx = int(obj[len("$line["):-1])
        return line_ids[idx] if idx < len(line_ids) else "no-such-line"
    if isinstance(obj, dict):
        return {k: _subst(v, line_ids) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_subst(v, line_ids) for v in obj]
    return obj


def _line_matches(actual: dict, expected: dict) -> bool:
    """Every key of `expected` equals the actual line's value (None matches a missing value)."""
    return all((actual.get(k) is None and v is None) or str(actual.get(k)).lower() == str(v).lower() for k, v in expected.items())


def _match_lines(actual: list[dict], expected: list[dict]) -> bool:
    """Same number of lines and a one-to-one subset match (order-independent)."""
    if len(actual) != len(expected):
        return False
    pool = list(actual)
    for exp in expected:
        hit = next((a for a in pool if _line_matches(a, exp)), None)
        if hit is None:
            return False
        pool.remove(hit)
    return True


async def _seed_orders(session, client, phone, specs: list[dict]) -> None:
    """Orders that are already open for this customer."""
    from app.models.order import Order
    from app.models.order_line_item import OrderLineItem

    for spec in specs:
        order = Order(
            order_number=spec["number"], client_id=client.id, customer_name="Asha", customer_phone=phone,
            delivery_address="12 MG Road, Surat 395001", product_name=spec.get("product", "Cotton Kurti"),
            product_sku=spec.get("sku", "KU10001"), quantity=spec.get("qty", 1), unit_price=spec.get("total", 499.0),
            total_amount=spec.get("total", 499.0), payment_method=spec.get("payment_method", "UPI"),
            status=spec["status"], mobile_number=phone, **({"is_sandbox": True} if spec.get("sandbox") else {}),
        )
        session.add(order)
        await session.flush()
        session.add(OrderLineItem(
            order_id=order.id, product_name=order.product_name, product_sku=order.product_sku, quantity=order.quantity,
            unit_price=order.unit_price, subtotal=order.total_amount, line_number=1,
        ))
    await session.commit()


async def _check(turn_no: int, turn: dict, me: Customer, outs, logs: list[dict], planner: ScriptedPlanner, calls_before: int, session, trace: str = "") -> list[str]:
    """Evaluate one turn's expectations; returns human-readable failures."""
    exp = turn.get("expect") or {}
    fails: list[str] = []
    where = f"turn {turn_no} ({turn.get('user') or ('image' if 'image' in turn else 'tap ' + str(turn.get('tap')))!r})"
    text = joined(outs)
    low = text.lower()
    log = logs[-1] if logs else {}

    def fail(msg: str) -> None:
        """Record a failure with the turn's context."""
        fails.append(f"{where}: {msg}\n      reply: {text[:600]!r}")

    if not logs:
        fail("the agent-v3 path produced no AGENT trace line (flag not applied?)")
    for a in exp.get("actions_contains", []):
        if a not in log.get("actions", []):
            fail(f"expected action {a!r}, executed {log.get('actions')}")
    for a in exp.get("actions_absent", []):
        if a in log.get("actions", []):
            fail(f"action {a!r} must not run (executed {log.get('actions')})")
    if "fast_path" in exp and log.get("fast") != exp["fast_path"]:
        fail(f"fast path {log.get('fast')!r} != {exp['fast_path']!r}")
    if "planner_calls" in exp and not LIVE and len(planner.calls) - calls_before != exp["planner_calls"]:
        fail(f"planner called {len(planner.calls) - calls_before}x, expected {exp['planner_calls']}")
    if "repairs" in exp and log.get("repairs") != exp["repairs"]:
        fail(f"repairs {log.get('repairs')} != {exp['repairs']}")
    if "translate_calls" in exp:
        from sqlalchemy import func, select

        from app.models.llm_usage import LLMUsage

        conv_row = await me.conv()
        n = (await session.execute(select(func.count(LLMUsage.id)).where(LLMUsage.conversation_id == conv_row.id, LLMUsage.purpose == "translate"))).scalar_one()
        if n != exp["translate_calls"]:
            fail(f"{n} translate (localizer) call(s) so far, expected {exp['translate_calls']}")
    for needle in exp.get("turn_contains", []):
        if needle not in trace:
            fail(f"TURN block should contain {needle!r}: {trace[-700:]!r}")
    agent_line = next((entry["raw"] for entry in logs if entry["raw"].startswith("AGENT conv=")), log.get("raw", ""))     # the TURN block's copy is clipped
    for needle in exp.get("log_not_contains", []):
        if needle in agent_line:
            fail(f"AGENT trace line must NOT contain {needle!r}: {agent_line[:400]!r}")
    for needle in exp.get("log_contains", []):
        if needle not in agent_line:
            fail(f"AGENT trace line should contain {needle!r}: {agent_line[:400]!r}")
    if "images" in exp and sum(1 for o in outs if o.image) != exp["images"]:
        fail(f"{sum(1 for o in outs if o.image)} image(s) sent, expected {exp['images']}")
    for code in exp.get("issues_contain", []):
        if not any(i.startswith(code) for i in log.get("issues", [])):
            fail(f"expected issue {code}, got {log.get('issues')}")
    if log.get("violations") and not exp.get("allow_violations"):
        fail(f"invariant violations: {log['violations']}")
    for needle in exp.get("reply_contains", []):
        if needle.lower() not in low:
            fail(f"reply should contain {needle!r}")
    for needle in exp.get("reply_not_contains", []):
        if needle.lower() in low:
            fail(f"reply must NOT contain {needle!r}")
    ids = [i for o in outs for i in o.action_ids()]
    for b in exp.get("buttons_contain", []):
        if re.fullmatch(r"v3_(opt|rm)_\d+", b):         # "an option button": ids are structured (v3_opt:<line>:<slot>:<value>, v3_rm:<line>)
            wanted = ("v3_opt:", "v3_var:", "v3_opt_") if b.startswith("v3_opt") else ("v3_rm:",)
            if not any(i.startswith(wanted) for i in ids):
                fail(f"no option button offered (offered {ids})")
        elif b not in ids:
            fail(f"button {b!r} not offered (offered {ids})")
    for b in exp.get("buttons_absent", []):
        if b in ids:
            fail(f"button {b!r} must not be offered")
    if exp.get("ends_with_question"):
        last = (outs[-1].text if outs else "").strip().split("\n\n")[-1]
        if "?" not in last and not any(o.buttons or o.rows for o in outs):
            fail("reply does not end with a question / CTA")
    cart_row = await me.cart()
    conv = await me.conv()
    if "stage" in exp and (cart_row.state or {}).get("stage") != exp["stage"]:
        fail(f"cart stage {(cart_row.state or {}).get('stage')!r} != {exp['stage']!r}")
    if "conv_stage" in exp and conv.current_stage != exp["conv_stage"]:
        fail(f"conversation stage {conv.current_stage!r} != {exp['conv_stage']!r}")
    if "cart" in exp:
        want = exp["cart"]
        lines = (cart_row.lines or []) if cart_row else []
        if want.get("empty") and lines:
            fail(f"cart should be empty, has {lines}")
        if "lines" in want and not _match_lines(lines, want["lines"]):
            fail(f"cart lines {[{k: ln.get(k) for k in ('sku', 'color', 'size', 'qty')} for ln in lines]} != {want['lines']}")
        for k, v in (want.get("customer") or {}).items():
            if (cart_row.customer or {}).get(k) != v:
                fail(f"cart customer.{k} = {(cart_row.customer or {}).get(k)!r} != {v!r}")
        if "payment_method" in want and cart_row.payment_method != want["payment_method"]:
            fail(f"cart payment_method {cart_row.payment_method!r} != {want['payment_method']!r}")
    if "orders" in exp or "order" in exp:
        orders = await orders_of(session, me.client.id)
        if "orders" in exp and len(orders) != exp["orders"]:
            fail(f"{len(orders)} orders, expected {exp['orders']}")
        if "order" in exp and orders:
            from sqlalchemy import select

            from app.models.order_line_item import OrderLineItem

            o, want = orders[-1], exp["order"]
            for key in ("status", "payment_method"):
                if key in want and getattr(o, key) != want[key]:
                    fail(f"order.{key} {getattr(o, key)!r} != {want[key]!r}")
            if "total" in want and int(o.total_amount) != int(want["total"]):
                fail(f"order total {o.total_amount} != {want['total']}")
            if "lines" in want:
                items = (await session.execute(select(OrderLineItem).where(OrderLineItem.order_id == o.id))).scalars().all()
                actual = [{"sku": i.product_sku, "color": i.variant_color, "size": i.variant_size, "qty": i.quantity} for i in items]
                if not _match_lines(actual, want["lines"]):
                    fail(f"order lines {actual} != {want['lines']}")
    from tests.scenarios.lint import foreign_words

    allowed = frozenset(w for o in outs for w in re.findall(r"[a-z]{3,}", (turn.get("user") or "").lower())) | _SHOP_WORDS
    leaked = foreign_words(text, conv.last_customer_language, allowed)
    if leaked and not exp.get("allow_foreign_words"):
        fail(f"reply in {conv.last_customer_language} contains another language's template words: {sorted(leaked)}")
    if "handoff" in exp and (conv.ai_enabled is False) != exp["handoff"]:
        fail(f"bot paused={conv.ai_enabled is False}, expected handoff={exp['handoff']}")
    return fails


async def run_scenario(name: str, doc: dict, http, session, monkeypatch, planner: ScriptedPlanner, caplog) -> list[str]:
    """Play one scenario; returns its failures (empty = pass)."""
    sent = capture_structured(monkeypatch)
    client, phone, pnid = await seed_shop(session, n="01", shop=doc.get("shop", "default"))
    setup = doc.get("setup", {})
    if setup.get("orders"):
        await _seed_orders(session, client, phone, setup["orders"])
    if setup.get("stage"):
        from datetime import datetime, timezone

        from app.models.conversation import Conversation

        session.add(Conversation(phone_number=phone, channel="whatsapp", client_id=client.id, current_stage=setup["stage"],
                                 flow_state_at=datetime.now(timezone.utc)))
        await session.commit()
    me = Customer(http, session, sent, client, phone, pnid)
    failures: list[str] = []
    caplog.set_level(logging.INFO)
    for n, turn in enumerate(_expand(doc["turns"]), start=1):
        cart_row = await me.cart()
        line_ids = [ln["line_id"] for ln in (cart_row.lines if cart_row else [])]
        if not LIVE:
            rounds = [turn["plan"]] if turn.get("plan") is not None else []
            rounds += list(turn.get("repair") or [])
            if rounds and turn.get("usage"):
                rounds[0] = {**rounds[0], "__usage__": turn["usage"]}
            planner.script([ProviderError(r["error"], r.get("status_code")) if "error" in r else _subst(r, line_ids) for r in rounds])
        calls_before = len(planner.calls)
        if turn.get("age_cart_hours"):       # the cart has sat untouched for this many hours (a customer who comes back later)
            from datetime import datetime, timedelta, timezone

            from sqlalchemy import update

            from app.models.conversation_cart import ConversationCart

            cart_row = await me.cart()
            await session.execute(update(ConversationCart).where(ConversationCart.id == cart_row.id).values(
                updated_at=datetime.now(timezone.utc) - timedelta(hours=float(turn["age_cart_hours"]))))
            await session.commit()
        caplog.clear()
        ts = None
        if turn.get("ts"):        # sender-side Meta timestamp, relative to when the bot's latest list was sent
            cart_row = await me.cart()
            lists = list((cart_row.state or {}).get("shown_lists") or [])
            sent_at = lists[-1]["sent_at"] if lists else time.time()
            # the scenario runs in milliseconds: put the earlier replies a minute back so "typed before the newest one arrived" is meaningful
            cart_row.state = {**cart_row.state, "shown_lists": [{**h, "sent_at": h["sent_at"] - 60} for h in lists[:-1]] + lists[-1:]}
            await session.commit()
            ts = int(sent_at - 1) if turn["ts"] == "before_last_list" else int(sent_at + 30)
        if "image" in turn:
            outs = await me.image()
        elif "tap" in turn:
            outs = await me.tap(turn["tap"], ts=ts)
        else:
            outs = await me.say(turn["user"], ts=ts)
        logs = parse_agent_logs(caplog.records)
        trace = "\n".join(r.getMessage() for r in caplog.records if r.name == "app.services.turn_trace")
        failures += await _check(n, turn, me, outs, logs, planner, calls_before, session, trace)
        if not LIVE and planner.queue:
            failures.append(f"turn {n}: {len(planner.queue)} scripted planner plan(s) were never used")
            planner.queue = []
    return failures


@pytest.fixture
def planner(monkeypatch):
    """Scripted planner installed as agent_planner.call_model (left alone in live mode)."""
    fake = ScriptedPlanner()
    if not LIVE:
        monkeypatch.setattr("app.services.agent_planner.call_model", fake)
    return fake


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_scenario(name, replay_http, replay_session, planner, monkeypatch, caplog):
    """Play one YAML scenario against the real stack."""
    failures = await run_scenario(name, SCENARIOS[name], replay_http, replay_session, monkeypatch, planner, caplog)
    _RESULTS[name] = not failures
    if STRICT:
        assert not failures, f"scenario {name!r} ({SCENARIOS[name].get('description', '')}):\n  " + "\n  ".join(failures)


def test_zz_pass_rate_gate():
    """CI gate: at least AGENT_SCENARIO_MIN_PASS_RATE (95 %) of the scenarios that ran must have passed."""
    if not _RESULTS:
        pytest.skip("no scenario ran (Postgres unavailable?)")
    rate = sum(_RESULTS.values()) / len(_RESULTS)
    failed = sorted(k for k, ok in _RESULTS.items() if not ok)
    assert rate >= PASS_RATE_GATE, f"scenario pass rate {rate:.0%} < {PASS_RATE_GATE:.0%}; failing: {failed}"
