"""
Offline evaluation of the LLM tool-router prompt against a labelled dataset.

Run from backend/:

    python tests/router_eval/run_eval.py --golden                # production prompt + model (LLM_MODEL_CLASSIFIER)
    python tests/router_eval/run_eval.py --model openai/gpt-oss-20b llama-3.3-70b-versatile
    python tests/router_eval/run_eval.py --allow-unlabeled     # run unlabelled rows too (tokens/cost/outputs only)

Calls only the Groq chat API (needs GROQ_API_KEY). No WhatsApp/Instagram sends, no DB
access (the catalogue comes from catalogue_snapshot.json), and the live pipeline is not
imported — only config and the shared llm_client request builder, so reasoning-model
settings match production.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pydantic import ValidationError  # noqa: E402

from app.schemas.router import RouterDecision  # noqa: E402
from app.services import intent_router  # noqa: E402
from tests.router_eval.common import (  # noqa: E402
    ACTIONS, CATALOGUE_PATH, GOLDEN_PATH, HERE, LANGUAGE_CODE, PRIMARY_ARG, UNSCORED_ARGS,
    eval_context, load_dataset,
)

# The prompt is the PRODUCTION prompt (app/prompts/router_v2.md): offline accuracy = production behaviour.
PROMPT_PATH = intent_router.PROMPT_PATH
PRICES_PATH = HERE / "prices.json"


def load_prompt(path: Path | None = None) -> tuple[str, str]:
    """(system_prompt, user_template) of the production router prompt (or `path`, for prompt experiments)."""
    parts = intent_router.load_prompt(str(path or PROMPT_PATH))
    return parts.system, parts.user_template


def render_user_prompt(template: str, row: dict, catalogue: list[dict]) -> str:
    """Fill the production user template for a dataset row using the production context builders."""
    ctx = eval_context(row, catalogue)
    return intent_router.render_user_prompt(
        template, shop=ctx.shop, state=ctx.state, orders=ctx.orders, candidates=ctx.candidates,
        history=ctx.history, message=row["message"],
    )


def _norm(v) -> str:
    """Normalise an arg value for comparison (case/space-insensitive string)."""
    return " ".join(str(v).strip().lower().split())


def score_prediction(row: dict, pred: dict | None) -> dict:
    """
    Compare one prediction to its label.

    action_ok: predicted action == expected_action.
    args_ok:   None when no scorable expected arg exists; otherwise whether the primary
               arg (sku for show_product/start_order, value for answer_slot) matches.
               search_catalog `query` and faq `question` are free text and never scored.
    correct:   action_ok and (args_ok is not False).
    """
    exp_action, exp_args = row.get("expected_action"), row.get("expected_args") or {}
    if row.get("expect_low_confidence"):
        # gibberish etc.: right = the engine would ask a clarifying question (confidence below threshold)
        low = pred is not None and (pred.get("confidence") or 0) < intent_router.get_settings().router_confidence_threshold
        return {"action_ok": low, "args_ok": None, "correct": low}
    pred_action = (pred or {}).get("action")
    pred_args = (pred or {}).get("args") or {}
    action_ok = pred_action == exp_action
    args_ok = None
    key = PRIMARY_ARG.get(exp_action)
    if key and key not in UNSCORED_ARGS and exp_args.get(key) not in (None, ""):
        args_ok = action_ok and _norm(pred_args.get(key) or "") == _norm(exp_args[key])
    return {"action_ok": action_ok, "args_ok": args_ok, "correct": action_ok and args_ok is not False}


def estimate_cost_inr(model: str, in_tok: float, out_tok: float, prices: dict, override: tuple[float | None, float | None]) -> float | None:
    """₹ for the given token counts from per-1M USD prices (CLI override > prices.json); None if unknown."""
    entry = prices["models"].get(model, {})
    p_in = override[0] if override[0] is not None else entry.get("in")
    p_out = override[1] if override[1] is not None else entry.get("out")
    if p_in is None or p_out is None:
        return None
    return (in_tok * p_in + out_tok * p_out) / 1_000_000 * prices["usd_to_inr"]


def _retry_after_seconds(exc: Exception, attempt: int) -> float:
    """Seconds to wait after a 429: the provider's 'try again in Xs' / retry-after hint, else exponential backoff."""
    import re

    text = str(exc)
    m = re.search(r"try again in (?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", text)
    if m and (m.group(1) or m.group(2)):
        return float(m.group(1) or 0) * 60 + float(m.group(2) or 0) + 1.0
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    if headers.get("retry-after"):
        try:
            return float(headers["retry-after"]) + 1.0
        except ValueError:
            pass
    return min(2 ** (attempt + 1), 60)


async def call_model(client, model: str, system: str, user: str, sem: asyncio.Semaphore, tpm: int = 0) -> dict:
    """
    One router call. 429s wait for the provider's retry hint (up to 8 tries); `tpm` > 0 paces calls
    so the run stays under a tokens-per-minute cap (Groq's free/on_demand tier is 8,000 TPM).
    """
    from app.services import llm_client

    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    use_json_mode = True
    async with sem:
        for attempt in range(8):
            kwargs = llm_client.build_request(
                model, messages, max_tokens=intent_router.get_settings().router_max_tokens,
                response_format={"type": "json_object"} if use_json_mode else None,
            )
            t0 = asyncio.get_event_loop().time()
            try:
                resp = await asyncio.wait_for(client.chat.completions.create(**kwargs), timeout=60)
                usage = getattr(resp, "usage", None)
                in_tok, out_tok = getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0
                latency = round(asyncio.get_event_loop().time() - t0, 2)     # measured BEFORE any pacing sleep
                if tpm:
                    await asyncio.sleep((in_tok + out_tok) / tpm * 60)
                return {
                    "raw": llm_client.final_text(resp), "in_tok": in_tok, "out_tok": out_tok,
                    "latency_s": latency, "error": None,
                }
            except Exception as exc:  # noqa: BLE001 — report any API failure per-row
                status = getattr(exc, "status_code", None)
                print(f"  call failed ({type(exc).__name__} {status}); attempt {attempt + 1}/8", flush=True)
                if (status == 429 or isinstance(exc, asyncio.TimeoutError)) and attempt < 7:
                    await asyncio.sleep(_retry_after_seconds(exc, attempt) if status == 429 else 1)
                    continue
                if status == 400 and use_json_mode:
                    use_json_mode = False  # model/endpoint rejected json mode — retry as plain text
                    continue
                return {"raw": "", "in_tok": 0, "out_tok": 0, "latency_s": 0.0, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return {"raw": "", "in_tok": 0, "out_tok": 0, "latency_s": 0.0, "error": "exhausted retries"}


def parse_prediction(raw: str, ctx=None) -> dict | None:
    """
    Parse + validate the model output exactly like production (RouterDecision, then sanitize_decision
    when a context is given). None if it is not JSON, has no action, or fails validation.
    """
    from app.services import llm_client

    data = llm_client.parse_json_object(raw)
    if not data or "action" not in data:
        return None
    try:
        decision = RouterDecision.model_validate(data)
    except ValidationError:
        return None
    if ctx is not None:
        decision, _ = intent_router.sanitize_decision(decision, ctx)
    args = decision.args.model_dump(exclude_none=True)
    args["filters"] = decision.args.filters.model_dump(exclude_none=True)
    return {"action": decision.action.value, "args": args, "language": decision.language,
            "confidence": decision.confidence, "reply_hint": decision.reply_hint}


def summarise(results: list[dict]) -> dict:
    """Aggregate accuracy overall / per expected action / per language from scored rows."""
    def acc(rows):
        """Share of rows that are correct, or None when empty."""
        return round(sum(r["correct"] for r in rows) / len(rows), 4) if rows else None

    by_action, by_lang = defaultdict(list), defaultdict(list)
    for r in results:
        by_action[r["expected_action"]].append(r)
        by_lang[r["language"]].append(r)
    return {
        "n": len(results),
        "accuracy": acc(results),
        "action_accuracy": round(sum(r["action_ok"] for r in results) / len(results), 4) if results else None,
        "per_action": {a: {"n": len(v), "accuracy": acc(v)} for a, v in sorted(by_action.items())},
        "per_language": {k: {"n": len(v), "accuracy": acc(v)} for k, v in sorted(by_lang.items())},
        "invalid_json": sum(r["pred"] is None for r in results),
        "invalid_action": sum(r["pred"] is not None and r["pred"].get("action") not in ACTIONS for r in results),
    }


async def run_model(model: str, rows: list[dict], catalogue: list[dict], args, prices: dict) -> dict:
    """Evaluate one model over all rows and return the full result document."""
    from app.services import llm_client

    system, template = load_prompt()
    client = llm_client.get_client()
    sem = asyncio.Semaphore(args.concurrency)
    calls = await asyncio.gather(*[call_model(client, model, system, render_user_prompt(template, r, catalogue), sem, args.tpm) for r in rows])

    scored, unlabeled = [], []
    for row, call in zip(rows, calls):
        pred = parse_prediction(call["raw"], eval_context(row, catalogue))
        entry = {
            "id": row["id"], "message": row["message"], "language": row["language"],
            "expected_action": row.get("expected_action") or None, "expected_args": row.get("expected_args"),
            "pred": pred, "raw": call["raw"] if pred is None else None, "error": call["error"],
            "in_tok": call["in_tok"], "out_tok": call["out_tok"], "latency_s": call["latency_s"],
            "pred_language_match": (pred or {}).get("language") == LANGUAGE_CODE.get(row["language"], row["language"]),
        }
        if row.get("expected_action"):
            entry.update(score_prediction(row, pred))
            scored.append(entry)
        else:
            unlabeled.append(entry)

    ok_calls = [c for c in calls if not c["error"]]
    avg_in = sum(c["in_tok"] for c in ok_calls) / len(ok_calls) if ok_calls else 0
    avg_out = sum(c["out_tok"] for c in ok_calls) / len(ok_calls) if ok_calls else 0
    cost = estimate_cost_inr(model, avg_in, avg_out, prices, (args.price_in, args.price_out))
    misses = [e for e in scored if not e["correct"]]
    return {
        "model": model, "date": date.today().isoformat(), "dataset_rows": len(rows),
        "labelled_rows": len(scored), "unlabelled_rows": len(unlabeled), "api_errors": sum(bool(c["error"]) for c in calls),
        "summary": summarise(scored) if scored else None,
        "avg_input_tokens": round(avg_in, 1), "avg_output_tokens": round(avg_out, 1),
        "avg_latency_s": round(sum(c["latency_s"] for c in ok_calls) / len(ok_calls), 2) if ok_calls else None,
        "est_cost_inr_per_message": round(cost, 5) if cost is not None else None,
        "est_cost_inr_per_1000_messages": round(cost * 1000, 2) if cost is not None else None,
        "confusions": dict(Counter(f"{m['expected_action']} -> {(m['pred'] or {}).get('action', 'INVALID_JSON')}" for m in misses).most_common()),
        "misses": misses, "results": scored, "unlabelled_outputs": unlabeled,
    }


def print_report(doc: dict) -> None:
    """Human-readable console report for one model's result document."""
    print(f"\n{'=' * 70}\nMODEL: {doc['model']}   rows: {doc['dataset_rows']} (labelled {doc['labelled_rows']}, unlabelled {doc['unlabelled_rows']})   API errors: {doc['api_errors']}")
    s = doc["summary"]
    if s:
        print(f"\nOVERALL accuracy: {s['accuracy']:.1%}  (action-only {s['action_accuracy']:.1%})   invalid JSON: {s['invalid_json']}  invalid action: {s['invalid_action']}")
        print("\nPer expected action:")
        for a, v in s["per_action"].items():
            print(f"  {a:<16} n={v['n']:<3} acc={v['accuracy']:.1%}")
        print("\nPer language:")
        for k, v in s["per_language"].items():
            print(f"  {k:<18} n={v['n']:<3} acc={v['accuracy']:.1%}")
        print("\nMisses (expected -> predicted):")
        for m in doc["misses"]:
            p = m["pred"] or {}
            extra = f" args={json.dumps(p.get('args'), ensure_ascii=False)}" if p else f" [{m['error'] or 'unparsable output'}]"
            print(f"  {m['id']} {m['expected_action']} -> {p.get('action', 'INVALID')}{extra} | {m['message'][:70]!r}")
        print("\nConfusions:", doc["confusions"] or "none")
    else:
        print("\nNo labelled rows — fill expected_action (see README) to get accuracy. Outputs saved under 'unlabelled_outputs'.")
    print(f"\nAvg tokens/message: in={doc['avg_input_tokens']} out={doc['avg_output_tokens']}   avg latency: {doc['avg_latency_s']}s")
    if doc["est_cost_inr_per_message"] is None:
        print("Cost: unknown model price — add it to prices.json or pass --price-in/--price-out.")
    else:
        print(f"Est. cost: ₹{doc['est_cost_inr_per_message']:.5f}/message  (₹{doc['est_cost_inr_per_1000_messages']:.2f} per 1,000)")


async def main() -> None:
    """CLI entry: run each requested model, print its report, save results_<model>_<date>.json."""
    from app.config import get_settings

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", nargs="+", help="model id(s); default LLM_MODEL_CLASSIFIER from config (what production uses)")
    ap.add_argument("--golden", action="store_true", help="run the hand-labelled golden_router.jsonl instead of dataset.jsonl")
    ap.add_argument("--allow-unlabeled", action="store_true", help="also run rows without expected_action (excluded from accuracy)")
    ap.add_argument("--limit", type=int, help="only the first N rows (smoke test)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--tpm", type=int, default=0, help="pace calls under this tokens-per-minute cap (use with --concurrency 1; Groq on_demand = 8000)")
    ap.add_argument("--price-in", type=float, help="USD per 1M input tokens (overrides prices.json)")
    ap.add_argument("--price-out", type=float, help="USD per 1M output tokens (overrides prices.json)")
    args = ap.parse_args()

    settings = get_settings()
    if not settings.groq_api_key:
        sys.exit("GROQ_API_KEY is not set (backend/.env).")
    rows = load_dataset(GOLDEN_PATH) if args.golden else load_dataset()
    if not args.allow_unlabeled:
        rows = [r for r in rows if r.get("expected_action")]
        if not rows:
            sys.exit("No labelled rows in dataset.jsonl. Fill expected_action in dataset.csv and run import_csv.py (or pass --allow-unlabeled).")
    if args.limit:
        rows = rows[: args.limit]
    catalogue = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    prices = json.loads(PRICES_PATH.read_text(encoding="utf-8"))

    for model in args.model or [settings.llm_model_classifier]:
        doc = await run_model(model, rows, catalogue, args, prices)
        print_report(doc)
        out = HERE / f"results_{model.replace('/', '-')}_{doc['date']}.json"
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nsaved {out.relative_to(HERE.parents[1])}")


if __name__ == "__main__":
    asyncio.run(main())
