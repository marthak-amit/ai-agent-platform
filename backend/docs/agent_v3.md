# Agent v3 — Cart + Planner + Validator

Order flow for clients with `Client.agent_v3_enabled` (NULL → `AGENT_V3_CLIENT_IDS`, default client `1`). Everyone else keeps
the legacy router / slot machine, untouched. Hooked in `order_pipeline._handle_inbound_message_core` right after the early
guards (button nonce, blocklist, takeover, spam) and the payment-help guard.

```
message → fast path (₹0) ─┐
        → planner (1 LLM call, JSON schema) → closed-set actions
                                   ↓
        executor: validate vs DB → apply to cart copy → ≤2 repair rounds → real-options clarify
                                   ↓
        invariants → renderer (templates + DB rows) → PipelineResult
```

| File | Role |
|---|---|
| `models/conversation_cart.py`, migration `0072` | `conversation_cart`: one **active** cart per conversation (partial unique index); `lines`, `customer`, `payment_method`, `status`, `version`, `state`, `order_id`. Backfills in-flight legacy drafts (one line, or one per legacy `cart_items`). |
| `schemas/agent_actions.py` | The closed action set (pydantic discriminated union) + the flat strict JSON schema sent to Groq. |
| `services/agent_planner.py`, `prompts/agent_planner_v1.md` | The one LLM call. `LLM_MODEL_PLANNER` (default `openai/gpt-oss-120b`), temperature 0, `json_schema` response format (falls back to JSON mode if the provider rejects the schema). |
| `services/agent_executor.py` | Validates and applies actions, repair loop, deterministic fallback, order creation. |
| `services/agent_renderer.py` | Every customer-facing word (EN + Hinglish hand-written; other languages go through `reply_localizer`). |
| `services/agent_invariants.py` | Checks that run before anything is sent. |
| `services/agent_fast_paths.py` | Buttons / numbers / exact options / gate words → actions, no LLM. |
| `services/agent_cart.py`, `agent_catalog.py`, `agent_text.py` | Cart persistence, real variants + stock, pure text helpers. |
| `services/agent_v3.py` | Orchestrator for one turn + the flag. |

## Action schema

The planner returns `{"actions": [...], "reply_intent": "none|greeting|smalltalk|thanks|goodbye"}`. Each of the 16 actions has a `type`
(other keys null). The planner never writes a reply: prices, stock, ETA, totals and order numbers come from the database.

| `type` | Keys | Executor validates |
|---|---|---|
| `search_catalog` | `query` | — (0 hits → closest in-stock items) |
| `browse_catalog` | `category` \| null | — (generic "show products") |
| `show_product` | `sku` | SKU exists (exact, no prefix guessing) |
| `send_images` | `sku` | SKU exists; sends up to `AGENT_MAX_IMAGES` (3) photos — the product's, then its variants' — each with the SKU watermark |
| `add_line` | `sku`, `variant{color,size,material}`, `qty` | SKU; each value exists for that product (aliases like "laal" → Red); variant in stock; qty fits stock after the cart's other lines. Re-adding an identical variant **sets** qty (idempotent). |
| `update_line` | `line_id`, `fields{color,size,material,qty}` | line exists; same checks on the resulting line |
| `remove_line` | `line_id` | line exists |
| `split_line` | `line_id`, `parts[{variant, qty}]` | parts' qty sum == line qty (`QTY_MISMATCH`); each part in stock |
| `set_customer` | `name`, `address`, `mobile` | name plausible; address passes `conversation_flow.is_valid_address`; 10-digit mobile |
| `set_payment` | `method` COD\|UPI | the shop offers it (UPI needs a UPI id) |
| `ask_clarify` | `question`, `options[]`, `slot` | options replaced by real catalogue values; question must pass `sanitize_question` (no digits / prices / claims) |
| `request_confirmation` | — | cart complete, stock still fits |
| `confirm_order` | — | summary for **this** cart is on screen, customer said an explicit yes, nothing else changed this turn |
| `cancel` | — | explicit wording, else asks "Cancel this order? (Yes / No)"; empty cart + unpaid order → cancels the order |
| `handoff_human` | `reason` | — (pauses the bot) |
| `answer_question` | `topic` | answered from the shop's facts via `side_questions.build_answer` |

### Failure handling

Issues the **planner can fix** (`UNKNOWN_SKU`, `UNKNOWN_LINE`, `INVALID_VARIANT(_ATTR)`, `QTY_MISMATCH`, malformed action) are
sent back as structured errors with the real `valid_options`: at most `AGENT_MAX_REPAIRS` (2) rounds. Issues it cannot fix
(sold out, quantity above stock, incomplete cart, bad address, payment not offered) are answered deterministically at once.
Whatever remains becomes an `ask_clarify` built from **real** options ("Pink / M", "Blue / XXL" for a sold-out Pink/XXL).
After `AGENT_CLARIFY_FAIL_LIMIT` (2) unresolved clarifies on the same slot the conversation is handed to a person.
Valid actions of a plan are kept even when a sibling action fails (no all-or-nothing).

### Invariants (violations are logged as `INVARIANT …` and repaired)

1. a colour / size / material / quantity / payment / name / address is written only if the customer's message or a tap produced it — otherwise reverted and asked;
2. a reply ends with a question / CTA, or is a terminal state (order placed, hand-off);
3. a new cart starting while an order awaits payment / verification gets one "Your order #X is being verified. Meanwhile …";
4. the conversation stage is not touched while an order is pending;
5. no ₹ amount that no template / DB row produced; no "not available" without the options that are.

## Operating it

* Flag: `UPDATE clients SET agent_v3_enabled = true WHERE id = …` (or the admin console flags endpoint). `false` forces the legacy path.
* Trace: every turn logs `AGENT conv=… fast=… planner=… actions=[…] issues=[…] repairs=n violations=[…] cart=[…] stage=…` and
  `PLANNER … actions=… parse_issues=… v=<prompt version/hash>`; both show up in the `TURN … WHY` block.
* Cost: ≈ one planner call per non-fast-path message (`llm_usage.purpose = "planner"`). Repairs add calls. The Groq free tier
  (8k tokens/min) cannot carry this; a paid tier is needed in production.
* Tests: unit `tests/test_agent_v3_units.py`; end-to-end YAML scenarios `tests/scenarios/*.yaml` run by
  `tests/scenarios/test_scenarios.py` against real Postgres with a **scripted planner**. `RUN_LIVE_LLM=1` ignores the scripts and
  uses the real planner (needs `GROQ_API_KEY`); the suite fails below `AGENT_SCENARIO_MIN_PASS_RATE` (0.95).

## Behaviour added after the live conv=60 test (2026-10-19)

* **Token / cost log** — the `AGENT …` line carries `tok=<in>/<cached>/<out> cost_usd=… conv_tok=… conv_cost_usd=…` (planner calls of the
  turn, repairs and 429 retries included; running per-conversation total kept in `conversation_cart.state.usage_total` and carried
  across carts). Prices: `AGENT_PLANNER_PRICE_USD_PER_1M` = (0.15 input, 0.075 cached input, 0.60 output).
* **Greeting with a cart** — "Welcome back! You have N items in your cart." + `[Continue order] [Start fresh]`; no summary, no upsell, the
  open question / summary stay valid. A greeting / thanks never wipes a product list the customer may still answer by number.
* **Upsell is deterministic per cart** — drawn from the best three candidates with a generator seeded by the cart id, then kept
  (`state.upsell`) every time that cart's summary is shown. ≤ 30 % of the cart; never a product viewed-and-left, removed, or offered for an
  earlier cart.
* **Priority rule** — a product / category that is not the open line is searched / shown first and the open line is kept without
  re-asking its slot. Prompt rule 12–13, plus an engine guard: an *empty* plan for a message that names something the shop sells (and no
  free-text slot is open) becomes `search_catalog`. Plural words match singular products ("sarees" → saree).
* **Fast paths are bare gate words only** — yes / no / cancel / confirm fire without the planner only for ≤ 3 words that name nothing the
  shop sells. A longer sentence goes to the planner, which may return several actions (`[cancel, search_catalog("shoes")]`). A search with
  no hit says "We don't have shoes. We have: <categories>." + a Browse button.
* **Planner failure is never a bare apology** — error / invalid JSON / 429 after one retry (waits the provider's "try again in Xs",
  capped at 6 s): a message naming a catalogue product becomes a search, otherwise the current step is re-asked with its buttons. The raw
  model output / provider `failed_generation` is logged (`PLANNER invalid output …`, `PLANNER provider error …`).
* **Single-option autofill** — an attribute with exactly one available value is selected and announced ("Only One Size is available for
  Leather Wallet, so I selected it."); it runs after the grounding invariant.
* **Stage sync every turn** — browsing → greeting / product_inquiry · collecting → order_collection · confirming →
  awaiting_final_confirmation · order placed → payment (UPI) / completed (COD, paid at once). With an order pending, a browsing cart is
  `payment` (never reset to greeting) and a new cart being filled shows its real stage.

## Round 3 (live conv=60, 2026-10-19 16:00)

_Cost display cleanup (later): the COST line and the AGENT line's `tok= cost_usd=` (planner rows of this turn) read `llm_usage` only; the per-process
accumulators and the cart `usage_total` are gone (so the AGENT line no longer has `conv_*`). `scripts/backfill_llm_cost_usd.py` filled `cost_usd`
on pre-0073 rows. `GET /admin/llm-cost` also returns `per_client_last_7d` (LLM ₹, confirmed orders, avg ₹ per confirmed order)._

**Cost tracking (all LLM calls).** `llm_client.llm_call` / `llm_transcribe` report every provider call to `llm_usage_service.record_usage`
with prompt / *cached* / completion (reasoning included) tokens, or Whisper audio seconds. Prices live in config
(`LLM_PRICE_USD_PER_1M` = per model `[in, cached_in, out]`, `LLM_STT_USD_PER_HOUR`); `USD_INR` is **required** (no default in code).
Table `llm_usage` (migration 0073 added `cached_tokens`, `audio_seconds`, `cost_usd`; `prompt_tokens` = in_tok, `completion_tokens` =
out_tok). The TURN block prints
`COST turn=₹0.15 session=₹0.32 lifetime=₹4.79` — all three read from the `llm_usage` ledger: `turn` = this message's rows, `session` = since the
conversation's latest *confirmed cart* (`confirm_order`; not an inbound photo, not a cancelled / expired cart — round 4) or the last 24 h, whichever is later (conversations that never had a cart, i.e. the legacy flow, use their last placed order), `lifetime` = every row of the conversation —
and
`TIME total=…ms wait=… db=… search=… planner=… render=… localizer=… wa_send=… image_send=…` (spans overlap: `db` is inside the others;
`wait` = deliberate delay + batching). `GET /admin/llm-cost?client_id=&from=&to=` (permission `usage.read`) returns totals per day /
purpose / model / conversation / order in ₹ and $, plus `orders_created` and `cost_per_order_created_*`.

**Latency.** The webhook verifies, rate-limits and de-duplicates (also against messages still in flight), answers `{"status":"accepted"}`
at once and processes the turn in a background task; turns of one sender run in arrival order (ticket chain, compatible with message
batching). `WEBHOOK_BACKGROUND_PROCESSING=false` processes inline (the test suite). Agent-v3 specifics: the catalogue's static rows are
cached per tenant for 60 s (`catalogue_snapshot`, dropped on every `catalogue_service` write) while stock + reservations are re-read each
turn; the human-like pause is a *target total* (`AGENT_REPLY_DELAY_MIN/MAX_SECONDS` 0.3–0.8 s) of which only the remainder is slept;
v3 tenants batch with 1.5 / 2.5 / 4.0 s windows (`AGENT_BATCH_*`) and never hold a greeting / thanks / yes-no / exact product name /
photo request / side question; an exact product name is a ₹0 fast path.

**Behaviour.** `search_catalog` parses colour / size / material from the query and filters on in-stock variants; no match →
"No red saree right now. Closest: Maroon — …" (nearest colour family / size). Grounding also accepts a line value that is already in
the cart or was chosen earlier for the same SKU ("same size"). The planner prompt is cache-friendly: rules + action reference +
store policies are one byte-identical system message, the user message holds only what changes (history, state, catalogue hits, message)
and the catalogue stock is a compact `{colour: {size: stock}}` map. The plan schema is a typed `anyOf` union (one branch per action
with its own required fields) with fallbacks to the flat schema and to JSON mode; INVALID_ACTION is logged with the raw output.

## Round 4 (live conv=60, 2026-10-19 17:40–18:00)

1. **Colour search** — `Catalogue.search_filtered` filters FIRST, over every product (in-stock variant with a matching colour / size / material), and
   only then ranks what is left by the remaining words and cuts to the top 5. Before, it took the 8 best *name* matches and filtered those, so on
   a shop with many "… Block Print Saree" rows a "black saree" never saw the black one (client 1 does have 72 black sarees — the earlier
   "none" answer was this bug, not an empty shelf). A colour word is matched against variant colour values only (exact or via the shared
   colour vocabulary: black = kala / kalo / kaali / કાળો …), never fuzzily against product names ("Black Magic Kurti" in cream is not black).
2. **List context by time** — every reply stores `state.shown_lists` (the open question it left, or none, + `sent_at`; 6 kept, carried across
   carts). A number / option resolves against the latest question DELIVERED (`sent_at + 2 s`) before the customer's own Meta timestamp
   (`message.timestamp`), not the newest one — "1" typed while the next reply was in flight picks from the list the customer was looking at.
   `CartData.clarify_as_of(ts)`; no timestamp (widget / IG) or a far-future one → newest. The planner and fast paths both get that question.
3. **Reactions** — `agent_social.is_reaction` ("wah / wow / nice / mast / saras / 👍 …", ≤ 4 words, nothing else): ₹0 fast path, one line
   + the same list's rows / the same card's buttons (`Renderer.reshow_open_question`), list memory kept. Not while a name / address / phone is asked.
4. **Declines** — `agent_social.is_decline` ("no thanks", "nahi joiye", "bas", "not now", …; a bare "no" only when no yes/no question is open):
   "Okay! Message anytime 😊", nothing else (with items in the cart the open step follows). Never the thanks reply.
5. **Best sellers** — `browse_catalog.sort = "bestseller"` (fast path for "best product / best seller / saru shu che / sabse popular …"; the planner
   can set it too, prompt v1.3): products ranked by units in real order lines (`units_sold_by_sku`: cancelled / failed / expired orders and
   simulator orders excluded), header "Our best sellers 🌟", the categories line, the numbered list. No sales yet → the plain "Here's some of what
   we have" browse — the bot never claims best sellers it cannot prove.
6. **Localization** — `app/services/agent_templates/{hindi_roman,hindi_devanagari,gujarati_roman,gujarati_script}.json` hold EVERY renderer key
   (139), loaded at import / startup (`agent_templates.warm` logs problems; `problems()` = missing keys, changed `{placeholders}`, English words in a
   native-script text, >20-char buttons — covered by `tests/test_agent_round4_units.py`). English-only constructions became keys (`eta_*`,
   `for_what*`, plural / "is-are" stock lines, list headers, the "one moment" and photo-not-recognised replies). `Reply.localized` →
   `PipelineResult.skip_localizer` → `_ensure_reply_language` returns at once: a template turn costs **0 LLM calls and 0 ms of localizer**. Only
   dynamic free text still goes through the localizer: the planner's own clarify question and a knowledge-base answer (`build_answer(info=)`),
   plus every language without a table (Marathi, Tamil, …). The reply language is the language of the customer's LAST message
   (`agent_v3._reply_language`: a confident detection switches even in the middle of an order; a name / address / phone answer, a bare number /
   yes / emoji or a tap keeps the previous language; while a free-text slot is open only a script change switches).
7. **Nudge age cap** — `scheduler._send_abandoned_intent_followups`: past Meta's 24 h window nothing is sent; the conversation is marked once
   (`last_followup_sku`) and logged at INFO — `nudge_needs_template` for 24–72 h, `nudge_stale` for 72 h+ (`_FOLLOWUP_STALE_HOURS`) — so the job no
   longer warns about the same chat every 6 h; one `nudge_run examined= sent= needs_template= stale=` summary per run.
8. **Session cost** — `COST session=` resets at the latest `confirm_order` (a confirmed `conversation_cart`) or after 24 h, never at an inbound
   image or a cancelled cart.

Scenarios: `40_colour_search_all_products` … `50_dynamic_text_still_uses_the_localizer` (harness: per-turn `ts: before_last_list|after_last_list`,
`image: true`, `translate_calls: N`, order seeding with line items).

## Round 5 (live conv=60 ORD-2026-0074, 2026-10-19 — quality + language)

Planner prompt `agent_planner_v1.4` (file name unchanged). Scenarios `52`–`60`, units `tests/test_agent_v14_units.py`.

1. **Removal** — `remove_line` takes `line_id` **or** `sku` (all lines of the product). `agent_removal.detect` reads "kurtinathi joti / nahi chahiye / mat bhejo / hatao /
   kadhi nakho / don't want / remove / cancel <item>" (EN, Hinglish, Roman + native Gujarati/Hindi/Marathi) at ₹0 as a fast path (`remove`): the WHOLE sentence must be
   explained by the phrase + words naming cart items (a change of mind — "…, I want shoes" — goes to the planner). Several equally likely products → `ask_clarify(slot remove)`
   with one `v3_rm:<line_id>` button per line (`remove_which`). Reply: `ack_removed` + "In your cart now" (`cart_block`) + the next open question / the summary.
2. **Pointless clarifies** — `Executor._pointless_clarify`: a planner `ask_clarify` for a slot with ONE available value, or for a slot that is already set on every line
   (customer's message does not mention it, not a repair round), is dropped and logged (`fixes=[…]` on the AGENT line); `autofill_single_options` / the next gap take over.
3. **Structured ids** — option buttons/rows are `v3_opt:<line_id>:<slot>:<value>` (`v3_var:<line|->:<sku>:<combo>` for colour×size lists, `v3_rm:<line_id>`): a tap resolves from
   the id alone — not from `last_clarify`, not from the (shortened) title; a tap on a line that left the cart is a no-op re-ask. Old `v3_opt_N` ids still work. >150 chars → positional.
4. **Focus line** — `state.focus_line_id` (last line added/changed); `ex.gaps()` asks about it first (`focus_first`).
5. **Greeting on a cart** — `finish()` no longer turns a bare greeting into the old line's question. Idle < `AGENT_GREETING_STALE_HOURS` (6): short welcome-back + [Continue][Start fresh]
   (as before); ≥ 6 h: `welcome_back_stale` — greeting, the cart lines, [Continue order][New order][Browse products]. A greeting never refreshes `updated_at` (`save(touch=False)`).
6. **Spelling / transliteration** — `agent_query` (alias map + `rapidfuzz.fuzz.ratio ≥ AGENT_FUZZY_MIN_RATIO` against THIS tenant's product/category words) feeds
   `Catalogue.search/search_filtered/in_category/mentions/keyword_hit`. Category exists but sold out → `category_empty`; neither category nor product → `category_not_found` + the real
   categories (browse no longer falls back to "the first 5 products").
7. **Labels** — `wa_labels` (word boundary + "…"; button 20 / row title 24 / description 72 / list button 20 / header 60) applied once in `Renderer.render` → `fit_labels`.
8. **Perf** — `http_client.meta_session()`: one keep-alive `httpx.AsyncClient` (HTTP/2 when `h2` is installed) instead of one per WhatsApp send; the TURN `TIME` line gains
   `wa_connect` (DNS/TCP/TLS) and `wa_request`. The webhook already acked Meta immediately (`WEBHOOK_BACKGROUND_PROCESSING`).
9. **Language** — `language_tracker` (no LLM): per-message marker scoring, hysteresis (strong message or 2 consecutive clear ones switch; taps/"ok"/numbers/names never), seller default
   (`Client.dashboard_language`) for a first message without signal; state in `cart.state.lang_track` (no migration), mirrored to `Conversation.last_customer_language`; log `LANG conv= lang= conf= switched=`.
   v3 clients skip the legacy one-message `sync_conversation_language`. Five template tables (`english.json` is the reference); `agent_templates.assert_complete()` runs at startup.
   Planner free text (`ask_clarify.question`) in the wrong language → template + `lang_mismatch`. `docs/templates_review.md` (generated by `scripts/gen_templates_review.py`) lists every key × 5 languages.
10. **Lint** — scenarios fail when a Roman-script reply contains another language's template words (`tests/scenarios/lint.py`).


## Round 6 (live conv=60 ORD-2026-0075 / ORD-2026-0068, 2026-10-20 — wrong colours ordered, product photo filed as payment, 500 on approve)

Planner prompt `agent_planner_v1.5`. Migration `0074` (indexes only). Scenarios `61`–`67` (`61b`, `63b`), units `tests/test_agent_v15_units.py`,
`tests/test_payment_inbound_gate.py` (classifier), replay `tests/replay/test_payment_proof_hardening.py`.

1. **Quantity per option** — `agent_multivariant.parse` reads "2 purple and one red and one green" (separators `, ; & + / | newline` and joiner words in
   EN/Hinglish/Gujarati/Hindi/Marathi; quantity = digits in any script, number words, `2x`; option = a variant value of the product in focus, colour aliases and
   one-letter typos included) and `agent_fast_paths.multi_variant` turns it into one `add_line` per pair (an existing line of that option gets its quantity
   SET, an empty line is filled — never a duplicate; the focus line's other attributes are carried). A line that already holds the TOTAL quantity ("3 kurtis")
   becomes one `split_line`, so "2 red 2 green" for 3 is still answered with "adds up to 4 but the order is for 3". An option the product does not come in
   (a colour/size word that is not a variant: "green") never cancels the valid pairs: `r_option_unavailable` — "Green isn't available for <product>.
   Available: Navy, Purple, Red." Only if EVERY segment is `<qty> <option>` (else → planner); never while a name/address is open; ₹0.
   **No template carries example values any more** (`q_split` with `e.g. {example}` is gone from all five tables, `(e.g. 2)` too): the customer's copy of
   "2 Navy, 1 Purple" was the bug. `q_split_named` ("How many of each — Red, Green?") quotes only options the customer wrote.
   **Grounding** — a colour/size/material the customer's message or tap does not contain is reverted and logged `option_not_from_user` (add_line, update_line
   and split_line alike). Planner prompt v1.5: rule 17 (quantity per option, five-language examples), rule 18 (never choose an option).
2. **Payment proof classification** — `payment_proof_classifier.classify` (caption → catalogue SKU watermark → whole-image OCR (Tesseract) → vision model only
   when still unsure) returns `payment_proof | product_photo | other` + confidence + signals (amount = an unpaid order's total ±₹1, UTR / 12-digit reference,
   "payment successful / paid to", GPay/PhonePe/Paytm/BHIM). `payment_inbound` acts on it: decisive proof → filed under the right order; decisive product
   photo → the normal product flow (`InboundContext.image_is_product_photo`; v3 also tries a vision match on catalogues ≤ 80 products, else "Nice! Want me to
   find something similar? …"); unsure → buttons [Payment screenshot][Product photo] (`v3_proof_payment|product`), the tap decides (stateless: the customer's
   newest unattached image of the last hour). OCR unavailable = no evidence = ask (it used to default to "proof"). One `PROOF_CLASSIFY` log line per image.
   **Several unpaid orders** → `pick_order_for_amounts` (single order, or the one whose total is on the receipt); ambiguous → "Which order is this payment
   for?" with one button per order (`v3_proof_order:<id>`); the ack names the order.
3. **"This is product not the payment"** — `pvs.is_payment_denial` (EN/Hinglish/Gujarati/Hindi/Gujarati-script; strict: a demonstrative + "payment", "wrong photo",
   "by mistake"; questions and "I haven't paid" are not denials) → `pvs.withdraw_last_proof`: the newest pending proof becomes `withdrawn` (row + `proof_withdrawn`
   audit entry kept; `list_payments`/`count_pending` ignore it), the order goes back `payment_submitted → pending_payment` when no other pending proof remains,
   realtime `payment_reviewed` (decision `withdrawn`), the customer gets `pay_proof_withdrawn` + the product-flow invitation. A repeated denial with nothing
   attached → `pay_denial_nothing`. `is_payment_help_request` is False for a denial, so `run_payment_help_guard` can no longer resend the instruction.
4. **High-value approve** — `ManualReviewRequired` holds plain values (`order_id, order_number, total, threshold`), `.payload()` is the 409 body
   `{code: "high_value_confirmation_required", order_number, order_id, total, threshold, message}`. Rule (CLAUDE.md "Coding Rules"): never read ORM attributes
   after a rollback / failed transaction — copy what a handler needs when you raise. A static test (`test_no_custom_exception_stores_an_orm_object`) guards it.
   Dashboard: `api/chat.ts approvePayment` → `utils/highValueConfirm.ts` → `HighValueConfirmHost` modal ("Verify ₹X in your bank before approving") → re-POST
   with `confirm_high_value: true`.
5. **WhatsApp labels** — `wa_labels.unique_labels/unique_buttons/unique_row_titles` guarantee distinct titles after shortening ("Floral… – Navy" / "Floral… – Purple",
   else " 1", " 2"), applied in `Renderer.fit_labels` AND in `_whatsapp_adapter`. Cart disambiguation ("which one should I remove?") is a LIST (title ≤ 24, description =
   full name + variant + qty), never buttons. A refused interactive send (Meta #131009 or any 400; `whatsapp_service.last_interactive_error`) logs
   `wa_interactive_fallback conv= kind= status= code=` and sends the options as numbered plain text + "Reply with the number" (five languages).
6. **Remove something that is not in the cart** — `agent_removal.detect` → `Removal.absent` for a short removal sentence whose noun names nothing in the cart
   ("Remove chair"): "There's no chair in your cart." + the cart + [Remove an item][Continue order] (`v3_remove_menu` → the which-one list). ₹0; "I don't want this, I want
   shoes", "cancel payment", "remove it" etc. are left alone; while a name/address is open only a sentence BEGINNING with a removal verb counts.
7. **Stage vs pending orders** — a pending order no longer pins `Conversation.current_stage` to `payment` (`stage_to_mirror`). The TURN header shows both:
   `TURN conv=60 stage=browsing pending_orders=[ORD-2026-0068:payment_submitted] 412ms` (`turn_trace.note`).
8. **Upsell** — button label "+ Chanderi ₹3,199" (`Renderer.upsell_label`, ≤ 20 chars, nickname = name minus category words); ONE offer per order cycle
   (`cart.state.upsell_done`); none once the cart is worth more than `AGENT_UPSELL_MAX_CART_TOTAL` (₹10,000; per client `upsell_max_cart_total` if the client row has one);
   a product the customer removed is never offered again (`removed_skus`).
9. **Cheap fast paths** — `agent_social.is_ack` ("ok", "fine", "hmm", "theek hai", "ઓકે" …) and `is_praise_sentence` ("You have such a amazing catalogue",
   "aapka collection bahut accha hai", "tamaru collection khub saras che") join `is_reaction` → `fast=reaction`, 0 planner calls, the open question's buttons stay. "hi sir / hello /
   good morning" are WEAK English markers in `language_tracker` (set a first message's language, never flip a running chat); "kem cho" → gujarati_roman; "namaste" is neutral.
10. **Performance** — see "Performance findings" below.

### Performance findings (2026-10-20)

Local Postgres: every statement < 1 ms and a ₹0 turn is 25–60 ms, so the production `db=1000–1700 ms` is **network round trips**, not slow queries: a v3 turn ran
**36–64 statements** (≈ 40 ms × 40 over Railway's public proxy). What changed:
* `TURN` block: new `DB` line (`DB   : 38 statements 14ms · slowest: 312ms SELECT … FROM … | … · repeated: SELECT messages×6, …`) and `TURN_TRACE_SQL=1` (every statement, in order).
* The COST line's 5–7 ledger queries (`cost_summary`) ran BEFORE the reply was sent, for a log line — now `await turn_trace.emit_pending()` evaluates them after the send, and not at all when `TURN_TRACE_ENABLED=false`.
* Three `db.refresh()` round trips removed (`save_message`, `usage_service`, `lead_service`) — the INSERT/UPDATE already had the values (−3 to −5 statements per turn).
* Catalogue snapshot: stale-while-revalidate (`catalogue_snapshot.get_stale/refresh_soon`): past its 60 s TTL the next message is answered from the old rows while one background task reloads them — the
  ~1–2 s reload of a 987-product / 6,301-variant shop no longer lands on a customer's turn (a catalogue WRITE still drops the snapshot at once).
* Migration `0074`: `orders` partial indexes over the two payment-open states (by conversation and by phone) + `orders(conversation_id)` (had none), `messages(conversation_id,id)`,
  `llm_usage(conversation_id,id)`, `payment_proofs(conversation_id,status)`, `leads(client_id,phone_number)`.
* Still the biggest lever, outside the code: run the backend and Postgres on Railway's **private network** (`DATABASE_URL` = `*.railway.internal`, same region) — the public proxy
  adds tens of ms to each of the ~35 statements left per turn.


## Round 7 — v1.6 (conv=60: ORD-2026-0077 / 0078 / 0068)

Planner prompt `agent_planner_v1.6`. Migration `0075` (`conversations.pending_proof_image_id / image_batch_reply_at / last_payment_instruction_at`,
`payment_proofs.reclassified_by_customer`). Units `tests/test_v16_units.py`, replay `tests/replay/test_payment_images_v16.py`, scenarios `69`–`72`.

| # | Bug | Fix | Where |
|---|---|---|---|
| 1 | Dark-mode UPI screenshots taken for product photos ("no_receipt_text" ⇒ product, 0.85) | Three outcomes `payment_proof / product_photo / unknown`. `product_photo` needs POSITIVE evidence (catalogue watermark, perceptual-hash match, enquiry caption, or vision ≥ 0.85 — 0.92 inside the payment window). Watermark probe + OCR + vision run **concurrently** (vision cancelled when OCR alone is decisive). Prior: an order is `pending_payment` and `Conversation.last_payment_instruction_at` ≤ `PAYMENT_PROOF_PRIOR_MINUTES` ⇒ weaker payment evidence is enough. Unknown ⇒ `[Payment screenshot][Product photo]`, first image id kept in `pending_proof_image_id`; images within `PAYMENT_IMAGE_BATCH_SECONDS` of the last image reply (and only if the customer's previous message was an image too) get no reply. New signals: the shop's UPI id on the picture (+4), bank-SMS wording, dark-mode inversion before OCR. | `payment_proof_classifier.py`, `payment_inbound.py`, `image_fingerprint.py` |
| 2 | "This is payment photo" went to the planner | `pvs.is_payment_affirmation` (5 languages) ⇒ unfiled images of the last `PAYMENT_RECLASSIFY_WINDOW_MINUTES` become proofs (`reclassified_by_customer`, audit `proof_reclassified_by_customer`, `PROOF_RECLASSIFIED` log); order from the amount, else asked; no image ⇒ "Please send the payment screenshot". Runs in `payment_inbound.pre_process`, before any planner. | `payment_verification_service.py`, `payment_inbound._handle_reclassification` |
| 3 | `₹8,548` read as `[8.0, 7.0]` | `payment_amounts.py`: blanks dates/times/phones/UTR/ref/percent/UPI ids/masked accounts first, then strong (next to ₹ / Rs / INR / `/-`, OCR glyph slips `% € T`) vs weak amounts; Indian + international grouping, OCR-spaced groups. Auto-attach when exactly one unpaid order matches (±₹1). | `payment_amounts.py` |
| 4 | Picker listed `payment_submitted`; stale buttons; no status | Picker = `pending_payment` only (newest first), > 3 ⇒ WhatsApp list; a tap whose image is already filed ⇒ "Your last screenshot is linked to X. Send a separate screenshot for Y 📸"; "Please confirm my payments" ⇒ per-order status (not paid / waiting for verification / confirmed), deterministic. | `payment_inbound.py` |
| 5 | Groq 429 (8k TPM, ~4.8k-token planner call) | `LLM_MODEL_PLANNER_FALLBACK` tried immediately; `llm_rate_limiter` (per-model sliding window from `LLM_RATE_LIMITS`, queue ≤ 2 s, `x-ratelimit-*` / `retry-after` learned from the httpx response hook); one shared keep-alive `AsyncOpenAI`; prompt 4,166 → ≈ 2,600 tokens (system v1.6 rewrite, grouped union schema, 4 history turns); bare yes/no/number with the planner down is answered against the open question (`agent_fallback`), never "didn't quite get that". TURN block: `PLANNER: model_used= fallback_reason= prompt_tokens= cached_tokens=`. | `agent_planner.call_with_fallback`, `llm_rate_limiter.py`, `llm_client.get_client`, `agent_fallback.py` |
| 6 | "1000" for stock 1 appended an unrelated catalogue; "1" then opened product 1 | `QTY_EXCEEDS_STOCK` ⇒ "Only n of X …" + `[Take n][Remove item][See similar]`, `last_clarify` stays `quantity`; the invariant `unavailable_without_options` only appends SAME-CATEGORY alternatives (`like`), never when a numeric question is open (`product_list_widget` refuses to replace one); qty < 1 ⇒ `QTY_MIN` ("Quantity must be at least 1."), qty > `AGENT_QTY_SANE_MAX` ⇒ `QTY_CONFIRM` ("Did you mean 150?" [Yes][No]). | `agent_executor`, `agent_renderer`, `agent_invariants`, `agent_fast_paths` |
| 7 | Unsupported message type silent | `webhook._notify_unsupported`: `unsupported_message` template (5 languages), once per sender per `UNSUPPORTED_NOTICE_MINUTES` (document, video, location, contacts, order, unsupported; reactions stay silent). | `routers/webhook.py` |
| 8 | Cheap paths | `agent_catalogue_words.parse`: a category word ⇒ `browse_catalog` (`fast=category`); nouns + colours + chatter ⇒ `search_catalog` (`fast=catalogue_search`) or the one product that carries all the words (`fast=product_name`). Order intent, digits, sizes, negations, unknown words, a lone colour ⇒ planner. Hindi-roman markers `dekh dekho deke dikhaiye de` added. | `agent_catalogue_words.py`, `language_service.py` |
| 9 | "Nice photo!" ×3, no search | Photo ⇒ watermark/hash match ⇒ product card; else the vision description (reused from the payment classifier) ⇒ `search_catalog` ⇒ 3–5 closest products; no match ⇒ ONE `image_unknown` question; the same reply is never repeated within 5 min. "Product photo" tap looks at the stored earlier picture. | `agent_v3._image_outcome` |

Trace: image / payment short-circuits now report `stage=<browsing|collecting|confirming> pending_orders=[…]` (never `payment→payment`); `PROOF_CLASSIFY` logs `kind conf decided_by ocr_text_len vision_label vision_conf prior signals amounts`.


## Round 8 — v1.7 (conv=60: ORD-2026-0079 — latency, the silent tap, the black "screenshot", editing a just-placed order)

Planner prompt `agent_planner_v1.7` (rule 12: a message about a product in the cart / a pending order is about THAT product; no catalogue list outside browse/search).
Migration `0076` (composite indexes + one-time `messages.channel` backfill). Units `tests/test_v17_units.py`; replay `tests/replay/test_v17_flows.py`;
scenarios `tests/scenarios/test_order_edit_v17.py`, `tests/scenarios/test_perf_v17.py`; OCR fixtures `tests/fixtures/ocr/`.

### Latency — what the TURN block now tells you
`TIME … loop_lag=0/1/3ms` = event-loop lag at the start of the turn / at its end / the worst in between (`loop_monitor.measure`, a zero-length hop through the loop).
**Near 0 while `db=` is large ⇒ the database really is slow. A big `loop_lag` ⇒ something synchronous held the loop** — the `db` span is wall clock from send to reply, so a
blocked loop makes every statement look slow (that is what "INSERT llm_usage 4.2 s" was). The watchdog (`loop_monitor.Watchdog`, started in `main.lifespan`; `LOOP_WATCHDOG=0` turns it off)
logs `LOOP_BLOCKED for 312ms+ — the loop thread is currently executing: <stack>` while the loop is stuck (the stack names the line) and `LOOP_STALL total=…ms` when it comes back.
In development asyncio debug mode is on as well (`ASYNCIO_DEBUG=0` to disable): "Executing <Task …> took 0.35 seconds".

| Blocking call found on the loop | Fix |
|---|---|
| `storage_service.get_watermarked_image_url` (`async def`, one per product photo sent): sync boto3 `head_object` + `put_object` (R2 round trips) and the Pillow watermark | all three in `asyncio.to_thread`; a per-process URL cache (`_watermark_url_cache`) makes later sends of a product cost no R2 call. A/B with R2 at 300 ms/call, 3 photos: **1,961 ms with a 902 ms loop stall → 736 ms / 52 ms → 0 ms** on later sends |
| `image_fingerprint`: `dhash` decoded full-size photos on the loop; `build_index` hashed ≤ 200 catalogue photos there (benchmark: 200 × 12 MP = **16.8 s of loop time, 42 stalls > 100 ms**) and was started from inside a turn every hour | `Image.draft` (JPEG decoded at 1/8 scale), a 2-worker `_HASH_POOL`, `match_async`; the index is built by `ensure_index` (catalogue snapshot load + startup warm-up), rebuilt only when the set of `(sku, image_url)` pairs changes — `match()` never builds |
| `Catalogue.load` rebuilt ~7,000 view objects + the fuzzy vocabulary per message; `load_stock` materialised ~7,000 `Row`s (ORM path, GC pauses ≈ 80 ms) | `Catalogue` cached per tenant while the snapshot rows and every stock / reservation number are unchanged (`_catalogue_cache`); stock comes back as ONE `jsonb_object_agg` document; the O(products × reservations) sum became a dict |
| `PUT /catalogue/upload-image`: `upload_product_image` (Pillow + boto3) inside an `async` route | `asyncio.to_thread` |
| `scheduler._weekly_quality_job`: `subprocess.run(timeout=120)` inside a coroutine | `asyncio.to_thread` |
| APScheduler default `misfire_grace_time=1 s` ("Run time of job … was missed by 0:00:03") | grace 60 s + `coalesce=True` |

DB: `messages.channel` is set when the row is written (`conversation_service._loaded_channel`); the per-turn `UPDATE messages SET channel … WHERE channel IS NULL` is gone (migration `0076` fixed old rows once).
Composite indexes `orders(client_id, conversation_id, status)`, `conversation_cart(conversation_id, status)`, `product_variants(client_id, is_active)`, `products(client_id, is_active)`
(`messages(conversation_id, created_at)`, `llm_usage(conversation_id, id)`, `stock_reservations(product_id, status)` already existed). Pool: `DB_POOL_SIZE=10`, `DB_MAX_OVERFLOW=20`, `pre_ping`;
`db.release_connection(session)` (a commit — rows stay readable, `expire_on_commit=False`) runs before the planner call, the vision / OCR classification and the WhatsApp send, so a pooled connection is
never held "idle in transaction" across a multi-second network call (6 customers mid-LLM-call: `pg_stat_activity` showed 6 × `idle in transaction` in v1.6, 0 in v1.7). The TURN block was already ONE logger call and still is (the `TURN_TRACE_SQL=1` statement list is now part of it). The torn line `COST turn=₹0.00INFO: …` is a second writer on the same terminal: uvicorn's access log goes to **stdout**, the app's logging to **stderr**, and `cost_log.print_report` used `print()` — it now logs. If it still shows, run the server with `--no-access-log` or one stream.
Perf guard: `tests/scenarios/test_perf_v17.py` — 15 fast-path turns on a 1,000-product / 6,000-variant shop: p95 < 300 ms, loop lag < 50 ms, no channel UPDATE, zero planner calls.

### Behaviour
| # | Bug | Fix |
|---|---|---|
| 1 | "Payment screenshot" tap → no reply (`Image burst … no reply of its own`) | `_claim_image_reply` only ever suppresses IMAGE messages; taps and typed answers are always answered |
| 2 | Silent turns | `PipelineResult.skip_reason`; `ensure_reply` accepts `SILENCE_REASONS` (paused, blocked, not live, muted, render_error) and `DEDUPE_REASONS` (image_burst / image_repeat / ack_throttled) **only while the bot's last reply is ≤ `NO_REPLY_DEDUPE_SECONDS` (10 s) old**; anything else logs `no_reply_sent` and sends the stage-aware fallback. A typed "paid" under review is answered every time |
| 3 | Order edited after confirming ("1 black and 1gray") | `agent_order_edit.py` — see below |
| 4 | Black frame → amounts `[7.0, 2.0]` | `is_blank_image` (brightest pixel ≤ `IMAGE_BLANK_MAX_LUMA`, or no contrast): no OCR, no vision, "too dark to read — send it again"; `payment_amounts.looks_like_text` (≥ 2 words of 3+ letters) gates WEAK amounts, `MIN_AMOUNT=10`; vision returns `amount` as a second source, adopted only when it equals an unpaid order's total. The stored image of conv=60 was a black frame — fixture `tests/fixtures/ocr/black_frame_receipt.{jpg,ocr.txt}` |
| 5 | Picker asked even when obvious | `pick_order_by_recency`: newest unpaid order's instructions ≤ 30 min old AND every other unpaid order's > 6 h old ⇒ attach + "Wrong order? [Change order]" (withdraws the proof and asks) |
| 6 | `vision_label=unknown vision_conf=1.0` | `vision_problem` validates the JSON; a mismatch logs `VISION_SCHEMA_MISMATCH raw=…`, retries once with a strict prompt, then clamps "unknown" to ≤ 0.5 |
| 7 | `ask_clarify slot=product` dumped the catalogue | renderer: sold-out / unknown product ⇒ its own category only; planner candidates ⇒ only those; nothing to anchor ⇒ one plain question |
| 8 | Greeting / category not ₹0 | `is_greeting_message` (greeting + ≤ 3 filler words: "hello buddy", "hi bro", "hey there", "hello ji"); intent words (want / need / chahiye / joiye / buy) are stripped before the category lookup; `1gray` / `black2` are split by `agent_multivariant._unglue` |
| 9 | Approval notification invisible | `notify_payment_approved order=… sent=true` (also `_rejected` / `_cancelled`) |

**Editing a placed order** (`agent_order_edit.py`, ₹0, no planner): editable = `pending_payment`, no live payment proof, created ≤ `ORDER_EDIT_WINDOW_MINUTES` (30) ago. The message is read by `agent_multivariant`
against THAT order's product; missing attributes (the size) are inherited from the existing line; a colour without that size asks for a size from its own options; a colour the product does not come in is reported
with the real options; stock counts the order's own reservation as available. Proposal "Update order #… to: 1× Black/XL, 1× Grey/XL — ₹1,758?" `[Update order | Keep as is]` (typed yes / no works;
Instagram gets the hint). **Update edits the same order in place** (decision: the number the customer holds stays valid and no second payment request exists for it): row locked, line items replaced, total
recalculated, reservations swapped (`stock_reservation_service.replace_lines`), audit `order_edited_by_customer` (before / after / totals), fresh payment instructions + QR. A screenshot on file, or a status past
`pending_payment`, is never edited: "can't change it here — new order for …?" `[New order]` adds the asked-for options to a new cart.
