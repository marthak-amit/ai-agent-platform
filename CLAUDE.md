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
ROUTER_V2_CLIENT_IDS=1   # optional: ids the LLM router is on for (see "LLM Intent Router")
```

- `WHATSAPP_ACCESS_TOKEN`/`WHATSAPP_PHONE_NUMBER_ID` are the **global fallback** used only when a client has no credentials of its own on `Client.whatsapp_access_token`/`Client.whatsapp_phone_number_id`. Every WhatsApp send in `app/services/outbound.py` prefers the per-client values when the caller has the `Client` row loaded — see the module docstring there for why that resolution happens via already-loaded objects rather than a DB lookup inside the send path.
- **Instagram tokens**: per-client long-lived tokens live on `Client.instagram_access_token`. `instagram_token_service.py` verifies each at startup (`graph.instagram.com/me`, logs VALID/INVALID per `client_id`, never token values; verdicts feed `/health`) and a weekly scheduler job (`refresh_instagram_tokens`, Mon 03:00 IST) refreshes them via `graph.instagram.com/refresh_access_token` before the 60-day expiry. Global `INSTAGRAM_ACCESS_TOKEN` is a fallback that `instagram_service` sends and `vision_service.download_instagram_media` still use.
- `META_APP_ID`/`META_OAUTH_REDIRECT_URI` back the self-serve Instagram OAuth connect flow (`app/routers/integrations.py`).
- `META_WHATSAPP_CONFIG_ID` is the WhatsApp **Embedded Signup** Configuration ID (Meta App Dashboard → WhatsApp → Embedded Signup → Configurations) that powers the self-serve "Connect WhatsApp" button (`app/routers/whatsapp_signup.py`). Unset = that button is disabled and clients fall back to manually pasting Phone Number ID / Access Token. Getting a client fully live also requires them to appear as a Meta test user or the app to have passed Review for `whatsapp_business_management`/`whatsapp_business_messaging` — see Meta Business Settings → Roles → Test Users during development.

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
- **API**: `GET /usage/llm?from=&to=` (owner, own client) and `GET /admin/usage/llm?client_id=` (X-Admin-Key, all clients) → totals + per day/purpose/model/conversation/order.
- `client_id`/`conversation_id`/`order_id` on `llm_usage` are soft references (no FKs) so telemetry survives conversation resets.

## LLM Intent Router (ROUTER_V2)

The front door for every message that isn't a slot answer. **LLM understands + picks an action; the engine executes it with DB data.** The model never states prices, stock, totals, order status or dates — it returns one small JSON decision and nothing else. Replies come from EN/HI/GU templates (`rt_*` keys in `language_templates.py`); the model's free text (`reply_hint`) is used only for `general_answer`/`smalltalk`, after `router_actions.guard_reply_hint` (no digits/prices/SKUs/stock/delivery/payment/order claims, max 2 sentences, then `guard_product_reply`).

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
