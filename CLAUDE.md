# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

WhatsApp + Instagram + Website chat AI agent SaaS platform targeting the Indian market. Clients self-serve their own AI agent setup. Conceptually similar to TailorTalk.ai. Payments via Razorpay, messaging via Meta Cloud API.

**AI providers — this is not "Gemini" despite legacy naming:** conversational replies, intent classification, and product-image recognition all run on **Groq** (`llama-3.3-70b-versatile` for replies, `llama-3.1-8b-instant` for classification/fallback, see `app/services/gemini_service.py` and `app/services/vision_service.py`). Google Gemini (`GEMINI_API_KEY`, `app/services/photo_enhancement_service.py`) is used only for the AI photo-enhancement feature on catalogue images — nothing else. The `gemini_service.py` filename, the `Client.gemini_system_prompt` column, and the `role='model'` convention are pre-Groq-migration names that were never renamed; don't infer from them that replies go through Gemini.

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
```

- `WHATSAPP_ACCESS_TOKEN`/`WHATSAPP_PHONE_NUMBER_ID` are the **global fallback** used only when a client has no credentials of its own on `Client.whatsapp_access_token`/`Client.whatsapp_phone_number_id`. Every WhatsApp send in `app/services/outbound.py` prefers the per-client values when the caller has the `Client` row loaded — see the module docstring there for why that resolution happens via already-loaded objects rather than a DB lookup inside the send path.
- `META_APP_ID`/`META_OAUTH_REDIRECT_URI` back the self-serve Instagram OAuth connect flow (`app/routers/integrations.py`).
- `META_WHATSAPP_CONFIG_ID` is the WhatsApp **Embedded Signup** Configuration ID (Meta App Dashboard → WhatsApp → Embedded Signup → Configurations) that powers the self-serve "Connect WhatsApp" button (`app/routers/whatsapp_signup.py`). Unset = that button is disabled and clients fall back to manually pasting Phone Number ID / Access Token. Getting a client fully live also requires them to appear as a Meta test user or the app to have passed Review for `whatsapp_business_management`/`whatsapp_business_messaging` — see Meta Business Settings → Roles → Test Users during development.

## Manual UPI Payment Verification

We never collect money: the customer pays the seller's UPI directly and sends a screenshot in chat; the seller approves/rejects it in the dashboard. **LLM = understand only, engine = act only** — every payment reply is a deterministic EN/HI/GU template (`pay_*` keys in `app/services/language_templates.py`).

- **Order states** (`order_state_machine.py`, `ORDER_STATUS_TRANSITIONS`): `pending_payment → payment_submitted` (customer image) · `payment_submitted → paid` (approve) · `payment_submitted → pending_payment` (reject) · `pending_payment|payment_submitted → cancelled`. Typing "paid" **never** confirms anything — it asks for the screenshot.
- **Stock**: reserved at `pending_payment` (`stock_reservations`, `stock_reservation_service.py`), deducted at `paid` in the same transaction as the status change, released at `cancelled`. New-order stock checks subtract active reservations.
- **Core**: `payment_verification_service.py` (approve/reject/cancel/expire; row-locked + idempotent, audit rows in `order_audit_log`). Inbound screenshots: `payment_inbound.py`, wrapped around `handle_inbound_message` (no vision model runs on proofs). Media is re-hosted in our storage (`media_service.py`) because Meta URLs expire.
- **Dashboard sends** go through `channel_sender.py` → `outbound.py` (24h window / opt-out / block enforced by `send_gate`). A dashboard send pauses the bot (`conversation_control.py`; `ai_enabled` stays the flag the pipeline reads, `bot_pause_source='human_send'` auto-resumes after `Client.bot_auto_resume_minutes` idle). Approve/reject templates are sent even while the bot is paused.
- **Realtime**: SSE (`GET /events/stream?token=`) from an in-process hub (`realtime_service.py`, single-instance like the rate limiter — swap `publish()` for Redis pub/sub when scaling out) with a DB-derived polling fallback (`GET /events/poll?since=`).
- **Permission**: `payment_verify` (Owner always; in the Manager preset). Payment settings (`/settings/payment`) are Owner-only.
- Dependency: `segno` (pure-python QR PNG for the `upi://pay?...&tn=Order{id}` code).

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

**Vision model:** `qwen/qwen3.6-27b` on Groq (`meta-llama/llama-4-scout-17b-16e-instruct` was used previously but was deprecated by Groq on 2026-07-17 and now 404s — see `app/services/vision_service.py`).  
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
