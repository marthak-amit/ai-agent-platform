# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

WhatsApp + Instagram + Website chat AI agent SaaS platform targeting the Indian market. Clients self-serve their own AI agent setup. Conceptually similar to TailorTalk.ai. Payments via Razorpay, messaging via Meta Cloud API.

**AI providers — this is not "Gemini" despite legacy naming:** conversational replies, intent classification, and product-image recognition all run on **Groq**. Model ids are NOT hardcoded: they come from `LLM_MODEL_REPLY` / `LLM_MODEL_CLASSIFIER` / `LLM_MODEL_VISION` / `LLM_MODEL_STT` (defaults in `app/config.py`; verify with `python scripts/check_llm_models.py`, which lists Groq's `/models`). All chat calls go through `app/services/llm_client.py` (reasoning-model handling, final-content-only parsing, JSON repair); `app/services/llm_health.py` holds the failure counter, circuit breaker and startup model check (see `gemini_service.py`, `conversation_flow.py`, `vision_service.py`). Google Gemini (`GEMINI_API_KEY`, `app/services/photo_enhancement_service.py`) is used only for the AI photo-enhancement feature on catalogue images — nothing else. The `gemini_service.py` filename, the `Client.gemini_system_prompt` column, and the `role='model'` convention are pre-Groq-migration names that were never renamed; don't infer from them that replies go through Gemini.

## Commands

### Backend
```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Run dev server
uvicorn app.main:app --reload --port 8000

# Run all tests
pytest

# Run a single test
pytest tests/test_<module>.py::test_<function> -v

# Lint
ruff check .
ruff format .
```

### Frontend
```bash
cd frontend
npm install

# Dev server
npm run dev

# Build
npm run build

# Lint
npm run lint
```

## Architecture

### Backend (`backend/`)

FastAPI application. Entry point is `app/main.py`. All routes use `async/await` — no sync endpoint handlers.

Key layers:
- **Routers** (`app/routers/`) — one file per channel/feature (e.g. `webhook.py`, `whatsapp.py`, `instagram.py`, `payment.py`)
- **Services** (`app/services/`) — business logic; one service per domain (e.g. `gemini_service.py`, `conversation_service.py`, `lead_service.py`)
- **Models** (`app/models/`) — SQLAlchemy ORM models for PostgreSQL
- **Schemas** (`app/schemas/`) — Pydantic models for all request/response validation; never use raw dicts at API boundaries
- **DB** (`app/db.py`) — async SQLAlchemy engine + session factory

### Frontend (`frontend/`)

React + Tailwind CSS dashboard for client self-serve setup. Communicates only with the FastAPI backend.

### Message Flow

```
Meta Cloud API webhook → /webhook (FastAPI)
  → parse sender + message
  → conversation_service: load/create conversation in DB
  → gemini_service: build prompt + call Groq API (see naming note above)
  → whatsapp/instagram sender: reply via Meta Cloud API
  → lead_service: tag lead based on conversation signals
```

## Build Order

Features must be built in this sequence (each depends on the previous):

1. Webhook handler (FastAPI)
2. Gemini AI integration
3. WhatsApp reply sender
4. Conversation database
5. Lead tagger
6. Instagram webhook
7. Website widget
8. Payment handler (Razorpay)
9. Follow-up engine
10. React dashboard

## Coding Rules

- **Async everywhere**: all FastAPI route handlers and service calls must be `async def`
- **Env vars only**: all secrets and API keys loaded from `.env` via `python-dotenv`; never hardcode
- **Docstrings required**: every function and class must have a docstring
- **Pydantic for I/O**: define request/response schemas in `app/schemas/`; use them on every endpoint
- **One test per function**: each new function gets a corresponding test in `tests/`
- **No ORM attributes after a rollback**: `db.rollback()` / a failed transaction expires every instance, and an async lazy load then raises `MissingGreenlet` (HTTP 500). Exceptions that cross a transaction boundary carry plain values (`ManualReviewRequired.payload()`), never ORM rows — guarded by `test_no_custom_exception_stores_an_orm_object`

## Environment Variables

Store in `backend/.env` (never commit):
```
DATABASE_URL=
GROQ_API_KEY=
GEMINI_API_KEY=
META_APP_ID=
META_APP_SECRET=
META_VERIFY_TOKEN=
META_OAUTH_REDIRECT_URI=
META_WHATSAPP_CONFIG_ID=
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
INSTAGRAM_ACCESS_TOKEN=
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
PUBLIC_SHOP_BASE_URL=
LLM_MODEL_REPLY=
LLM_MODEL_CLASSIFIER=
LLM_MODEL_VISION=
AGENT_V3_CLIENT_IDS=1    # optional: ids agent v3 (cart + planner + validator) is on for (see "Agent v3")
LLM_MODEL_PLANNER=       # optional: defaults to openai/gpt-oss-120b
ROUTER_V2_CLIENT_IDS=1   # optional: ids the LLM router is on for (see "LLM Intent Router")
GOOGLE_CLIENT_ID=        # optional: "Continue with Google" (see "Dashboard-First Onboarding")
TRIAL_DAYS=7             # optional: free-trial length, starts at Go live
TRIAL_CONVERSATION_LIMIT=100   # optional: conversations included in the trial
SIMULATOR_DAILY_MESSAGE_LIMIT=0   # optional: 0 = unlimited in-dashboard test chat
TURN_TRACE_ENABLED=true  # optional: one "TURN conv=… USER/REPLY/WHY" log block per customer message
AGENT_UPSELL_MAX_CART_TOTAL=10000   # optional: no summary upsell once the cart is worth more (₹)
TURN_TRACE_SQL=          # optional: 1 = also list every SQL statement of a turn (round-trip diagnosis; the DB line is always on)
ADMIN_SECRET_KEY=        # legacy shared key for scripts (see "Admin Console"); the console itself uses operator accounts
SECRET_KEY=              # signs tenant AND admin JWTs and derives the admin 2FA encryption key
```

- `WHATSAPP_ACCESS_TOKEN`/`WHATSAPP_PHONE_NUMBER_ID` are the **global fallback** used only when a client has no credentials of its own on `Client.whatsapp_access_token`/`Client.whatsapp_phone_number_id`. Every WhatsApp send in `app/services/outbound.py` prefers the per-client values when the caller has the `Client` row loaded — see the module docstring there for why that resolution happens via already-loaded objects rather than a DB lookup inside the send path.
- **Instagram tokens**: per-client long-lived tokens live on `Client.instagram_access_token`. `instagram_token_service.py` verifies each at startup (`graph.instagram.com/me`, logs VALID/INVALID per `client_id`, never token values; verdicts feed `/health`) and a weekly scheduler job (`refresh_instagram_tokens`, Mon 03:00 IST) refreshes them via `graph.instagram.com/refresh_access_token` before the 60-day expiry. Global `INSTAGRAM_ACCESS_TOKEN` is a fallback that `instagram_service` sends and `vision_service.download_instagram_media` still use.
- `META_APP_ID`/`META_OAUTH_REDIRECT_URI` back the self-serve Instagram OAuth connect flow (`app/routers/integrations.py`).
- `META_WHATSAPP_CONFIG_ID` is the WhatsApp **Embedded Signup** Configuration ID (Meta App Dashboard → WhatsApp → Embedded Signup → Configurations) that powers the self-serve "Connect WhatsApp" button (`app/routers/whatsapp_signup.py`). Unset = that button is disabled and clients fall back to manually pasting Phone Number ID / Access Token. Getting a client fully live also requires them to appear as a Meta test user or the app to have passed Review for `whatsapp_business_management`/`whatsapp_business_messaging` — see Meta Business Settings → Roles → Test Users during development.

## Language Mirroring (reply in the customer's language and script)

The customer is answered in the language AND script they wrote — pure Gujarati script in → pure Gujarati script out
(`શું તમે ચણિયા-ચોળી પણ વેચી રહ્યા છો?` → `હા, અમે ચણિયા ચોળી વેચીએ છીએ`), Hinglish in → Hinglish out, Tamil in → Tamil out.

- **Detection** — `app/services/language_service.py`. `detect(text, previous)` → `Detection(lang, confident)`; script first (Gujarati, Devanagari
  → Hindi *or* Marathi, Bengali, Tamil, Telugu, Kannada, Malayalam, Gurmukhi, Odia, Arabic → Urdu), then Roman-script keywords
  (Hindi / Gujarati / Marathi), then English. A message with no signal (name, address, `ok`, `2`, `COD`, emoji) is *not confident*
  and keeps the previous language. `normalize_language()` folds legacy values (`hinglish`) and unknown codes (→ `english`).
  Codes: `english hindi_roman hindi_devanagari gujarati_roman gujarati_script marathi marathi_roman bengali tamil telugu kannada
  malayalam punjabi odia urdu`.
- **Sync per turn** — `order_pipeline.sync_conversation_language()` runs first in `_handle_inbound_message_core`, so every template
  of the turn (including early-return guards) reads the CURRENT message's language from `conv.last_customer_language`. While a
  name/address is being collected only a different *script* switches language (a Roman street name must not).
- **Replies** — five languages have hand-written templates (`language_templates.TEMPLATES`: English, Hindi Roman/Devanagari,
  Gujarati Roman/script). The LLM reply path gets `build_language_rule(lang)` (pure language, same script, no English words).
- **Last-mile guarantee** — `reply_localizer.py`, applied in `handle_inbound_message` (and in `channel_sender` for system/bot
  messages). A reply in the wrong script, an English leftover inside a native-script reply, or an English reply to a Hinglish
  customer is translated through the LLM (`purpose="translate"`); every digit, price, SKU, URL and brand word must survive and
  the result must be in the right script, otherwise the original text is sent. Catalogue names the seller wrote in English and
  the customer's own typed words are allowed (`allowed_words_from`). Button labels of fixed actions are translated; product /
  colour / size buttons keep the catalogue spelling because the tap is matched back against it. The saved history keeps the
  engine's own text (the stage machine recognises "the agent just asked X" from it).
- **Understanding replies** — the keyword (non-LLM) flow reads `command_words.py` (yes / no / cancel / continue / switch /
  same / different / change / order-status / COD / UPI / greeting / thanks in all 15 languages) and `indic_vocab.py` (script-safe
  `tokenize`, native product words → English catalogue words, colour aliases). Never use `re` `\w` on customer text: it deletes Indic
  vowel signs — use `indic_vocab.tokenize` / `command_words.normalize`.
- **Voice notes** are transcribed *before* the pipeline (`_transcribe_voice_note_early`, Whisper language auto-detect) and then behave
  like typed text; the reply is prefixed with `_what was heard_`.
- **Adding a language**: add it to `language_service.LANGUAGES` + script ranges, words to `command_words` / `indic_vocab`, and (optionally)
  a template dict — without one the localizer translates the English text.
- **Simulator** — `backend/tests/sim/` plays thousands of multilingual customers through the real app (see its README):
  `python -m tests.sim.run_sim --n 5000 --concurrency 10`. Run it after touching the pipeline, templates or language code.

## Turn Trace (what came in, what went out, why)

Every customer message ends with ONE log block from `app/services/turn_trace.py` (started/finished in `order_pipeline.handle_inbound_message`):
`TURN conv=60 stage=order_collection→awaiting_order_switch_confirm 19ms` · `USER : '<message>'` · `REPLY: '<text>' [buttons: …] [images: n]` · `WHY  :` followed by
every INFO+ `app.*` log line that turn emitted (`ROUTER …`, `ROUTE …`, guard hits such as "Mid-order product name", "Slot filled", warnings) prefixed with the source module.
Collection is per-turn via a ContextVar, so concurrent customers never mix. Customer text appears as in `CONV-TRANSCRIPT`; `TURN_TRACE_ENABLED=false` turns it off.
Grep `TURN conv=60` to follow one conversation. Tests: `tests/test_turn_trace.py`, plus a real-webhook assertion in `tests/replay/test_address_buttons_and_mid_order_name.py`.

## Side Questions & Social Messages (answer, then resume)

A customer in the middle of an order asks "ketla diwas ma return kari sakai?", "COD available?", "delivery charge?", says "thanks", "ok", "nice", "I'll think about it", "show me something else", "talk to someone" — none of that answers the open question. `app/services/side_questions.py` handles it for **router and keyword shops alike**, at ₹0, before the router / slot machine (`order_pipeline._handle_inbound_message_core`, right after the catalogue-link short-circuit).

- **Detect** — `detect_topic(text, next_slot=…)`: multilingual lexicons (15 languages) for `returns, cod, payment_methods, delivery_time, delivery_charge, delivery_area, warranty, quality, trust, location, contact, size_guide, bulk, discount, change_product, why_details, human, thanks, bye, positive, ack, decline, think_later`. Never fires for cancel wording / order-status questions (their own flows), a bare option ("COD", "Pink"), or a name/address (free-text slots need a clear question; `positive` is excluded there because "Sundar" is a name). A bare `no`/`ok`/👍 only counts at the colour/size/quantity/name/payment question (and at the address question when no saved address is on offer).
- **Answer from facts only** — COD / payment methods come from `Client.accepts_cod/accepts_upi/cod_limit`, delivery time from `delivery_service` (product > client min/max), location from `business_address`. Returns / warranty / delivery charges / contact / size guide / bulk / trust: the seller's approved **knowledge-base** entry if one matches, otherwise the honest `rt_handoff_offer` ("I don't have a confirmed answer… type 'talk to someone'"). **A policy is never invented** — sellers should add a "Return policy" (and "Delivery charges") entry in the Knowledge Base. Templates: `sq_*` keys (5 hand-written languages, the rest via `reply_localizer`).
- **Resume, never mutate** — in `order_collection` the open slot question is re-asked (with its buttons) after the answer; at the confirm step the order summary comes back; with a product on offer the "Would you like to order X?" line follows. `bye` / `think_later` ("wait", "I'll think") are not nagged with the question. State is never written (the reply is saved as an ordinary assistant message, so the stage machine still recognises the question).
- **Other deterministic actions** — `human` → same hand-off as the router (bot paused, "team will reply"); `cancel` wording while an order is being filled → draft cancelled without any LLM (works during an LLM outage); `change_product` → catalogue link + the open question.
- **Skipped** for stages with a pending yes/no prompt (`awaiting_switch_confirm`, `awaiting_afc_switch_confirm`, …) and while `interrupted_sku` is set.
- **Tests**: `tests/test_side_questions.py` (detection tables, answers), `tests/replay/test_side_questions.py` (real webhook → Postgres: the screenshot scenario, KB answers, full order with interruptions), simulator scenario `sc_side_questions`.

## Irrelevant-Message Guard (stop paying for noise)

A customer who only sends noise (😂😂😂, "???", "hahaha", off-topic questions) must not keep costing LLM calls. `app/services/spam_guard.py` + `order_pipeline.run_spam_guard` (runs right after the human-takeover guard, BEFORE the router / any LLM).

- **What counts** — one counter, `Conversation.off_topic_count` = CONSECUTIVE irrelevant messages: emoji/symbol-only and laughter (`classify_text` → `junk`, ₹0) plus whatever the router (`out_of_scope`) or the keyword pipeline's off-topic check flags. Thanks/acknowledgement emojis (👍 🙏 ❤️ 😊 …) are `ack`: answered as before, count nothing. A real on-topic message resets the counter (router: any confident non-smalltalk action; keyword path: judged on-topic or a filled slot).
- **Escalation** — limit `IRRELEVANT_STREAK_LIMIT` (3; per client `Client.off_topic_threshold`). Below it: a fixed template (`rt_junk_nudge` for noise, `rt_out_of_scope` for off-topic) — never model text. At the limit: ONE final notice (`rt_spam_final`: what we can help with, "pause replies for ~N minutes", send a SKU or "talk to someone", catalogue link) and the conversation is **muted** for `IRRELEVANT_MUTE_MINUTES` (15; 0 = never mute) via `Conversation.spam_muted_until` (migration 0069).
- **While muted** — typed free text is saved to the transcript but NOT answered: no router call, no LLM, no reply. Still processed: exact SKUs, bare numbers / yes / no / COD / UPI, catalogue "Order" pastes (`message_batcher.is_exact_reference`), "talk to someone" (`side_questions` `human`), button taps, images (payment proof) and voice notes. Ends by time (the next message restarts from zero) or when staff resume the bot (`conversation_control.resume_bot`). An exact SKU / menu number does NOT lift the mute.
- **Never during an order** — in order stages (`order_collection, awaiting_final_confirmation, payment, awaiting_switch_confirm`) nothing is muted (a name/address answer must still work): emoji noise is silently ignored (no reply, no LLM, no strike); an off-topic question at the limit gets the "talk to someone" offer instead of the mute.
- **Batching** — emoji noise / thanks emojis are never batched or waited for (`message_batcher.batchable_text`).
- **Tests**: `tests/test_spam_guard.py` (no DB: classification, counter, mute, the guard), `tests/replay/test_spam_guard.py` (real webhook → Postgres: emoji ×3, 4th message silent with zero LLM calls, SKU/human still work, mute expiry, router and keyword paths, thanks emojis, mid-order).

## Message Batching (one answer for a burst)

A customer types one request across several messages ("I want" · "to buy" · "banarasi silk sadi" · "if red colour is there then good"). The bot answers **once**, from the merged text. WhatsApp/Instagram never say "the customer is typing", so the signal is silence: `app/services/message_batcher.py`.

- **Leader / followers** — the first text of a burst (leader) opens a per-sender batch and waits until the sender has been quiet long enough; later texts (followers) append to the batch and return `{"status":"batched"}` at once. The quiet window depends on the LATEST fragment: a full sentence `MESSAGE_BATCH_WINDOW_SECONDS` (3.5), a one/two-word fragment ("pan", "choli", "bija") `MESSAGE_BATCH_SHORT_WINDOW_SECONDS` (5), an unfinished fragment ("I want", "mare bija", "mujhe", a trailing comma) `MESSAGE_BATCH_EXTENDED_WINDOW_SECONDS` (7). Caps: `MESSAGE_BATCH_MAX_WAIT_SECONDS` (15) and `MESSAGE_BATCH_MAX_MESSAGES` (8). `MESSAGE_BATCH_WINDOW_SECONDS=0` turns it off (the test suite does this in `tests/conftest.py`). Real typing gaps between fragments are 2–4 s, which is why the windows are this long.
- **Never batched** (`batchable_text()` / `eligible()`): anything while an order is in progress (name/address/colour/quantity/payment answers), an interrupted-SKU prompt, a paused bot, catalogue "Order" pastes, messages containing a SKU, bare yes/no/number/COD/UPI (these are also never *joined* to a waiting batch — they end its wait and run after it), button taps, images, voice notes. Only stored stages `greeting, product_inquiry, qualification, objection_handling, offer_making, completed, off_topic` batch. An open "reply with the number" menu does **not** block batching (its answers are bare numbers/SKUs, which are never batched).
- **Ordering & safety** — turns of one sender are serialised (one lock per sender; an unbatchable message arriving mid-wait ends the wait and runs *after* the batch); the leader takes its queue place before collecting, so nothing overtakes it; wamids of batched messages are remembered (10 min) so a Meta retry is not processed twice; the conversation row is refreshed after waiting. The merged text is what the pipeline sees and what is stored (one customer bubble, the leader's wamid).
- **Latency** — time already waited is credited against the artificial reply delay (`InboundContext.reply_delay_credit` → `order_pipeline._reply_delay_credit`), so a single message is not slower overall.
- **Wired in** `routers/webhook.py` (`receive_message` → `_run_turn`) and `routers/instagram.py` (typed DMs only). The website widget is request/response and is not batched. State is in-process like the rate limiter — single Railway instance; move the buffer to Redis when scaling out.
- **Tests**: `tests/test_message_batcher.py` (no DB: decisions, window, caps, ordering, dedupe, cleanup), `tests/replay/test_message_batching.py` (real webhook → Postgres: the four-fragment example, single message, separate bursts, order answers untouched, retry dedupe, batching off).

## Cancel Safety, Confusion & Draft Restore (conv=60 fixes)

Real transcript (Riya Sarees, 2026-10-17) showed a customer who said "sorry i cant get it!" lose their order, get "Sure, let's continue!" afterwards, be switched to another product silently, etc. Behaviour now (migration 0071; code in `app/services/order_recovery.py`, `router_actions.py`, `side_questions.py`):

- **Cancel safety** — router `cancel_order` cancels directly only with explicit wording (`order_recovery.has_explicit_cancel_keyword`: cancel / nahi chahiye / rehne do / don't want / stop order … in all scripts; negations and questions don't count) AND confidence ≥ `ROUTER_CANCEL_MIN_CONFIDENCE` (0.95). Otherwise `ask_cancel_confirmation`: "Cancel this order? (Yes / No)" + buttons (`cancel_yes`/`cancel_no`), stage and slots untouched, `Conversation.cancel_confirm_at` open for `CANCEL_CONFIRM_TTL_MINUTES`; the answer is read by `order_recovery.run_recovery_guard` (right after the spam guard, before every other yes/no guard). The keyword pipeline's CANCEL intent needs cancel wording too. A bare `cancel` at the summary / the side-question `cancel` (order_collection) stay deterministic.
- **Draft snapshot** — every cancel path (`handle_cancel_order`, cancel-in-payment guard, AFC cancel, CANCEL intent) calls `save_cancelled_draft` first → `Conversation.last_cancelled_draft` `{ts, fields{pending_product_sku, slots, cart_*}}`, restorable for `DRAFT_RESTORE_TTL_MINUTES` (30). "continue" / "yes" / "ye" / "haan" / "I want to continue this" (`is_resume_request`) while nothing else is open → `handle_restore_draft`: "Restored your order: <summary>" + the next open step (or the summary). A completed order clears the snapshot.
- **`confused`** (router action, prompt `router_v2.6`) — "can't get it", "what are you trying to say", "samjh nahi aaya", bare "?" (`order_recovery.is_confused`; deterministic via `side_questions` at ₹0, and the lexicon overrules a model verdict of cancel/smalltalk/general/out_of_scope). `handle_confused` changes nothing: the open question is re-sent simply (`resume_open_step`); a "different colours" split of the pinned product is abandoned (`cart_variant_mode="same"`, other products' cart lines kept).
- **No empty small talk** — `handle_free_text`: with a restorable draft / pinned product / open menu / last bot message a question, small talk resumes that step instead of acking; hints that are pure acks (<6 words, no question/number/link) are dropped for the menu fallback (thanks/bye/compliments exempt).
- **Quantity change** (`handle_change_slot`) with colour/size already chosen and past the split question → `cart_variant_mode="same"` → summary again. "different" asks *"Which colours …, and how many of each? (e.g. 1 Red, 1 Pink)"* (`ask_cart_colors`, parsed by the existing per-unit breakdown parser via `conversation_flow.breakdown_slot_open`), never "How many of Red?" first.
- **Search vs sticky SKU** — `handle_more_like_this` (router `search_catalog` with a "more/another/similar" request, and side-question `change_product`): next 3–5 items of the same search (`Conversation.last_search`), excluding pinned / last shown / recently ordered / already shown SKUs; list (not a card), never re-pins. A `show_product` verdict on a "more like that" message is overruled into a search.
- **Mid-order product switch** — another product's SKU/name/shop paste/NEW_PRODUCT mid-order is never a silent switch: card + `switch_offer3` with **Switch / Add both / Keep current** buttons (`switch_product`/`add_both`/`keep_current`). Resolved by `run_order_switch_confirm_guard`: switch (also a plain yes), keep (anything else), add both (collecting: queued as the summary's "Add X too"; at the summary: `_fold_into_cart_and_pin` immediately).
- **Discount** — `sq_discount` is one statement, the open question follows once. `Conversation.last_side_topic` makes "please" / a repeated plea within 10 min a `discount_followup`: fixed price said once more + cheaper items of the same category (not `start_order`).
- **Fuzzy matching** — query words shorter than 4 letters never match inside longer words ("got" ⊄ "Gota"); chatter is a stop word; "Did you mean A or B?" only for guesses with `catalogue_service.name_match_score` ≥ 0.6.
- **Summary** — no "We currently accept payments via UPI only." paragraph: the 💳 line says "UPI only (id)"; numbered options == buttons (`add_crosssell` button for "2️⃣ Add X too"; the cart summary offers Confirm / Cancel only). ETA: `delivery_service.get_delivery_time_str` and the order-status window share `order_delivery_days`; a same-day window prints one date (`router_actions.eta_window_line`).
- **High-value orders** — total > `Client.high_value_order_threshold` (default `HIGH_VALUE_ORDER_THRESHOLD` ₹50,000): the screenshot ack says it will be checked manually, `Order.requires_manual_review` is set, and `payment_verification_service.approve` raises `ManualReviewRequired` (HTTP 409 `high_value_confirmation_required` with `{order_number, total, threshold, message}`) until the call repeats with `confirm_high_value=true` (audited as `manual_review_cleared`); the dashboard shows a "Verify ₹X in your bank before approving" modal (`HighValueConfirmHost`) and re-POSTs.
- **Log hygiene** — the "bare affirmation" line logs what the customer typed (`order_pipeline._typed_text`), not the router's canonical SKU.
- **Tests**: `tests/test_order_recovery.py` (no DB), `tests/replay/test_chat_flow_hardening.py` + `tests/replay/fixtures/conv60_2026_10_17.json` (the real turns replayed). Simulator scenarios answer "yes" to the cancel question (`say_cancel`).

## Out-of-Scope Boundary (shop profile)

A customer asks "Ahmedabad ma su vatavaran chhe?", "who won the match?", "tell me a joke" — nothing to do with the business. The bot answers with a **fixed template, never model text**, and what counts as "about the business" comes from each shop's own data (no per-customer rules).

- **Shop profile** — `app/services/shop_context_service.py`, cached on `Client.shop_context` / `shop_context_keywords` / `shop_context_hash` / `shop_context_updated_at` (migration 0068). Built **by code, no LLM** from `business_type`, `business_description`, the active catalogue's categories + product names and the approved KB question titles: `sells: … | about: … | categories: … | products: … | shop topics: …` (≤900 chars, ≈200 tokens) plus a keyword set covering *every* product/category word.
- **Self-maintaining, lazily** — `ensure_fresh(db, client, products)` fingerprints business type/description + catalogue categories/names (the router already loads the active catalogue each turn, so this is free). A changed fingerprint rebuilds on the next message; **price/stock edits don't change it**. KB titles refresh via `invalidate(db, client_id)` (called by every `/knowledge` write endpoint) and a TTL (`SHOP_CONTEXT_TTL_SECONDS`, 1800). Writes go through a SAVEPOINT of the caller's session; every failure degrades to "no profile" = never refuse on scope.
- **Router shops** — the profile is added to the SHOP block of the prompt (`what the business offers: …`); new action **`out_of_scope`** (`router_v2.5`); `general_answer` is narrowed to advice about what *this* business sells. Engine rules (`router_actions.run_router_front_door`): an `out_of_scope` below `ROUTER_OUT_OF_SCOPE_THRESHOLD` (0.7), or one whose message shares a word with the shop profile (`shop_context_service.matches`), becomes a **clarifying question, not a refusal** (while a free-text slot — name/address/phone — is open, an `out_of_scope` verdict below 0.9 is treated as "it is the answer"). `handle_out_of_scope`: `rt_out_of_scope` (shop name + catalogue link), mid-order `rt_out_of_scope_midorder` + the open question re-asked (state untouched), and at the streak limit (3, see "Irrelevant-Message Guard") the ONE final notice goes out and the conversation is muted for a while (mid-order: `rt_out_of_scope_repeat` offers "talk to someone" instead — never muted). Strikes use `Conversation.off_topic_count`, shared with the keyword pipeline; an on-topic action or a fast-path message resets it, small talk doesn't.
- **Keyword-pipeline shops** — `conversation_flow.is_off_topic_message(..., shop_profile=…)`: a message sharing a word with the profile is on-topic with **no LLM call**; otherwise the classifier prompt includes what the business offers and the "Hindi/Gujarati in English letters" hint. Still fail-open (error / unsure → not off-topic, counted in `llm_health`). Reply = the existing `off_topic_idle` template.
- **Cost** — ≈ +100 tokens per router call (the profile line); rebuilds are ₹0; keyword-path shops save the classifier call whenever the profile matches.
- **Offline eval** — `tests/router_eval/common.eval_context` builds the profile from the catalogue snapshot with the same builder, so `golden_router.jsonl` (rows g052–g058) measures production behaviour.
- **Tests**: `tests/test_shop_context_service.py`, `tests/test_out_of_scope.py` (no DB); `tests/replay/test_out_of_scope.py` (real webhook → Postgres: the screenshot scenario, guards, strikes, mid-order, profile rebuild / KB invalidate, keyword path).

## Dashboard-First Onboarding (no forced wizard)

A new account lands on the **dashboard**, not a stepper. `app/services/account_lifecycle.py` owns the lifecycle; the UI is `frontend/src/pages/QuickStart.tsx`, `components/setup/*` and `context/SetupContext.tsx`.

- **Sign-up** — email+password (`business_name` is now optional) or **Continue with Google** (`POST /auth/google`, `GET /auth/google/config`; `google_auth_service.py` verifies the Google Identity Services ID token against Google's JWKS — signature, `aud == GOOGLE_CLIENT_ID`, `iss`, `exp`, `email_verified`). Same verified email = **one account**: Google is linked to the existing login (`User.google_sub`, `User.email_verified_at`); the match is case-insensitive. If that login's email was never verified (plain sign-up form), linking **revokes its password** so a squatter who registered someone else's address first cannot keep access. Accepting a team invite marks the email verified. A Google-only account has no password. **There is no Firebase**: the consent screen shows whatever the SellerTalk24 Google Cloud project's *OAuth consent screen* says (app name, logo, authorised domains, publishing status "In production", brand verification) — configure that in Cloud Console; nothing in code can brand it. Authorised JavaScript origins must list the dashboard origin(s).
- **Quick start** (`POST /onboarding/quick-start`, route `/welcome`) — business name + EN/HI/GU, then the dashboard. `ProtectedRoute` sends an Owner with `quick_start_completed=false` there; nothing else is forced. The first call seeds **3 sample products** (`Product.is_demo`: Cotton Kurti, Banarasi Silk Saree, Handmade Tote Bag, with variants) — `DELETE /onboarding/demo-products` removes them in one click.
- **Account status** (`Client.account_status`): `setup` (new; **unlimited simulator, no trial, no expiry, bot silent to real customers**) → `trial` (after Go live) / `active` (everything else, incl. every client that pre-dates migration 0070). `Client.bot_active=false` until Go live: `order_pipeline._billing_preflight` saves an inbound message but sends nothing; the website widget answers 503; `entitlement.automation_allowed` denies nudges/follow-ups/campaigns; IG comment auto-replies are off.
- **Checklist** (`GET /onboarding/checklist`, Owner only) — `add_products` (active **non-sample** product), `setup_agent` (`business_type`+`business_description`), `connect_whatsapp` (phone_number_id **and** token), `connect_instagram` (optional), `test_agent` (`agent_tested_at`), `go_live` (`bot_active`). **Derived from real state, never ticked by hand.** `hidden` once live; `next_key` = first unfinished required item (the empty states of Orders / Customers / Conversations point at it, `EmptyStateCta`). The wizard's step bodies live on as pages `/setup/agent|products|whatsapp|instagram` (`pages/Onboarding.tsx`, `section` prop); `/onboarding` redirects (the Instagram OAuth callback still returns there). `onboarding_step` / `PATCH /onboarding/progress` are legacy and gate nothing.
- **Go live** (`POST /onboarding/go-live`) — refuses with 409 `SETUP_INCOMPLETE` (+`missing`), `DEMO_PRODUCTS_PRESENT` (send `remove_demo_products:true`), `TRIAL_ALREADY_USED`. Idempotent. Row-locked.
- **Trial** (`trial_service.py`) — **starts at Go live** (`trial_started_at`/`trial_ends_at`), `TRIAL_DAYS` (7) or `TRIAL_CONVERSATION_LIMIT` (100) counted conversations (the same 24h-window rows paid plans count, written with no subscription), whichever first; then `GRACE_DAYS` of grace anchored on when the trial stopped; then EXPIRED. **One trial per WhatsApp `phone_number_id`**, ever (`trial_claims`, UNIQUE) — a second account on the same number must subscribe. A running plan goes live without a trial. Entitlement states `setup` / `trial` are added (`entitlement.py`); like every expiry, EXPIRED only restricts the bot when `SELLERTALK24_BILLING_ENFORCE=true`.
- **Chat simulator** (`/simulator/session|message|reset`, page `/sandbox`) — `simulator_service.run_turn` builds the same `InboundContext` the webhook builds and calls `handle_inbound_message`, so the **real router/keyword flow + order engine** answer, on the tenant's real catalogue/KB/payment settings; the `PipelineResult` is **returned as bubbles (buttons/lists nonce-encoded like production), never handed to a channel adapter**. The fake customer is `sandbox_<client_id>` on a conversation with `is_sandbox=true` (channel `whatsapp`). Fences (`sandbox_guard.py`): `outbound` refuses the sandbox phone; no conversation-window usage (billing/trial), no legacy usage counters, no LLM caps, no leads, no inbox notifications, no owner alerts; orders get `Order.is_sandbox` + their own `SIM-YYYY-NNNN` numbers, **reserve/deduct no stock**, send no invoice, and are excluded from the orders list/stats, customers, analytics, briefing, KB auto-learning, follow-ups and admin counts. `test_agent` flips after 3 customer turns while the LLM is healthy. `POST /simulator/reset` deletes the chat and everything it created. Unlimited by default (`SIMULATOR_DAILY_MESSAGE_LIMIT`), but **every turn spends real LLM tokens**. The legacy `/sandbox/*` endpoints (a separate lookalike pipeline) are no longer used by the UI.
- **Tests**: `tests/replay/test_dashboard_first_onboarding.py` (signup → quick start → checklist → go-live → trial/grace/cap → bot silent until live → Google linking), `tests/replay/test_simulator.py` (full COD/UPI order leaves no trace; router attribution; test-pass; reset; limits), `tests/test_google_auth_service.py` (real RSA-signed tokens), `tests/test_simulator_unit.py` (outbound refusal, tap mapping, sandbox order lifecycle).

## Admin Console (`/admin`)

Operator panel for running the whole platform: tenants, billing, AI cost, system health, operators and an audit trail. UI: `frontend/src/pages/admin/*` under the `/admin` routes of the existing React app (own `AdminAuthProvider`, own axios client `src/api/admin.ts`, token in `sessionStorage` — never the tenant `access_token`). Backend: `app/routers/admin_auth.py`, `admin_users.py`, `admin_panel.py`, plus the older `admin.py` / `admin_billing.py` (retrofitted).

- **First operator** (no UI path on purpose): `cd backend && python scripts/create_admin.py --email you@co.com --role superadmin` (hidden password prompt; `--reset` resets an existing one). New/reset accounts must change their password at first sign-in. Everything after that is done in the console (Operators page).
- **Auth** (`admin_auth_service.py`, `routers/admin_deps.py`): operators live in `admin_users`, separate from `users`/`clients`. JWT carries `aud="admin"` + `ver` (= `token_version`), so tenant tokens never work on `/admin` and vice versa; bumping `token_version` (logout, password/role/2FA change, deactivation) revokes every session. Password policy ≥12 chars with letters+digits; N=5 failures lock the account 15 min (`ADMIN_MAX_FAILED_LOGINS`, `ADMIN_LOCKOUT_MINUTES`); per-IP throttle; unknown email and wrong password are indistinguishable. Optional TOTP 2FA (RFC 6238, replay-guarded, secret Fernet-encrypted with a key derived from `SECRET_KEY` — rotating `SECRET_KEY` invalidates enrolled 2FA). Token lifetime `ADMIN_TOKEN_EXPIRE_MINUTES` (480).
- **RBAC** (`models/admin_user.py` `ADMIN_PERMISSIONS`): roles `superadmin` (all), `support` (clients, chats, impersonation, flags, suspend), `billing` (subscriptions, plans, payment events), `viewer` (read-only, no chats/audit). Every route declares `require_perm("…")`; denials are audited. The UI hides what a role can't do, the server enforces it. A superadmin can't change themself, and the last active superadmin can't be demoted/deactivated.
- **Legacy shared key**: `X-Admin-Key` (+ optional `X-Admin-User`) still authenticates scripts with all permissions, attributed to that name in the audit log. Disable with `ADMIN_API_KEY_ENABLED=false`; it is ignored outside `ENVIRONMENT=development` while it is still the default value. `GET /health/billing` and `/followup/*` accept it too.
- **Audit** (`admin_audit.py`, `admin_audit_log`): one row per mutation (`client.suspend|activate|flags|impersonate`, `plan.update`, `billing_plan.update`, `billing.*`, `admin_user.*`, `auth.*`), reading a customer transcript (`conversation.view`), sign-in failures and permission denials — actor, IP, reason, before/after, secrets redacted. `record()` joins the caller's transaction; `record_standalone()` persists failures. Caller IP = right-most `X-Forwarded-For` hop (`ADMIN_TRUST_PROXY_HEADERS`, default on for Railway).
- **Tenant data is read through `admin_panel_service.py`, which never returns credentials** (WhatsApp/Instagram/Razorpay tokens, bank details, password hashes are reported as "is set?" booleans). Impersonation mints a 30-minute tenant JWT for the owner (`impersonated_by` claim), needs a reason, and is refused for suspended tenants.
- **Overview revenue** = paid LIVE-mode `payment_orders` in the last 30 days (test/mock payments excluded) — not the legacy `plan_price_snapshot` sum.
- **Tests**: `tests/test_admin_panel.py` (no DB: policy, TOTP vectors, tokens, lockout, RBAC, "every `/admin` route needs credentials" sweep), `tests/replay/test_admin_panel_e2e.py` (real Postgres over HTTP: full lifecycle + audit rows). The replay billing tests pin the legacy key via `app.routers.admin_deps.get_settings`.

## Manual UPI Payment Verification

We never collect money: the customer pays the seller's UPI directly and sends a screenshot in chat; the seller approves/rejects it in the dashboard. **LLM = understand only, engine = act only** — every payment reply is a deterministic EN/HI/GU template (`pay_*` keys in `app/services/language_templates.py`).

- **Order states** (`order_state_machine.py`, `ORDER_STATUS_TRANSITIONS`): `pending_payment → payment_submitted` (customer image) · `payment_submitted → paid` (approve) · `payment_submitted → pending_payment` (reject) · `pending_payment|payment_submitted → cancelled`. Typing "paid" **never** confirms anything — it asks for the screenshot.
- **Stock**: reserved at `pending_payment` (`stock_reservations`, `stock_reservation_service.py`), deducted at `paid` in the same transaction as the status change, released at `cancelled`. New-order stock checks subtract active reservations.
- **Core**: `payment_verification_service.py` (approve/reject/cancel/expire; row-locked + idempotent, audit rows in `order_audit_log`). Inbound screenshots: `payment_inbound.py`, wrapped around `handle_inbound_message` (no vision model runs on proofs). Media is re-hosted in our storage (`media_service.py`) because Meta URLs expire.
- **Dashboard sends** go through `channel_sender.py` → `outbound.py` (24h window / opt-out / block enforced by `send_gate`). A dashboard send pauses the bot (`conversation_control.py`; `ai_enabled` stays the flag the pipeline reads, `bot_pause_source='human_send'` auto-resumes after `Client.bot_auto_resume_minutes` idle). Approve/reject templates are sent even while the bot is paused.
- **Log hygiene** (`app/log_redaction.py`): `httpx`/`httpcore` loggers are WARNING (they log full URLs incl. `access_token=`), and uvicorn's access log is filtered to redact `*token=` query values — this is how the SSE JWT in `/events/stream?token=` stays out of logs. Never log tokens/URLs carrying them. A startup IG `debug_token` failure sets a runtime flag (`channel_status.py`) that makes `outbound.ig_*` skip sends and `/health` report `"Instagram disconnected"`.
- **LLM failures are never silent**: classifier wrappers keep their safe defaults but call `llm_health.record_failure()` (ERROR log + counter → `llm_failures_last_hour` on `/health`). After `llm_breaker_threshold` consecutive hard failures (or a missing configured model at startup) the breaker opens: `/health` shows `LLM unavailable`, LLM calls short-circuit, and the order pipeline answers with the deterministic `llm_unavailable_rephrase` template instead of an AI error. A probe re-closes it once the LLM recovers. Empty `LLM_MODEL_VISION` or a missing vision model disables image matching (text-only fallback).
- **Realtime**: SSE (`GET /events/stream?token=`) from an in-process hub (`realtime_service.py`, single-instance like the rate limiter — swap `publish()` for Redis pub/sub when scaling out) with a DB-derived polling fallback (`GET /events/poll?since=`).
- **QR delivery**: the QR is only attached when its URL is publicly fetchable (`payment_verification_service.is_public_url` — absolute http(s), not localhost/private). With local storage and no `BACKEND_PUBLIC_URL`/R2, Meta can't download `/uploads/…`, so the QR is dropped, the "send the payment screenshot here" line goes into the text, and one WARNING says why. When a QR *is* sent, its caption carries that line; if the image is suppressed/rejected the adapter (`_whatsapp_adapter`) logs it and sends the line as text (`PipelineResult.image_fallback_text`). Every image send logs `Image sent` / `Image NOT sent` / `Image send error`.
- **Images while an order awaits payment are classified, not assumed** (`payment_proof_classifier.py`, `payment_inbound.py`): caption → catalogue SKU watermark → whole-image OCR (amount = an unpaid order's total ±₹1, UTR / 12-digit reference, "payment successful", GPay/PhonePe/Paytm) → vision model only when still unsure → `payment_proof | product_photo | other` + confidence (`PROOF_CLASSIFY` log line). Decisive proof → filed under the right order (several unpaid orders: the one whose total is on the receipt, else "Which order is this payment for?" buttons); decisive product photo → the product flow, never a proof; unsure (or OCR unavailable) → [Payment screenshot][Product photo] buttons. **"This is product not the payment"** (`pvs.is_payment_denial`, five languages) → `pvs.withdraw_last_proof`: proof `withdrawn` (row + `proof_withdrawn` audit kept, out of the pending queue), order back to `pending_payment`, never the payment instruction again. Tests: `tests/test_payment_inbound_gate.py`, `tests/replay/test_payment_proof_hardening.py`.
- **v1.6 (conv=60 ORD-2026-0077/0078)** — classification outcomes are `payment_proof / product_photo / unknown`; **no receipt text is never evidence of a product photo** (dark-mode screenshots): `product_photo` needs positive evidence (watermark, perceptual hash `image_fingerprint`, enquiry caption, vision ≥ 0.85 / 0.92 inside the `PAYMENT_PROOF_PRIOR_MINUTES` window after payment instructions). Watermark + OCR + vision run concurrently. Unknown ⇒ `[Payment screenshot][Product photo]` (first image id in `Conversation.pending_proof_image_id`; a burst of images within `PAYMENT_IMAGE_BATCH_SECONDS` gets one question). "This is payment photo" / "maine pay kar diya" / "paid" (`pvs.is_payment_affirmation`, 5 languages) files the unfiled images of the last 30 min as proofs (`PaymentProof.reclassified_by_customer`, audit `proof_reclassified_by_customer`) before any planner. Amounts come from `payment_amounts.py` (₹8,548 · 1,50,000 · "₹ 8 548"; phones/dates/UTR ignored); one unpaid order + matching amount ⇒ auto-attach. The picker lists only `pending_payment` orders (list message beyond 3); a stale picker tap says which order the last screenshot is linked to; "Please confirm my payments" ⇒ per-order status. Details: `backend/docs/agent_v3.md` "Round 7".
- **Switching product at the payment stage** (`run_switch_confirm_guard`, "yes"): the open `pending_payment` order is cancelled (reservation released), the old product's variant slots are dropped, and the customer is taken back to the **confirm step for the new product** — confirming creates a fresh order (number, amount, QR). If a screenshot is already under review (`payment_submitted`) or the new product is sold out, the switch is declined with an explanation and the order is untouched.
- A blocked payment step (no UPI id / product not found) is sent as plain text — never with Confirm & Pay buttons. Name/address answers never update the customer's language (`is_free_text_slot_answer`).
- **Permission**: `payment_verify` (Owner always; in the Manager preset). Payment settings (`/settings/payment`) are Owner-only.
- Dependency: `segno` (pure-python QR PNG for the `upi://pay?...&tn=Order{id}` code). Install with `python -m pip install -r requirements.txt` (not bare `pip`, which in a mixed-Python venv can target another interpreter).

## Image Recognition (Vision Service)

Both WhatsApp and Instagram support product image matching via `app/services/vision_service.py`.

**Channel differences — how images arrive:**
- **WhatsApp**: sends a `media_id` string. Two API calls needed:
  1. `GET graph.facebook.com/v21.0/{media_id}` → resolves to a CDN URL.
  2. Fetch the CDN URL with `Authorization: Bearer {whatsapp_access_token}`.
  Helper: `vision_service.download_whatsapp_media(media_id)`.
- **Instagram**: sends the CDN URL directly in `message.attachments[0].payload.url`.
  Single fetch with `Authorization: Bearer {instagram_access_token}`.
  Helper: `vision_service.download_instagram_media(image_url)`.

**Shared analysis function:**
`vision_service.analyze_product_image(image_source, catalogue_context)` accepts either
raw `bytes` (post-download) or a URL `str` (direct pass-through). Both are converted
to the `image_url` content block that Groq expects.

**Vision model:** `qwen/qwen3.8-27b` on Groq (verified to accept image input with `python scripts/check_vision_model.py`) (`meta-llama/llama-4-scout-17b-16e-instruct` was used previously but was deprecated by Groq on 2026-07-17 and now 404s — see `app/services/vision_service.py`).  
**Cost:** ~₹0.07 per image.

**Instagram image flow:**
```
Instagram DM (type="image") → /instagram webhook
  → _handle_image_dm()
  → vision_service.download_instagram_media(payload.url)
  → vision_service.analyze_product_image(bytes, catalogue)
  → instagram_service.send_dm() with vision reply
```

**Simulator:** `python tests/instagram_simulator.py` — type `/image` to load a local
image file and send it as a simulated Instagram image DM.

## LLM Usage & Cost Tracking

Every provider call goes through `app/services/llm_client.py`: `llm_call(purpose, model, messages, client_id, conversation_id, order_id=None, ...)` for chat (also used by `chat_json`) and `llm_transcribe(...)` for Whisper. Nothing else may touch a Groq/OpenAI client (CI guard in `tests/test_llm_usage.py`). Each call — failures included — writes one `llm_usage` row: purpose, model, real `response.usage` tokens, `cost_inr`, latency, success/error_code.

- **Cost** = tokens × per-model USD/1M prices (`llm_price_usd_per_1m` in `app/config.py`, override via `LLM_PRICE_USD_PER_1M`) × `USD_INR`. Unpriced models use the default rate and warn once; STT is estimated from audio size. **Verify the prices** — several are placeholders.
- **Attribution**: `order_pipeline.handle_inbound_message` sets an ambient (client, conversation) context so call sites that don't know the ids (e.g. `generate_reply`) are still attributed. `order_id` is back-filled for the conversation's unattributed rows when the order is created.
- **COST REPORT** (per order) is built from `llm_usage` (`cost_log.print_report`); `cost_log` still supplies the template-vs-LLM message counts.
- **API**: `GET /usage/llm?from=&to=` (owner, own client) and `GET /admin/usage/llm?client_id=` (X-Admin-Key, all clients) → totals + per day/purpose/model/conversation/order. `GET /admin/llm-cost` is the same ledger in ₹ **and** $ with cached tokens, audio seconds, `orders_created` and cost per order created (operator permission `usage.read`). **`USD_INR` is required** (no default); prices are `[in, cached_in, out]` per 1M tokens; every TURN block ends with `COST turn=₹ session=₹ lifetime=₹` (read from the ledger only) and a `TIME` line; `/admin/llm-cost` also has `per_client_last_7d` (avg ₹ per confirmed order) (see `backend/docs/agent_v3.md`, "Round 3").
- `client_id`/`conversation_id`/`order_id` on `llm_usage` are soft references (no FKs) so telemetry survives conversation resets.

## LLM Intent Router (ROUTER_V2)

The front door for every message that isn't a slot answer. **LLM understands + picks an action; the engine executes it with DB data.** The model never states prices, stock, totals, order status or dates — it returns one small JSON decision and nothing else. Replies come from EN/HI/GU templates (`rt_*` keys in `language_templates.py`); the model's free text (`reply_hint`) is used only for `general_answer`/`smalltalk` (`out_of_scope` always gets a fixed template — see "Out-of-Scope Boundary"), after `router_actions.guard_reply_hint` (no digits/prices/SKUs/stock/delivery/payment/order claims, max 2 sentences, then `guard_product_reply`).

- **Flag**: `Client.router_v2_enabled` (NULL = follow env, True/False = force) → else `ROUTER_V2_CLIENT_IDS` (default `1`; `*` = all; empty = none). Off ⇒ the old keyword pipeline runs unchanged. There is no dashboard toggle: `UPDATE clients SET router_v2_enabled = true WHERE id = …`.
- **Where**: `order_pipeline._handle_inbound_message_core`, after the early guards and before SKU/name pinning, replacing `run_pre_catalog_router`. Code: `app/services/intent_router.py` (flag, context, fast paths, the one LLM call, sanitiser), `app/services/router_actions.py` (front door + handlers), `app/schemas/router.py` (pydantic output), prompt `app/prompts/router_v2.md`.
- **Fast paths (₹0, never reach the LLM)**: button/list taps and non-text; catalogue "Order" paste; exact SKU / exact product name; number picked from an open "which one?" menu; a bare yes/no while a prompt is open; an exact option the open slot expects (colour/size/material/quantity/COD-UPI/phone). Image-while-`pending_payment` is handled earlier by `payment_inbound`. **Free-text slots (name, address) are not fast paths** — the router decides whether the message is the answer or something else ("I want to talk to someone"); low confidence there means "it's the answer". The bare word `cancel` (the Cancel button) is still handled by `run_cancel_in_payment_guard`; free-text cancel intent goes to the router.
- **One call**: `llm_client.chat_json` on `LLM_MODEL_CLASSIFIER`, JSON mode, `purpose="router"` in `llm_usage`; invalid output is retried once, then it's a counted failure (`llm_health.record_failure`) and the **keyword fallback** (`run_pre_catalog_router` + the legacy pipeline, which ends in the `llm_unavailable_rephrase` template) answers. The soft per-phone LLM cap also skips the router. Prompt context (~1.9k tokens) is PII-free: name/address/phone are reported as "set", history is redacted.
- **Engine rules**: a `sku` the model wasn't shown (candidates + pinned) is dropped; confidence < `ROUTER_CONFIDENCE_THRESHOLD` (0.5) ⇒ "Did you mean A or B?" / "what are you looking for?" (never echoes the customer's text); search = DB filters (price/colour/size/category on in-stock variants) + fuzzy, 0 hits ⇒ generic not-found + closest in-stock alternatives; ETA windows are computed by `delivery_service.order_eta_window` (business days from payment/confirmation, product `delivery_days` > client min/max); `handoff_human` pauses the bot (`bot_pause_source="escalation"`, publishes `conversation_updated`) and replies "Our team will reply here shortly."; `cancel_order` only cancels drafts / `pending_payment` orders (`cancel_by_customer`), `payment_submitted`/paid orders are explained.
- **The legacy slot machine stays the only writer of order state** for `answer_slot`/`start_order`/`show_product`: the router hands it a *canonical* text ("pink wala" → "Pink", `start_order` → the product's SKU or unique exact name + `force_order`) and the stored inbound message is restored to what the customer typed. `change_slot` is the one direct write (validated against variants/stock, then the normal question/summary renderers).
- **Log**: one line per turn — `ROUTER conv=.. action=.. conf=.. args=.. fast_path=.. fallback=.. ms=.. v=<prompt version>/<hash>` (PII slot values redacted).
- **Tests**: `tests/test_intent_router.py` (unit), `tests/replay/test_router_v2.py` (golden replay on Postgres with a scripted LLM; one `@pytest.mark.live` test, run with `RUN_LIVE_LLM=1`). The suite sets `ROUTER_V2_CLIENT_IDS=""` (autouse) so legacy replay tests don't run through the router; router tests opt in per client.
- **Offline eval**: `python tests/router_eval/run_eval.py --golden --concurrency 1 --tpm 7000` loads the *production* prompt and runs the production validation/sanitiser, so offline accuracy = production behaviour (`golden_router.jsonl` is hand-labelled; `dataset.jsonl` is the unlabelled real-traffic sample).
- **Rate limits**: Groq's free/on_demand tier is **8,000 tokens/min and 1,000 requests/day per model** (every chat model on this key). A router call is ~1.9k tokens, so production traffic needs a paid Groq tier; on 429 the router degrades to the keyword fallback.

## Agent v3 (Cart + Planner + Validator)

Strangler-fig replacement of the order flow, behind `Client.agent_v3_enabled` (NULL → `AGENT_V3_CLIENT_IDS`, default `1`; migration 0072). Off ⇒ the legacy router / slot machine runs unchanged. Full description, action schema and invariants: `backend/docs/agent_v3.md`.

- **Where** — `order_pipeline._handle_inbound_message_core` hands the turn to `agent_v3.handle_turn` after the early guards (nonce, blocklist, takeover, spam) and `run_payment_help_guard`; for v3 clients the legacy under-review "detach" reset and the keyword cancel-in-payment guard are skipped (v3 never resets the stage of a pending payment order).
- **State** — `conversation_cart` (one active row per conversation: `lines[{line_id,sku,color,size,material,qty}]`, `customer`, `payment_method`, `status`, `version`, `state`). A few legacy `conversations` columns (stage, name, address, mobile, payment, pinned SKU) are mirrored so dashboards / follow-ups / leads keep working.
- **LLM understands, engine acts** — one planner call (`LLM_MODEL_PLANNER`, `purpose="planner"`) returns closed-set actions; `agent_executor` validates each against the DB (≤2 repair rounds, then a clarify built from real options, hand-off after 2 failed clarifies on a slot); `agent_renderer` writes every word from templates / DB rows; `agent_invariants` guard grounding, CTA, amounts, pending-order ack, stage.
- **₹0 fast paths** — buttons (`v3_*` ids), numbers, exact options, BARE gate words (≤ 3 words, no product noun), side questions, greeting, exact SKU, "images / photo" for the product in focus, "continue" after a cancel (`agent_fast_paths.py`). Longer sentences go to the planner (multi-action plans). Planner failure → catalogue search / re-ask the step, never a bare apology. Behaviour added after the live conv=60 test (token/cost log, welcome-back buttons, per-cart upsell, autofill, send_images, stage sync): `backend/docs/agent_v3.md`.
- **ETA** — one source: `delivery_service.order_delivery_days` (client settings, product override for the slowest line).
- **Tests** — `tests/test_agent_v3_units.py`; scenarios `tests/scenarios/*.yaml` (scripted planner, real webhook → Postgres; `RUN_LIVE_LLM=1` for the real planner; pass-rate gate 95 %). The suite sets `AGENT_V3_CLIENT_IDS=""` (autouse) so legacy replay tests keep running the legacy pipeline.

- **Round 4 (conv=60)** — colour search filters over every product before ranking (a colour word matches variant colours only, never product names); `state.shown_lists` + the inbound Meta timestamp pick the list a number refers to (`CartData.clarify_as_of`); ₹0 fast paths for reactions ("wah", 👍 → ack + the same list's buttons), declines ("no thanks / nahi joiye / bas" → "Okay! Message anytime 😊") and best sellers (`browse_catalog.sort="bestseller"`, units from real order lines); Hindi / Gujarati (Roman + script) reply tables in `app/services/agent_templates/*.json` cover every renderer key so template turns skip the LLM localizer (`Reply.localized` → `PipelineResult.skip_localizer`; only planner free text / KB answers / unsupported languages still translate); reply language = the customer's LAST message; abandoned-intent nudges stop at 72 h (marked stale, INFO only); `COST session=` resets only at `confirm_order`. Details: `backend/docs/agent_v3.md` "Round 4".

- **Round 5 (conv=60 ORD-2026-0074)** — removal intent is a ₹0 fast path (`agent_removal.py`: "kurtinathi joti / nahi chahiye / hatao / don't want / cancel <item>" → `remove_line` by line or SKU; ambiguous → which-one buttons); a planner `ask_clarify` for a filled / single-option slot is dropped (`fixes=` on the AGENT line); option buttons carry `v3_opt:<line>:<slot>:<value>` so taps never depend on state or titles; the line added last is asked about first (`focus_line_id`); a greeting on a cart idle ≥ `AGENT_GREETING_STALE_HOURS` (6) recaps it with [Continue][New order][Browse]; categories/products are matched spelling- and transliteration-tolerantly (`agent_query.py`, `rapidfuzz`, tenant-scoped) and "sold out" / "not stocked" are told apart; WhatsApp labels go through `wa_labels.py`; WhatsApp sends share one keep-alive client (`http_client.py`, TURN line has `wa_connect`/`wa_request`); reply language is decided per message with hysteresis (`language_tracker.py`, `LANG …` log line, seller default = `Client.dashboard_language`) and the reply tables are five languages (`english.json` + 4; `agent_templates.assert_complete()`; proofreading sheet `backend/docs/templates_review.md` from `scripts/gen_templates_review.py`). Planner prompt `agent_planner_v1.4`. Details: `backend/docs/agent_v3.md` "Round 5".

- **Round 6 (conv=60 ORD-2026-0075 / 0068)** — quantity-per-option lists ("2 purple and one red and one green", five languages, number words, native digits) are read at ₹0 by `agent_multivariant.py` (valid pairs applied, an unavailable colour reported as "Green isn't available for X. Available: …", never a generic question); **no template carries example values** (customers copy them); a colour the customer did not write is reverted as `option_not_from_user`; "Remove chair" → "There's no chair in your cart" (`Removal.absent`); cart disambiguation is a list; titles are unique after shortening (`wa_labels.unique_*`) and a refused interactive send falls back to numbered text (`wa_interactive_fallback`); a pending order no longer pins `Conversation.current_stage` (TURN header: `stage=browsing pending_orders=[ORD-…:payment_submitted]`); upsell = "+ Name ₹price", once per order, capped; compliments and "ok" are `fast=reaction`; payment-proof classification/withdrawal (see "Manual UPI Payment Verification"); performance: `DB` line + `TURN_TRACE_SQL`, COST ledger queries after the send, 3 `db.refresh` round trips removed, catalogue snapshot stale-while-revalidate, migration 0074 (indexes). Planner prompt `agent_planner_v1.5`. Details: `backend/docs/agent_v3.md` "Round 6".

## Groq limits & planner fallback (v1.6)

The planner (`LLM_MODEL_PLANNER`) falls back to `LLM_MODEL_PLANNER_FALLBACK` (gpt-oss-20b) the moment it answers 429 / 5xx / times out — no sleep — and answers deterministically when both fail (`agent_fallback`: a bare yes/no/number is read against the open question). `llm_rate_limiter` keeps a per-model token bucket from `LLM_RATE_LIMITS` (default = Groq free/on_demand 8,000 TPM / 30 RPM) and learns from the `x-ratelimit-*` / `retry-after` headers; a call may queue ≤ `LLM_RATE_LIMIT_WAIT_SECONDS` (2 s) before going to the fallback. The planner prompt is ≈ 2.6k tokens (was ≈ 4.2k); the TURN block shows `PLANNER: model_used= fallback_reason= prompt_tokens= cached_tokens=`. Production needs a Groq tier above on_demand — see `backend/docs/agent_v3.md` Round 7.

## v1.7 — event loop, reply invariant, order edit (conv=60 ORD-2026-0079)

Details and tables: `backend/docs/agent_v3.md` "Round 8". Planner prompt `agent_planner_v1.7`, migration `0076`.

- **Never block the event loop.** A synchronous call inside an `async def` (boto3, Pillow, Tesseract, `subprocess`, `time.sleep`) stalls every customer, the DB replies and the scheduler; the TURN trace's `db` span is wall-clock, so it then
  *looks* like a slow database. Put such work in `asyncio.to_thread` (hashing: `image_fingerprint._HASH_POOL`). `TIME … loop_lag=start/end/worst` is in every TURN block; `loop_monitor` logs `LOOP_BLOCKED … <stack>` / `LOOP_STALL`
  (`LOOP_WATCHDOG=0` off, `ASYNCIO_DEBUG=0` disables dev debug mode). Guard: `tests/scenarios/test_perf_v17.py`.
- **Release the pooled connection before a slow network call**: `app.db.release_connection(db)` (planner, vision/OCR classification, WhatsApp send). Pool: `DB_POOL_SIZE` / `DB_MAX_OVERFLOW`.
- **Every inbound message gets a reply** (`order_pipeline.ensure_reply`): `PipelineResult.skip_send` needs a `skip_reason` in `SILENCE_REASONS`, or in `DEDUPE_REASONS` with the bot's last reply ≤ `NO_REPLY_DEDUPE_SECONDS` (10 s) old;
  otherwise `no_reply_sent` is logged and the stage-aware fallback is sent. Only IMAGE messages can be part of an image burst — a tapped button / typed answer never is.
- **Black / blank images** (`payment_proof_classifier.is_blank_image`) are "too dark to read — send it again": no OCR, no vision call, no amounts. OCR text that is not words yields no weak amounts; amounts < ₹10 are never read.
  Vision may report `amount` (second source, used only when it equals an unpaid order's total); its JSON is validated (`VISION_SCHEMA_MISMATCH`, one strict retry, "unknown" clamped to ≤ 0.5).
- **Order picker**: no amount ⇒ `payment_inbound.pick_order_by_recency` (newest unpaid order's instructions ≤ 30 min and all others > 6 h old) attaches it and offers [Change order]; otherwise the customer is asked.
- **Editing a placed order** (`agent_order_edit.py`): `pending_payment` + no live proof + ≤ `ORDER_EDIT_WINDOW_MINUTES` (30) ⇒ "1 black and 1 gray" proposes an in-place edit (size inherited, own reservation counts as stock,
  [Update order | Keep as is]); update keeps the order number, swaps reservations, writes an `order_edited_by_customer` audit row and re-sends the payment instructions. With a proof on file it offers a NEW order instead.
- Catalogue photos are hashed by `image_fingerprint.ensure_index` (snapshot load + startup), never inside a turn; `Catalogue.load` reuses its views while no stock / reservation number changed.

## Hosting

Deployed on Railway. Backend and frontend are separate Railway services. PostgreSQL is a Railway-managed add-on.

## Rate Limiting — Current State

Currently using an in-process `asyncio.Lock`-protected `defaultdict` (5 msgs / 10 sec per phone).
Works correctly for a **single Railway instance** (default deployment).

When to upgrade to Redis:
- Multiple Railway workers deployed (`--workers > 1` across instances)
- 50+ concurrent users observed
- Rate-limit bypasses seen in logs (different workers, no shared state)

Redis upgrade path:
```
pip install redis
# Replace _rate_limit_store defaultdict with redis.incr() + EXPIRE TTL
# REDIS_URL env var → Railway Redis add-on (~$5/month)
```

## Prompt Versioning — Current State

Every Groq API call logs the system prompt hash:
```
AI call | prompt_v:a3f1bc92 | model:llama-3.3-70b-versatile | history_len:4
AI reply | prompt_v:a3f1bc92 | model:llama-3.3-70b-versatile | reply_len:142 | tokens_approx:28
```

To compare prompt versions across deployments:
```bash
grep "prompt_v:a3f1bc92" logs/ | wc -l   # count replies on old prompt
grep "prompt_v:d7e2af01" logs/ | wc -l   # count replies on new prompt
```

Future: add a `prompts` table to DB to store named versions with quality metrics.
