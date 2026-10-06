# Router eval (offline)

Tests whether the LLM intent router (ROUTER_V2) maps customer WhatsApp messages to the right action.
It runs the **production prompt** (`app/prompts/router_v2.md`), builds the prompt context with the
production functions and applies the production validation + sanitiser (`app/schemas/router.py`,
`intent_router.sanitize_decision`) before scoring — so offline accuracy = production behaviour.
Does **not** touch the live pipeline, never sends WhatsApp/Instagram messages, and the DB is only
used by `build_dataset.py` (read-only connection). Run everything from `backend/`.

## Quick start

```bash
# hand-labelled golden set (51 rows, every action, EN/HI/GU/Hinglish) — production prompt + model
python tests/router_eval/run_eval.py --golden --concurrency 1 --tpm 7000
```

`--tpm` paces calls under a tokens-per-minute cap: Groq's free/on_demand tier is **8,000 TPM / 1,000 requests
per day** for every chat model, and one router call is ~1.9k tokens. Edit `app/prompts/router_v2.md`
(bump the `version:` comment) and re-run to iterate. `golden_router.jsonl` rows may carry extra state keys
(`next_slot`, `slot_options`, `orders`) and `expect_low_confidence` (gibberish: correct = the engine would clarify).

## Workflow

1. `python tests/router_eval/build_dataset.py --client-id 1 --n 50` (already done; re-run refuses to overwrite labelled data without `--force`)
2. Open `dataset.csv` in a spreadsheet, fill **expected_action** (and **expected_args** where relevant), save as CSV (UTF-8).
3. `python tests/router_eval/import_csv.py [edited.csv]` — merges labels into `dataset.jsonl`, aborts on unknown actions.
4. `python tests/router_eval/run_eval.py` — or `--model openai/gpt-oss-20b llama-3.3-70b-versatile` to compare; `--limit 5` for a smoke test; `--allow-unlabeled` to run unlabelled rows (outputs/cost only).

Results: `results_<model>_<date>.json` (git-ignored).

## Filling in the CSV

Allowed `expected_action`: `greeting | order_status | search_catalog | show_product | start_order | answer_slot | change_slot | cancel_order | faq | handoff_human | general_answer | smalltalk`

`expected_args` (optional; blank = only the action is scored):
- `show_product` / `start_order`: the SKU, e.g. `SR29821` (see `catalogue_snapshot.json`) — **scored**
- `answer_slot`: the value, e.g. `M` or `COD` — **scored**
- `order_status`: the focus (`status|delivery|payment|items`) — **scored**
- `change_slot`: the new value — **scored**
- `search_catalog` / `faq` (`query`): free text, recorded but **not scored**
- Also accepted: `sku=SR29821` or JSON `{"sku": "SR29821"}`.

Optionally correct the `language` column (auto-detected; drives the per-language breakdown) and use `notes`.

## Caveats

- **State is a snapshot of the conversation's *current* state**, not as of that message (the DB keeps no history of stage/selected size etc.). For old conversations the stage can disagree with the message; judge expected_action by the message + the 6 context messages.
- Source is whatever DB `DATABASE_URL` points at (currently the local dev DB, client_id=1, non-sandbox conversations). Point it at a prod read replica for real traffic.
- Anonymisation is regex-based (phones, URLs, UPI/email, pincodes, the customer's saved name/address, "📍" lines, replies to name/address/phone questions → `<PII_REPLY>`). Skim `dataset.csv` before sharing or committing.
- Catalogue snapshot (`catalogue_snapshot.json`) is frozen at build time; it holds in-stock colours/sizes but no stock counts. Matching is stdlib `difflib` over Roman-script tokens, so Gujarati/Devanagari messages get no catalogue matches unless a SKU is typed.
- Cost uses `prices.json` (USD per 1M tokens × FX) — verify those prices; override with `--price-in/--price-out`. Reasoning models (gpt-oss) bill hidden reasoning tokens as output, which the usage numbers include.
