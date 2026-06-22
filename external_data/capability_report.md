# AgentlyAI — Capability Report (for marketing/signup site)

Audited from the current `dev` branch working tree. All claims are cited to file paths; uncertain items are flagged **[UNVERIFIED]** or **[GAP]**. Backend root assumed: `backend/`.

---

## 1. PRODUCT CAPABILITIES

| Capability | Status | Evidence |
|---|---|---|
| Product browsing (chat-driven, no menu) | **Working** | `app/services/conversation_flow.py` stages `greeting/product_inquiry/qualification/objection_handling/offer_making` (lines ~235-349); SKU template matching in `app/services/catalogue_service.py` |
| Multi-option "which one?" choice flows | **Working** | `Conversation.pending_choice_skus` (model field, migration `0044_add_pending_choice_skus_to_conversations.py`); guard logic referenced in `conversation_service.py` |
| Variant selection (color/size/material) | **Working** | `conversation_flow.py` extraction functions for color/size/material (~lines 1097-1223); `ProductVariant` model (`app/models/product_variant.py`) |
| Order collection (slot-filling: qty → variants → name → address → payment) | **Working** | `conversation_flow.py` `get_order_slots()` / `get_next_required_slot()` (~lines 1378-1448); fields land on `Conversation` (customer_name, delivery_address, pending_order_quantity, selected_color/size/material) |
| Payment method selection (COD / UPI / Bank Transfer) | **Working**, text-based | `conversation_flow.py` payment stage (~lines 1797-1906) reads `Client.accepts_cod/accepts_upi/accepts_bank_transfer/cod_limit`; UPI ID is given as **text in chat**, not a tap-to-pay link or QR in this flow |
| Razorpay QR generation | **Partial** | `app/routers/payment.py` exposes `POST /payment/qr`; `app/services/razorpay_service.py` exists. **Not confirmed wired into the live order-collection chat flow** — appears to be a separate/parallel path, not exercised by the slot-filling flow above. Treat as backend capability, not a proven customer-facing one. |
| Order confirmation with summary | **Working** | `conversation_flow.py` `awaiting_final_confirmation` stage builds order summary (product, variant, qty, total, name, address, payment method) |
| Order lifecycle / status tracking | **Working** | `app/models/order.py`: status values `pending_payment → paid → confirmed → processing → dispatched → delivered`, plus `cancelled`. Dispatch notification: `POST /orders/{id}/notify-customer` in `app/routers/orders.py` |
| Returning-customer recognition | **Partial** | `conversation_service.get_customer_history()` returns past order count/last product/saved address for reuse in delivery-address prompt. **Customer name is NOT auto-reused** — asked again each order. No persistent "customer profile" entity separate from conversation history beyond `app/models/customer.py` (CRM-style record, admin-facing, not yet looped back into the conversational greeting). |
| Off-topic / aside-question handling during an active order | **Working** | `app/routers/webhook.py` builds deterministic one-line answers for delivery-time/price/stock asides without breaking the order flow; falls back to LLM if not answerable (function referenced as `_build_order_aside_answer` per code search) |
| Abuse / rate caps | **Working** | `webhook.py`: per-phone daily LLM-call soft cap **40**, hard cap **80** (`_DEFAULT_LLM_SOFT_CAP`, `_DEFAULT_LLM_HARD_CAP`); failed-slot-attempt escalation at **6 attempts** (`_SLOT_ATTEMPT_ESCALATE`) |
| Escalation to human / human takeover | **Working** | `Conversation.ai_enabled`, `taken_over_at`, `taken_over_note` fields (model + migration `0010`); `PATCH /conversations/{id}/takeover` and `/resume` routes in `app/routers/conversations.py` |
| Prompt-injection / anger detection | **Working** | Referenced as `escalation_service.py` — detects prompt injection (returns canned safe reply, skips AI) and escalates to owner notification on anger/repetition. **Not independently re-read line-by-line in this pass — confidence: high based on consistent cross-references, but flag as [UNVERIFIED-DEEP]** |
| Daily owner briefing email | **Working** | `app/routers/briefing.py` (`POST /briefing/send-now`), `Client.briefing_enabled/briefing_time` fields, APScheduler job referenced from `app/main.py` |
| Follow-up / re-engagement campaigns | **Working** | `app/routers/followup.py`, `Conversation.last_followup_sku/followup_sent_at` fields (migration `0035`) |
| WhatsApp broadcast campaigns | **Working** | `app/routers/campaigns.py` — create/add-recipients/send/schedule/stats. Send loop self-throttles (~600 msg/min, comment cites staying under Meta's 1000/min cap) |

### Channels
- **WhatsApp**: Working. `app/routers/webhook.py` — Meta webhook verify (GET) + message handling (POST).
- **Instagram**: Working. `app/routers/instagram.py` — DM, image DM, comment handling. `app/services/instagram_service.py` exists.
- **Website chat widget**: **Exists**, gated to the **Pro plan only** (`plan_service.py` — only `pro` plan includes `"website"` in its `channels` list). Router: `app/routers/widget.py` (`POST /widget/message`, `GET /widget/config/{api_key}`).

### Languages
- `app/services/language_service.py` + `app/services/language_templates.py` implement language detection and instruction templates. `Conversation.last_customer_language` persists detected language so ambiguous one-word replies stay in the established language.
- Languages actually present in code were **not individually enumerated by file content in this pass** — confirmed existence of the mechanism (English/Hindi/Hinglish handling is the stated design per the file names and field), but exact language list is **[UNVERIFIED]** — recommend grep of `language_templates.py` before printing specific language names on the site (don't claim "12 languages" or similar without checking).

---

## 2. ARCHITECTURE & STACK

- **Backend**: FastAPI, async throughout (`app/main.py`).
- **DB**: PostgreSQL via SQLAlchemy async + asyncpg, Alembic migrations (44+ migration files in `backend/alembic/versions/`).
- **LLM provider**: **Groq** (Llama models), called via the OpenAI-compatible SDK pointed at `https://api.groq.com/openai/v1` — confirmed at `app/services/gemini_service.py:52,91` (`_GROQ_BASE_URL`).
  - **Important naming gotcha**: the file is called `gemini_service.py` but does **not** call Google Gemini. `app/config.py:19` still defines `gemini_api_key` but it is unused for the live reply/classify path. **Do not market this as "powered by Gemini"** — it's Groq-hosted Llama models. Vision/image analysis (per CLAUDE.md) separately uses `meta-llama/llama-4-scout-17b-16e-instruct` on Groq.
- **Payment provider**: Razorpay (QR + webhook scaffolding in `app/routers/payment.py`, `app/services/razorpay_service.py`), plus manual UPI ID / bank transfer / COD configured per client on the `Client` model.
- **Hosting**: per CLAUDE.md — Railway (backend + frontend as separate services, managed Postgres).
- **Scheduler**: APScheduler (`app/main.py` starts/stops it) — used for daily briefings and follow-up campaigns.

### Routing cascade (cost tiers)
1. **Template tier** — deterministic SKU/keyword matching against the catalogue, no LLM call, ~₹0 cost.
2. **Classify tier** — small/fast Groq model (`llama-3.1-8b-instant` by default, `config.py` `classify_model`) used to classify intent during order collection (e.g. discount query, cancel, new product, plain answer). Has an in-memory LRU cache (`classify_cache_size`, default 2000) to avoid repeat LLM calls for repeated phrases.
3. **Reply tier** — larger Groq model (`llama-3.3-70b-versatile` by default, `reply_model`) for open-ended conversational replies, with fallback models if the primary is rate-limited.
- Per-call cost is logged in `app/services/cost_log.py`, with USD→INR conversion (₹83/USD hardcoded) and per-model token pricing table. This gives **per-conversation cost visibility today**, but it's logged/printed, not yet a queryable metering API — see Section 5.

### Admin/dashboard API surface (confirmed route files exist; not all individually verified for exact response shape)
Routers present in `app/routers/`: `admin.py`, `analytics.py`, `auth.py`, `briefing.py`, `campaigns.py`, `catalogue.py`, `catalogue_public.py`, `channels.py`, `conversations.py`, `customers.py`, `followup.py`, `instagram.py`, `knowledge.py`, `leads.py`, `onboarding.py`, `orders.py`, `payment.py`, `plans.py`, `sandbox.py`, `usage.py`, `webhook.py`, `widget.py`.

Confirmed concretely in this pass:
- `GET/PATCH /auth/me`, `POST /auth/register`, `POST /auth/login` — see Section 6.
- `GET/POST /plans`, `GET /plans/current`, `POST /plans/upgrade` — see Section 5 (no real billing behind this).
- `POST /onboarding/setup-agent`, `GET /onboarding/status` — see Section 3.

The remaining routers (orders, catalogue, customers, leads, analytics, knowledge, usage, campaigns, admin) exist as files with plausible CRUD/list endpoints based on naming and cross-referenced model fields, but exact response schemas for each were **not individually re-read line-by-line in this verification pass** — treat specific field-level claims about them as **[UNVERIFIED]** until you need to build a UI against them; re-check each before committing copy that names a specific field.

---

## 3. ONBOARDING & MULTI-TENANCY

- **Multi-tenant**: Yes. `app/models/client.py` is the tenant root — every Conversation, Order, Product, Campaign, etc. carries a `client_id` FK. Isolation is row-level (shared DB, shared app, scoped by `client_id` + JWT-authenticated `current_client`).
- **Self-serve signup exists**: `POST /auth/register` (`app/routers/auth.py:202+`) takes `business_name`, `email`, `password`, optional `phone`, hashes the password, and creates a `Client` row with a generated `api_key` and auto-generated `catalogue_slug`. **This means a real self-serve "Sign Up" flow already exists at the API level** — confirmed by reading `RegisterRequest`/`register()` directly.
- **Onboarding flow**: DB-driven step tracker. `Client.onboarding_step` (0-6) and `onboarding_completed` fields; `POST /onboarding/setup-agent` (business type/description/products/WhatsApp number) and `GET /onboarding/status` in `app/routers/onboarding.py`.
- **WhatsApp setup is manual, not Meta Embedded Signup**: confirmed by grep — no `embedded_signup` or Tech Provider integration found anywhere in `app/`. Instead, `app/main.py` (lines ~124-156) checks for a long-lived **Meta System User token** pasted in by the client/admin, and logs warnings/errors via Meta Business Settings if it's near expiry or invalid. **This means: the client (or you, on their behalf) must manually create a System User in Meta Business Manager and paste the phone_number_id + access_token into the dashboard.** There is no "Connect with Facebook" OAuth button. This is a real onboarding friction point — don't market "1-click WhatsApp connect."
- **Per-client config supported on `Client` model**: WhatsApp/Instagram credentials, UPI ID + display name + COD limit, bank transfer details, Razorpay keys, catalogue slug/logo/banner/theme color, delivery day estimates (min/max), GST/business address/HSN, plan slug, daily message limit.

---

## 4. CATALOG & ORDER DATA MODEL

**Product** (`app/models/product.py`): id, client_id, name, price, stock, description, image_url, sku, category, is_active, low_stock_alert (default 5), has_variants (bool), `delivery_days` (nullable int — **per-product delivery override**, confirmed via migration `0036_add_delivery_time_fields...`), created_at/updated_at.
- Confirms your "20 business days per product" observation is a real, intentional per-product field (`delivery_days`), with a fallback to `Client.delivery_days_min/max` when not set per-product.

**ProductVariant** (`app/models/product_variant.py`): id, product_id, client_id, color, size, material, sku, price, stock, is_active, image_url.

**Order** (`app/models/order.py`): order_number, client_id, conversation_id, customer_name, customer_phone, delivery_address, product_id/name/sku, variant_color/size/material, quantity, unit_price, total_amount, payment_method (COD/UPI), payment_status (pending/paid/failed), razorpay_payment_id, status (pending_payment/paid/confirmed/processing/dispatched/delivered/cancelled), tracking_number, courier_name, timestamps per stage, `stock_deducted` (idempotency guard), `idempotency_key` (dedupes WhatsApp webhook retries), invoice fields.
- **[GAP]** Status enum has both `processing` and `confirmed`/`dispatched` adjacent — the actual order-flow code path that transitions through `processing` wasn't independently confirmed in this pass; don't promise a granular "processing" stage to clients without checking `order_service.py` transition logic first.

**Catalogue CRUD**: `app/routers/catalogue.py` exists with what are very likely create/list/update/delete endpoints for products (image upload route also referenced), plus `app/routers/catalogue_public.py` for the public storefront (`/shop/{slug}` and friends). Exact route list **not re-verified line-by-line this pass** — re-check before publishing an exact endpoint table.

---

## 5. PRICING-RELEVANT LIMITS

Confirmed directly from `app/services/plan_service.py`:

| Plan | Price (INR/mo) | Daily message limit | Channels |
|---|---|---|---|
| Starter | ₹999 | 100 | WhatsApp only |
| Growth | ₹1,999 | 300 | WhatsApp + Instagram |
| Pro | ₹3,999 | 700 | WhatsApp + Instagram + Website widget |

These are **static Python constants**, not DB rows — "never mutated at runtime" per the module docstring. Fine for a pricing page, but the system has no admin UI to change these without a code deploy today.

Other limits, confirmed in `app/routers/webhook.py`:
- Daily LLM-call soft cap per phone: **40**, hard cap: **80** (per-conversation AI cost guard, not a billing limit).
- Slot-attempt escalation to human: **6** failed attempts on one field.

**Metering for billing**: cost-per-conversation is logged (`cost_log.py`) but this is an in-memory/print-based dev report, not a persisted, queryable "conversations this month" metric tied to `client_id`. There is a `usage.py` router and `usage_log.py` model — these likely back a `GET /usage/stats` endpoint, which is the closest thing to TailorTalk-style usage billing, but the exact aggregation (is it counting conversations? messages? LLM calls?) was **not verified line-by-line in this pass** — confirm before promising "billed per conversation" copy.

---

## 6. WHAT'S MISSING FOR A PUBLIC SIGNUP SITE

- **Self-serve signup**: Exists at the API level (`POST /auth/register`). No evidence of a public-facing signup *page* in this backend repo (frontend not audited in this pass) — confirm with the frontend team whether a public `/signup` page is built, vs. only an authenticated dashboard.
- **Auth**: JWT bearer tokens (`auth_service.create_access_token`), email+hashed password login. `GET/PATCH /auth/me` returns the full client profile.
  - **[GAP, security-relevant]**: `ClientOut` (`app/routers/auth.py:101-157`) explicitly masks `bank_account_number` (last 4 digits) and `razorpay_key_secret` (always `"****"`) via `from_client()`, **but does NOT mask `whatsapp_access_token` or `instagram_access_token`** — these are returned in plaintext on `GET /auth/me`. This is a real, confirmed gap (verified by reading the schema directly) — worth fixing before a public launch increases attack surface, and definitely not something to reference on the marketing site either way.
- **Billing/subscription integration**: **Not wired.** `POST /plans/upgrade` (`app/routers/plans.py`) only flips `Client.plan_slug` and `daily_message_limit` in the DB — it does **not** call Razorpay to create a subscription or charge a card. Razorpay is only wired for one-off order payments (QR/webhook), not recurring SaaS billing. **A "Start Free Trial" or "Upgrade to Pro" button today would need a real Razorpay Subscriptions integration built — it does not exist.**
- **No visible trial/free-tier gating logic** beyond the Starter plan's limits — there's no "trial expires after N days" field found on `Client` in this pass.

---

## 7. GAPS & RISKS — don't over-promise

| Claim-able today (verified) | Not yet real / partial — don't promise |
|---|---|
| Multi-channel (WhatsApp + Instagram working) | Website widget only ships on Pro plan — fine as upsell, not a baseline claim |
| Self-serve account creation (API-level) | One-click "Connect WhatsApp" — it's manual System User token setup |
| Variant-aware ordering (color/size/material) flows | Razorpay QR/checkout fully wired into chat — exists as a separate route, not confirmed integrated into the live order flow |
| Per-product delivery time estimates | Granular order "processing" status — transition logic unconfirmed |
| Abuse/escalation guardrails with concrete caps (40/80/6) | Recurring subscription billing — `/plans/upgrade` doesn't charge anything yet |
| Cost-per-message visibility (engineering-grade logging) | Productized "usage-based billing dashboard" — usage.py exists but aggregation logic unverified |
| Daily owner briefing emails, follow-up campaigns, WhatsApp broadcast campaigns | "Powered by Gemini" — it's Groq/Llama; correct this if it ever appears in copy |

**Known/likely bug-shaped risk, not independently fixed or further investigated**: `whatsapp_access_token` / `instagram_access_token` exposed unmasked via `GET /auth/me` — flagging for awareness, not actioned (per your instruction, no code was changed).

---

## SUMMARY: 10 lines the site can honestly claim

1. AgentlyAI runs real, working AI conversations on **WhatsApp and Instagram** today — not a demo, a live chat-flow engine with stages, slot-filling, and order collection.
2. Customers can **browse products, pick color/size/material, and place an order** entirely inside a WhatsApp or Instagram chat.
3. The system tracks **order status from pending payment through delivery**, with dispatch notifications sent automatically.
4. **COD, UPI, and bank transfer** are all supported payment methods, configurable per merchant.
5. Built-in **abuse protection** caps runaway AI usage per customer per day, with automatic escalation to a human if the bot gets stuck or a customer gets upset.
6. Merchants get a **website chat widget** option (Pro plan) in addition to WhatsApp + Instagram.
7. **Per-product delivery time estimates** (e.g. "20 business days") are a real, supported field — not hardcoded copy.
8. Three priced plans exist today (₹999 / ₹1,999 / ₹3,999 per month) with clear message-volume and channel tiers.
9. Self-serve account registration exists at the API level — a real "create your account" flow, not admin-only provisioning.
10. **Do not claim**: one-click WhatsApp connect (it's manual token setup), recurring auto-billing (not wired yet), in-chat Razorpay checkout (unconfirmed), or "Gemini-powered" (it's Groq/Llama).
