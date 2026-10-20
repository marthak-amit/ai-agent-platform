"""
v1.7 performance guard: fast-path turns (no planner) on a realistically large catalogue stay quick, nothing blocks the event loop, and
the per-turn ``messages.channel`` backfill UPDATE is gone.

The network is mocked (WhatsApp sends, LLM); what is measured is OUR work per turn — DB statements, catalogue handling, rendering.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from tests.scenarios.harness import Customer, ScriptedPlanner, capture_structured, seed_shop

N_PRODUCTS = 1000          # a real shop: ~1,000 products, ~6,000 variants (see catalogue_snapshot)
VARIANTS_PER_PRODUCT = 6
P95_BUDGET_SECONDS = 0.300
LOOP_LAG_BUDGET_SECONDS = 0.050

FAST_PATH_TURNS = [
    "hi", "hello buddy", "thanks", "ok", "hello ji", "kurti", "show me kurti", "hey there", "saree", "good morning",
    "I want kurti", "need saree", "show suits", "kurti chahiye", "saree joiye",
]


async def _bulk_catalogue(session, client_id: int) -> None:
    """Add ``N_PRODUCTS`` active products with ``VARIANTS_PER_PRODUCT`` colour × size variants each (bulk insert)."""
    from sqlalchemy import insert

    from app.models.product import Product
    from app.models.product_variant import ProductVariant

    colours = ["Red", "Black", "Grey", "Blue", "Green", "Pink"]
    rows = [
        {"client_id": client_id, "name": f"Catalogue Item {i:04d} Printed Dress", "sku": f"BK{i:05d}", "price": 500.0 + i % 50,
         "stock": 20, "is_active": True, "has_variants": True, "category": ("Kurti", "Saree", "Suit", "Dress")[i % 4]}
        for i in range(N_PRODUCTS)
    ]
    await session.execute(insert(Product), rows)
    await session.commit()
    ids = [r[0] for r in (await session.execute(__import__("sqlalchemy").text(
        "select id from products where client_id = :c and sku like 'BK%' order by id"), {"c": client_id})).all()]
    variants = [
        {"product_id": pid, "client_id": client_id, "color": colours[v % 6], "size": ("S", "M", "L")[v % 3], "stock": 5, "is_active": True, "price": 500.0}
        for pid in ids for v in range(VARIANTS_PER_PRODUCT)
    ]
    await session.execute(insert(ProductVariant), variants)
    await session.commit()


@pytest.fixture
def planner(monkeypatch):
    """A scripted planner that fails the test when consulted (every turn here must be a fast path)."""
    fake = ScriptedPlanner()
    monkeypatch.setattr("app.services.agent_planner.call_model", fake)
    return fake


async def test_fast_path_turns_are_quick_and_never_block_the_loop(replay_http, replay_session, planner, monkeypatch, caplog):
    """15 fast-path turns on a 1,000-product catalogue: p95 < 300 ms, event-loop lag < 50 ms, no channel backfill UPDATE."""
    from app.services import catalogue_snapshot

    sent = capture_structured(monkeypatch)
    client, phone, pnid = await seed_shop(replay_session, n="71")
    await _bulk_catalogue(replay_session, client.id)
    catalogue_snapshot.invalidate(client.id)
    me = Customer(replay_http, replay_session, sent, client, phone, pnid)

    lags: list[float] = []

    async def _probe() -> None:
        """Measure how late a 10 ms timer fires (a blocked loop shows up as lag)."""
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            lags.append(time.perf_counter() - t0 - 0.01)

    from app.services import loop_monitor

    watchdog = loop_monitor.Watchdog(asyncio.get_running_loop(), threshold=0.04)
    watchdog.start()
    caplog.set_level(logging.INFO, logger="app.services.turn_trace")
    monkeypatch.setenv("TURN_TRACE_SQL", "1")
    await me.say("hi")                       # cold: catalogue snapshot + imports (not part of the budget)
    probe = asyncio.create_task(_probe())
    durations: list[float] = []
    by_text: list[tuple[str, float]] = []
    for text in FAST_PATH_TURNS:
        t0 = time.perf_counter()
        await me.say(text)
        durations.append(time.perf_counter() - t0)
        by_text.append((text, round(durations[-1], 3)))
    print("by turn:", by_text)
    probe.cancel()
    watchdog.stop()
    for rec in caplog.records:
        if rec.name == "app.services.loop_monitor" and "LOOP_BLOCKED" in rec.getMessage():
            print("\n".join(rec.getMessage().splitlines()[:1] + rec.getMessage().splitlines()[-6:]))

    durations.sort()
    p95 = durations[min(len(durations) - 1, int(round(0.95 * len(durations))) - 1)]
    print("turn seconds:", [round(d, 3) for d in durations], "max loop lag:", round(max(lags or [0]), 3))
    assert not planner.calls, f"fast-path turns consulted the planner: {planner.calls}"
    sql = "\n".join(line for r in caplog.records for line in r.getMessage().splitlines() if "TURN-SQL" in line)
    assert "TURN-SQL" in sql, "the per-turn SQL listing is part of the TURN block"
    assert "messages SET channel" not in sql and "messages.channel IS NULL" not in sql, "the per-turn channel backfill UPDATE is back"
    assert p95 < P95_BUDGET_SECONDS, f"p95 turn time {p95:.3f}s ≥ {P95_BUDGET_SECONDS}s: {durations}"
    assert max(lags or [0]) < LOOP_LAG_BUDGET_SECONDS, f"event loop blocked for {max(lags):.3f}s"
