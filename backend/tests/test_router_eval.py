"""Tests for the offline router-eval harness helpers (no network, no DB)."""

import pytest

from tests.router_eval import common, import_csv, run_eval

CATALOGUE = [
    {"sku": "SR1", "name": "Kanjivaram Silk Saree", "price": 4200.0, "category": "Saree", "description": "", "colors": ["Gold"], "sizes": [], "in_stock": True},
    {"sku": "KU2", "name": "Cotton Kurti", "price": 600.0, "category": "Kurti", "description": "", "colors": [], "sizes": ["M"], "in_stock": True},
]


def test_anonymize_masks_pii():
    """Phones, URLs, UPI ids, pincodes, names and address lines are masked."""
    name_re = common.build_name_pattern({"Ramesh Patel"})
    out = common.anonymize("Ramesh call +91 98765 43210 pay x@okbank pin 380015 http://a.b/c\n📍 12 Foo St", name_re)
    for leaked in ("Ramesh", "98765", "okbank", "380015", "http", "Foo St"):
        assert leaked not in out


def test_top_catalogue_matches_is_fuzzy():
    """A misspelled product word still ranks the right product first."""
    assert common.top_catalogue_matches("kanjivaram sarii price", CATALOGUE)[0]["sku"] == "SR1"
    assert common.top_catalogue_matches("zzzz", CATALOGUE) == []


def test_format_catalogue_snippet_never_shows_stock_counts():
    """Snippet shows price/colours/sizes but handles empty matches."""
    assert "₹4,200" in common.format_catalogue_snippet(CATALOGUE[:1])
    assert "no close" in common.format_catalogue_snippet([])


def test_parse_expected_args_formats():
    """JSON, key=value and bare-value cells all parse; blank is None."""
    assert common.parse_expected_args('{"sku": "A"}', "show_product") == {"sku": "A"}
    assert common.parse_expected_args("sku=A;x=1", "show_product") == {"sku": "A", "x": "1"}
    assert common.parse_expected_args("A", "start_order") == {"sku": "A"}
    assert common.parse_expected_args("", "faq") is None


def test_dataset_roundtrip(tmp_path):
    """save_dataset/load_dataset preserve Gujarati text."""
    p = tmp_path / "d.jsonl"
    common.save_dataset([{"id": "r1", "message": "શું છે?"}], p)
    assert common.load_dataset(p)[0]["message"] == "શું છે?"


def test_import_merge_labels_and_rejects_bad_action():
    """Valid rows are labelled; unknown actions and unknown ids are reported."""
    rows = [{"id": "r1", "language": "english"}, {"id": "r2", "language": "english"}]
    n, errs = import_csv.merge(rows, [
        {"id": "r1", "expected_action": "Show_Product", "expected_args": "SR1"},
        {"id": "r2", "expected_action": "buy_now"}, {"id": "r9", "expected_action": "faq"},
    ])
    assert n == 1 and len(errs) == 2
    assert rows[0]["expected_action"] == "show_product" and rows[0]["expected_args"] == {"sku": "SR1"}


def test_load_prompt_is_the_production_prompt():
    """The eval runs the production prompt file (app/prompts/router_v2.md), not a private copy."""
    from app.services import intent_router

    system, user = run_eval.load_prompt()
    assert run_eval.PROMPT_PATH == intent_router.PROMPT_PATH and intent_router.PROMPT_PATH.name == "router_v2.md"
    assert system == intent_router.load_prompt().system and "JSON" in system
    for ph in ("{{shop}}", "{{state}}", "{{orders}}", "{{candidates}}", "{{history}}", "{{message}}"):
        assert ph in user
    assert not (common.HERE / "router_prompt.md").exists()


def test_render_user_prompt_fills_placeholders():
    """Rendering uses production context builders: fuzzy candidates, state, redacted last-8 history, orders, message."""
    _, tmpl = run_eval.load_prompt()
    row = {"message": "kanjivaram saree", "state": {"stage": "greeting", "x": None, "orders": ["- ORD-1 | status: paid"]},
           "context": [{"role": "bot", "text": f"m{i}"} for i in range(10)], "language": "english"}
    out = run_eval.render_user_prompt(tmpl, row, CATALOGUE)
    assert "sku=SR1" in out and "stage: greeting" in out and "ORD-1" in out and "{{" not in out
    assert "m9" in out and "m2" in out and "m1" not in out      # last 8 messages only


def test_parse_prediction_applies_production_validation_and_sanitiser():
    """Same pipeline as production: invalid action → None; a sku the model wasn't given is dropped."""
    row = {"message": "kanjivaram saree", "state": {"stage": "greeting"}, "context": [], "language": "english"}
    ctx = common.eval_context(row, CATALOGUE)
    assert run_eval.parse_prediction('{"action": "teleport"}', ctx) is None
    ok = run_eval.parse_prediction('{"action": "show_product", "args": {"sku": "SR1"}, "confidence": 0.9}', ctx)
    assert ok["args"]["sku"] == "SR1"
    hallucinated = run_eval.parse_prediction(
        '{"action": "show_product", "args": {"sku": "ZZ9"}, "confidence": 0.9}', ctx)
    assert hallucinated["args"].get("sku") is None and hallucinated["confidence"] <= 0.3


def test_golden_set_is_labelled_valid_and_covers_every_action():
    """golden_router.jsonl: every row labelled with a production action; all twelve actions appear."""
    from app.schemas.router import ROUTER_ACTIONS

    rows = common.load_dataset(common.GOLDEN_PATH)
    assert len(rows) >= 40 and all(r["expected_action"] in ROUTER_ACTIONS for r in rows)
    assert set(ROUTER_ACTIONS) <= {r["expected_action"] for r in rows}
    assert {r["language"] for r in rows} >= {"english", "hinglish", "gujarati_script", "hindi_devanagari"}
    for r in rows:   # every golden row renders against the snapshot catalogue without error
        assert "{{" not in run_eval.render_user_prompt(run_eval.load_prompt()[1], r, common_catalogue())


def common_catalogue():
    """The frozen catalogue snapshot the golden rows were written against."""
    import json

    return json.loads(common.CATALOGUE_PATH.read_text(encoding="utf-8"))


def test_score_low_confidence_rows_and_focus():
    """Gibberish rows are right when confidence is below the clarify threshold; order_status focus is scored."""
    low_row = {"expected_action": "smalltalk", "expect_low_confidence": True}
    assert run_eval.score_prediction(low_row, {"action": "smalltalk", "confidence": 0.1})["correct"]
    assert not run_eval.score_prediction(low_row, {"action": "greeting", "confidence": 0.9})["correct"]
    row = {"expected_action": "order_status", "expected_args": {"focus": "delivery"}}
    assert run_eval.score_prediction(row, {"action": "order_status", "args": {"focus": "delivery"}})["correct"]
    assert not run_eval.score_prediction(row, {"action": "order_status", "args": {"focus": "status"}})["correct"]


def test_score_prediction_action_and_sku():
    """Action must match; SKU is compared case-insensitively only when expected."""
    row = {"expected_action": "show_product", "expected_args": {"sku": "SR1"}}
    assert run_eval.score_prediction(row, {"action": "show_product", "args": {"sku": "sr1"}})["correct"]
    assert not run_eval.score_prediction(row, {"action": "show_product", "args": {"sku": "KU2"}})["correct"]
    assert not run_eval.score_prediction(row, None)["correct"]
    free = {"expected_action": "search_catalog", "expected_args": {"query": "red saree"}}
    assert run_eval.score_prediction(free, {"action": "search_catalog", "args": {"query": "x"}})["args_ok"] is None


def test_parse_prediction_handles_fences_and_garbage():
    """Fenced JSON parses; non-JSON or action-less output is None."""
    assert run_eval.parse_prediction('```json\n{"action": "faq", "args": {}}\n```')["action"] == "faq"
    assert run_eval.parse_prediction("sorry") is None
    assert run_eval.parse_prediction('{"args": {}}') is None


def test_summarise_per_action_and_language():
    """Aggregates accuracy overall and by action/language."""
    rs = [
        {"expected_action": "faq", "language": "english", "correct": True, "action_ok": True, "pred": {"action": "faq"}},
        {"expected_action": "faq", "language": "hindi_roman", "correct": False, "action_ok": False, "pred": None},
    ]
    s = run_eval.summarise(rs)
    assert s["accuracy"] == 0.5 and s["per_language"]["english"]["accuracy"] == 1.0 and s["invalid_json"] == 1


def test_estimate_cost_inr():
    """Cost uses per-1M USD prices × FX; unknown model gives None unless overridden."""
    prices = {"usd_to_inr": 80.0, "models": {"m": {"in": 1.0, "out": 2.0}}}
    assert run_eval.estimate_cost_inr("m", 1_000_000, 1_000_000, prices, (None, None)) == pytest.approx(240.0)
    assert run_eval.estimate_cost_inr("x", 10, 10, prices, (None, None)) is None
    assert run_eval.estimate_cost_inr("x", 1_000_000, 0, prices, (1.0, 1.0)) == pytest.approx(80.0)
