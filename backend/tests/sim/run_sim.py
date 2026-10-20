"""
Run N simulated customer conversations through the real app and report every defect.

    python -m tests.sim.run_sim --n 5000 --concurrency 12 --seed 1 [--chaos] [--only sc_browse_only] [--lang gujarati_script]

Writes tests/sim/out/report.json (+ report.md). Exit code 1 when any defect was found.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.sim import harness, lexicon as L, scenarios as S
from tests.sim.fake_llm import FakeGroq

OUT = Path(__file__).parent / "out"


async def run(args) -> int:
    """Create DB, wire the app, play the conversations, write the report."""
    mp = pytest.MonkeyPatch()
    mp.setenv("LLM_RATE_LIMITS", "{}")      # the fake LLM answers instantly; the client-side Groq token bucket would only queue the simulated customers
    harness.create_database()
    outbox = harness.Outbox()
    outbox.install(mp)
    chaos = dict(p_429=0.02, p_empty=0.01, p_bad_json=0.03, p_wrong_lang=0.05, p_low_conf=0.03) if args.chaos else {}
    fake = FakeGroq(seed=args.seed, translate_down=args.raw, **chaos)
    app = harness.make_app(mp, fake)
    collector = harness.ErrorCollector()
    import logging

    logging.getLogger().addHandler(collector)
    logging.getLogger().setLevel(logging.INFO if args.verbose else logging.ERROR)
    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    engine = create_async_engine(harness.ASYNC_URL, pool_size=40, max_overflow=20)
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    ids = await harness.seed(sf)

    rng = random.Random(args.seed)
    scenarios = [(f, w) for f, w in S.SCENARIOS if not args.only or f.__name__ in args.only.split(",")]
    funcs, weights = zip(*scenarios)
    langs = [l for l in L.LANGS if not args.lang or l in args.lang.split(",")]
    shops = [x for x in ("router", "legacy", "legacy", "ig") if not args.shop or x == args.shop]
    lang_w = [L.LANG_WEIGHTS[l] for l in langs]

    jobs = []
    for i in range(args.n):
        jobs.append((i, rng.choices(funcs, weights)[0], rng.choices(langs, lang_w)[0], rng.choice(shops), rng.randrange(1 << 30)))

    all_findings, transcripts, stats = [], {}, Counter()
    lat = []
    sem = asyncio.Semaphore(args.concurrency)
    t0 = time.time()
    done = 0

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        async def one(job):
            nonlocal done
            i, fn, lang, shop, seed = job
            phone = f"9199{i:07d}"
            sess = harness.Session(http, sf, shop, ids[shop], phone, lang, fn.__name__)
            sess.outbox = outbox
            async with sem:
                try:
                    await fn(sess, random.Random(seed))
                except Exception as exc:  # noqa: BLE001 - a crash of the simulated customer or app is a finding
                    import traceback

                    sess.flag("sim_exception", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}")
            all_findings.extend(sess.findings)
            lat.extend(t.ms for t in sess.turns)
            stats["turns"] += len(sess.turns)
            stats["conversations"] += 1
            if sess.findings or i % 250 == 0:
                transcripts[phone] = [dict(c=t.sent, lang=t.lang, b=[(r.kind, r.text, r.buttons, r.rows) for r in t.replies]) for t in sess.turns]
            done += 1
            if done % 250 == 0:
                print(f"  {done}/{args.n} conversations  ({time.time() - t0:.0f}s, {len(all_findings)} findings)", flush=True)

        await asyncio.gather(*(one(j) for j in jobs))

        # global DB invariants
        async with sf() as db:
            import sqlalchemy as sa

            neg = (await db.execute(sa.text("select count(*) from product_variants where stock < 0"))).scalar()
            negp = (await db.execute(sa.text("select count(*) from products where stock < 0"))).scalar()
            if neg or negp:
                all_findings.append(harness.Finding("db_negative_stock", f"variants={neg} products={negp}", "global", "-", "-", 0))
            bad_tot = (await db.execute(sa.text(
                "select count(*) from orders o where total_amount is not null and exists (select 1 from order_line_items li where li.order_id=o.id) "
                "and abs(total_amount - (select coalesce(sum(li.quantity*li.unit_price),0) from order_line_items li where li.order_id=o.id)) > 0.01"))).scalar()
            if bad_tot:
                all_findings.append(harness.Finding("db_order_total_mismatch", f"{bad_tot} orders", "global", "-", "-", 0))
            dup = (await db.execute(sa.text(
                "select count(*) from (select conversation_id from orders where status not in ('cancelled') group by conversation_id having count(*) > 1) x"))).scalar()
            stats["multi_order_conversations"] = dup

    for rec in collector.records:
        all_findings.append(harness.Finding("server_error", f"{rec['logger']}: {rec['msg']}\n{rec['exc']}", *(((rec['tag'] or 'untagged|-|-|-|t0').split('|') + ['', '', '', ''])[:3]), 0)
                            if False else harness.Finding("server_error", f"{rec['logger']}: {rec['msg']} {rec['exc'][-600:]}", (rec['tag'] or 'untagged').split('|')[0], (rec['tag'] or '||').split('|')[2] if rec['tag'] else '-', (rec['tag'] or '||').split('|')[1] if rec['tag'] else '-', 0))

    elapsed = time.time() - t0
    by_code = Counter(f.code for f in all_findings)
    by_flow_code = Counter((f.flow, f.code) for f in all_findings)
    by_lang_code = Counter((f.lang, f.code) for f in all_findings)
    report = {
        "conversations": stats["conversations"], "turns": stats["turns"], "seconds": round(elapsed, 1),
        "p50_ms": sorted(lat)[len(lat) // 2] if lat else 0, "p95_ms": sorted(lat)[int(len(lat) * 0.95)] if lat else 0,
        "llm_calls": fake.calls, "findings_total": len(all_findings), "by_code": dict(by_code),
        "by_flow_code": {f"{a}:{b}": n for (a, b), n in by_flow_code.most_common()},
        "by_lang_code": {f"{a}:{b}": n for (a, b), n in by_lang_code.most_common()},
        "multi_order_conversations": stats["multi_order_conversations"],
    }
    # group findings by (code, normalised detail) for triage, keep 3 examples each
    groups: dict = defaultdict(list)
    import re

    for f in all_findings:
        key = (f.code, re.sub(r"\d+", "N", f.detail)[:120])
        groups[key].append(f)
    OUT.mkdir(exist_ok=True)
    (OUT / "report.json").write_text(json.dumps({**report, "groups": [
        {"code": k[0], "detail": k[1], "count": len(v), "examples": [vars(x) for x in v[:3]]}
        for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))
    ]}, ensure_ascii=False, indent=1))
    (OUT / "transcripts.json").write_text(json.dumps(transcripts, ensure_ascii=False)[:20_000_000])
    print(json.dumps(report, ensure_ascii=False, indent=1))
    await engine.dispose()
    mp.undo()
    return 1 if all_findings else 0


def main() -> None:
    """CLI entry."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--chaos", action="store_true", help="inject 429s / bad JSON / wrong-language LLM answers")
    ap.add_argument("--raw", action="store_true", help="translator down: show the engine's own text before localisation")
    ap.add_argument("--verbose", action="store_true", help="print INFO logs (use with a tiny --n)")
    ap.add_argument("--only", default="", help="comma-separated scenario function names")
    ap.add_argument("--shop", default="", help="router | legacy | ig (default: all three, legacy double-weighted)")
    ap.add_argument("--lang", default="", help="comma-separated language codes")
    sys.exit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
