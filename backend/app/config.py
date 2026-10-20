"""
Application settings loaded from environment variables.

All secrets are read from .env via pydantic-settings.
Access the singleton via `get_settings()`.
"""

import warnings
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Anchored to backend/ (not the process CWD) so `uvicorn`, `alembic`, scripts and tests all read the same file.
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    """Typed environment variable bindings for the AI agent platform."""

    database_url: str
    gemini_api_key: str = ""
    openai_api_key: str = ""
    groq_api_key: str = ""
    meta_app_secret: str
    meta_verify_token: str
    whatsapp_access_token: str
    whatsapp_phone_number_id: str
    instagram_access_token: str = ""
    instagram_business_account_id: str = ""
    meta_app_id: str = ""
    meta_oauth_redirect_uri: str = ""
    # WhatsApp Embedded Signup (Tech Provider flow) — the Configuration ID
    # created in Meta App Dashboard → WhatsApp → Embedded Signup → Configurations.
    # Empty = the self-serve "Connect WhatsApp" button is disabled and clients
    # fall back to manual phone_number_id/access_token entry.
    meta_whatsapp_config_id: str = ""
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    # SellerTalk24 prepaid billing (Razorpay Orders API + Checkout.js).
    # RAZORPAY_WEBHOOK_SECRET signs webhooks (Razorpay Dashboard → Webhooks); it is NOT the API secret.
    razorpay_webhook_secret: str = ""
    # "auto" (default / blank): the key prefix decides — rzp_test_ keys run in test mode, rzp_live_ keys in live mode.
    # "test" or "live" is an optional safety lock: it must match the key prefix, and "live" also refuses mock billing.
    razorpay_mode: Literal["auto", "test", "live"] = "auto"
    # None = auto (mock when the Razorpay key id/secret are empty); True/False force it.
    sellertalk24_billing_mock: Optional[bool] = None
    # Plan prices in billing_plans are GST-EXCLUSIVE ("₹4,599 + GST"), so the default is False.
    # True would treat them as GST-inclusive and back-calculate GST out of the price.
    prices_include_gst: bool = False
    gst_rate_bps: int = Field(default=1800, ge=0, le=10000)  # basis points: 1800 = 18%
    # GST state code of the seller (24 = Gujarat). A client whose GSTIN starts with the same
    # code (or who has no GSTIN) is intra-state -> CGST+SGST; any other state -> IGST.
    seller_state_code: str = "24"
    # Seller identity printed on every tax invoice (Settings -> .env). SELLER_GSTIN / SELLER_ADDRESS must be set
    # before invoices go to real customers; an unset GSTIN prints as "—" and logs a warning.
    seller_legal_name: str = "SellerTalk24"
    seller_gstin: str = ""
    seller_address: str = ""
    seller_state_name: str = "Gujarat"
    invoice_prefix: str = "ST24"
    # Outbound email (billing notifications). "console" (default) only logs the message — nothing leaves the
    # server — so dev/staging never emails real customers by accident. "smtp" needs SMTP_HOST (+ credentials).
    # Operator address for billing incidents (amount mismatch, webhook signature burst, reconcile activations).
    # Empty = the alert is only logged (ERROR). Sent through EMAIL_PROVIDER like every other billing email.
    billing_alert_email: str = ""
    email_provider: str = "console"
    email_from: str = "SellerTalk24 <billing@sellertalk24.com>"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    # Days a client keeps full service after its last paid period ends (or after signup, if it never
    # subscribed). The bot works and the dashboard shows a red banner during grace.
    grace_days: int = Field(default=3, ge=0, le=60)
    # Free trial: starts at "Go live" (never at signup) and ends at whichever limit is hit first; then GRACE_DAYS of
    # grace, then the account is EXPIRED. One trial per WhatsApp phone_number_id (trial_claims).
    trial_days: int = Field(default=7, ge=1, le=90)
    trial_conversation_limit: int = Field(default=100, ge=1)
    # "Continue with Google": OAuth 2.0 Web client id from the SellerTalk24 Google Cloud project. Empty = the button
    # is hidden and POST /auth/google answers 503. The consent screen's app name/logo/domain are configured on that
    # project's OAuth consent screen (Google Cloud Console), not here.
    google_client_id: str = ""
    # In-dashboard simulator abuse brake: max simulator customer messages per client per UTC day. 0 = unlimited
    # (the product default: the simulator is unlimited before go-live); every turn still costs real LLM tokens.
    simulator_daily_message_limit: int = Field(default=0, ge=0)
    # Master switch for the hard stop: when False (default) an EXPIRED client is only flagged on the
    # dashboard and in logs — the bot is never restricted. Flip to True once existing clients have
    # subscribed (or been marked billing_exempt), otherwise every client without a plan goes quiet.
    sellertalk24_billing_enforce: bool = False
    # Keep the old calendar-month conversation counter (billing_service.record_conversation_activity)
    # running next to the new per-period counter for one release, and log both for comparison.
    billing_legacy_counting: bool = True
    cloudinary_cloud_name: str = ""
    cloudinary_api_key: str = ""
    cloudinary_api_secret: str = ""
    # Cloudflare R2 (S3-compatible) — product/catalogue image storage.
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = ""
    r2_endpoint: str = ""
    # Public base URL images are served from once the bucket's public access
    # is enabled (Cloudflare dashboard → bucket → Settings → Public access):
    # either the bucket's r2.dev URL or a custom domain. Empty = R2 upload
    # is disabled and product image uploads fall back to local /uploads/.
    r2_public_base_url: str = ""
    frontend_url: str = "http://localhost:5173"
    secret_key: str = "change-me-in-production"
    admin_secret_key: str = "change-me-admin-secret"
    # Admin panel (/admin): operator JWT lifetime, lockout policy, and whether the legacy shared
    # X-Admin-Key header still authenticates (break-glass / scripts). The key is ignored outside
    # development while it is still the default value, regardless of this flag.
    admin_token_expire_minutes: int = 480
    admin_max_failed_logins: int = 5
    admin_lockout_minutes: int = 15
    admin_api_key_enabled: bool = True
    admin_trust_proxy_headers: bool = True  # use the right-most X-Forwarded-For hop as the caller IP (Railway)
    admin_totp_issuer: str = "SellerTalk24 Admin"
    # Public base URL of the hosted catalogue/shop pages, used in every
    # customer-facing "browse more" / catalogue link. CATALOGUE_BASE_URL is the
    # legacy env var name and is still honoured if PUBLIC_SHOP_BASE_URL is unset.
    public_shop_base_url: str = Field(
        default="https://agentlyai.in/shop",
        validation_alias=AliasChoices("PUBLIC_SHOP_BASE_URL", "CATALOGUE_BASE_URL"),
    )
    # This backend's own publicly reachable base URL (e.g. the Railway domain),
    # used to build fully-qualified links (invoice PDFs) that Meta's WhatsApp
    # API can fetch. Empty in local dev — invoice links simply won't be
    # fetchable by WhatsApp until this is set.
    backend_public_url: str = ""
    # AgentlyAI team's own WhatsApp number (E.164, no '+'), notified on new
    # marketing-site demo leads. Unset = notification silently skipped.
    internal_lead_notify_number: str = ""
    min_reply_delay: float = 2.0
    max_reply_delay: float = 4.0

    # POST /webhook answers Meta with 200 as soon as the message is verified and de-duplicated and processes it in a background
    # task (one task at a time per sender). false = process inline before answering (what the test suite does).
    webhook_background_processing: bool = True
    # One "TURN conv=… USER/REPLY/WHY" log block per customer message (app/services/turn_trace.py).
    turn_trace_enabled: bool = True

    # ── Message batching (app/services/message_batcher.py): a customer's burst of text messages
    # ("I want" / "to buy" / "banarasi silk sadi") is answered ONCE, after the sender has been quiet for
    # `message_batch_window_seconds` (longer after a one/two-word or unfinished fragment). Hard caps bound the wait. 0 = batching off. Time waited is credited against the reply delay above.
    message_batch_window_seconds: float = 3.5            # a full sentence: quiet this long → answer
    message_batch_short_window_seconds: float = 5.0      # a one/two-word fragment ("pan", "choli", "bija")
    message_batch_extended_window_seconds: float = 7.0   # an unfinished fragment ("I want", "mare bija", a trailing comma)
    # The same three windows for tenants on agent v3 (shorter: its fast paths and planner already answer well from a
    # single message, and every second here is added to the customer's wait). 0 turns batching off for them.
    agent_batch_window_seconds: float = 1.5
    agent_batch_short_window_seconds: float = 2.5
    agent_batch_extended_window_seconds: float = 4.0
    message_batch_max_wait_seconds: float = 15.0
    message_batch_max_messages: int = 8
    environment: str = "development"  # set to "production" in Railway env vars
    use_tool_router: bool = False  # Phase 0: shadow; Phase 1+: live routing
    shadow_router_enabled: bool = False  # set True to re-enable background shadow Groq call

    # ── LLM models (Groq) — the ONLY place model names live. Override per
    # environment with the LLM_MODEL_* env vars; verify with
    # `python scripts/check_llm_models.py` (lists Groq's live /models).
    # NOTE: the legacy CLASSIFY_MODEL / REPLY_MODEL env vars are no longer read.
    groq_base_url: str = "https://api.groq.com/openai/v1"
    llm_model_reply: str = "openai/gpt-oss-20b"        # Tier 3: open-ended/ambiguous reply generation
    # Tier 2 cheap classification + JSON extraction. Defaults to the reply model
    # until /models confirms a smaller/faster one — see scripts/check_llm_models.py.
    llm_model_classifier: str = "openai/gpt-oss-20b"
    llm_model_vision: str = "qwen/qwen3.8-27b"         # "" = vision disabled (image matching skipped)
    llm_model_stt: str = "whisper-large-v3-turbo"      # voice-note transcription
    # Extra models tried (in order) when the reply model is rate-limited/missing.
    # Comma-separated; empty = fall back to llm_model_classifier only.
    llm_model_reply_fallbacks: str = ""
    # Reasoning models (e.g. gpt-oss) spend completion tokens on hidden reasoning
    # before the answer, so tiny max_tokens budgets (YES/NO classifiers) would
    # come back empty. For models whose id starts with one of these prefixes we
    # send reasoning_effort and add headroom to max_tokens.
    llm_reasoning_model_prefixes: str = "openai/gpt-oss"
    llm_reasoning_effort: str = "low"                  # "" = don't send
    llm_reasoning_headroom_tokens: int = 512
    # Circuit breaker: after this many consecutive non-429 LLM failures the
    # reply path is bypassed (deterministic template) and re-probed periodically.
    llm_breaker_threshold: int = 3
    llm_probe_interval_seconds: int = 60

    # ── LLM intent router (ROUTER_V2) — the front door for non-slot messages.
    # Client ids the router is ON for when Client.router_v2_enabled is NULL
    # (comma-separated ids, "*" = every client, "" = none). Default: client 1 only.
    router_v2_client_ids: str = "1"
    router_confidence_threshold: float = 0.5   # below this the engine asks a clarifying question
    router_max_tokens: int = 300               # visible-answer budget (reasoning headroom is added on top)
    # ── Agent v3 (Cart + Planner + Validator, app/services/agent_*.py) — per-client strangler-fig flag.
    # Client.agent_v3_enabled (True/False) wins; NULL follows this list ("1" = client 1 only, "*" = all, "" = none).
    agent_v3_client_ids: str = "1"
    # The planner is one JSON-schema call per non-fast-path message. gpt-oss-120b on Groq; verify with
    # `python scripts/check_llm_models.py`. A repair round re-calls it with the validation errors (max below).
    llm_model_planner: str = "openai/gpt-oss-120b"
    # v1.6: a 429 / 5xx / timeout on the planner model is answered at once by this model (no sleep first); if it fails too the turn
    # is answered deterministically. "" = no fallback.
    llm_model_planner_fallback: str = "openai/gpt-oss-20b"
    # Client-side limits per model, so a 429 is avoided instead of suffered: {"model": {"tpm": tokens/min, "rpm": requests/min}}.
    # The defaults are Groq's free/on_demand tier; raise them with the plan (Developer tier) — the response headers
    # (x-ratelimit-remaining-tokens / -reset-tokens) tighten them further at run time. A model with no entry is unlimited.
    llm_rate_limits: str = '{"openai/gpt-oss-120b":{"tpm":8000,"rpm":30},"openai/gpt-oss-20b":{"tpm":8000,"rpm":30}}'
    llm_rate_limit_wait_seconds: float = 2.0   # how long a call may queue for capacity before going to the fallback model
    agent_planner_max_tokens: int = 700        # visible-answer budget (reasoning headroom is added on top)
    agent_max_repairs: int = 2                 # planner re-tries after the executor rejects its actions
    agent_clarify_fail_limit: int = 2          # unresolved clarifies on one slot before handing over to a human
    agent_upsell_max_cart_share: float = 0.30  # an upsell may cost at most this share of the cart value
    agent_upsell_max_cart_total: float = 10000.0  # no upsell at all once the cart is worth more than this (₹); a client may set its own `upsell_max_cart_total`
    agent_history_turns: int = 4               # recent turns shown to the planner (v1.6: 4, was 6 — every token counts against the TPM cap)
    agent_qty_sane_max: int = 100              # a quantity above this is confirmed with the customer first ("Did you mean 150?")
    agent_max_images: int = 3                  # product photos sent for one send_images action
    # A greeting on a cart idle for at least this many hours gets the cart recap + [Continue / New order / Browse] buttons instead of
    # being answered with the old order's open question; a younger cart is greeted and continued where it stopped.
    agent_greeting_stale_hours: float = 6.0
    # Payment-image handling (v1.6). An order waiting for money whose payment instructions went out this recently biases an unclear image
    # to "payment screenshot"; images within the batch window of the last image reply get no reply of their own; "this is the payment"
    # re-reads the customer's unfiled images of the last N minutes; the "can't open that type of message" notice is sent once per window.
    payment_proof_prior_minutes: int = 60
    payment_image_batch_seconds: int = 60
    payment_reclassify_window_minutes: int = 30
    unsupported_notice_minutes: int = 10
    # v1.7. A just-confirmed order that is still unpaid (no screenshot filed) can be edited ("I want 1 black and 1 grey") for this long.
    order_edit_window_minutes: int = 30
    # Order picker: with no readable amount, the newest unpaid order is attached by itself when its payment instructions went out within
    # `picker_auto_attach_recent_minutes` AND every other unpaid order's instructions are older than `picker_auto_attach_others_older_hours`.
    picker_auto_attach_recent_minutes: int = 30
    picker_auto_attach_others_older_hours: float = 6.0
    # A photo darker than this (highest pixel value, 0–255) is a blank/black frame: no OCR, no amounts — the customer is asked to resend.
    image_blank_max_luma: int = 24
    # A vision verdict of "unknown" may not claim more than this confidence (a contradictory reply is retried once, then clamped).
    vision_unknown_max_confidence: float = 0.5
    # Database pool (asyncpg). The default 5 + 10 starves when sessions are held across LLM / WhatsApp calls; sessions now release their
    # connection before such calls, and the pool is sized for concurrent customers.
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_seconds: float = 30.0
    db_pool_recycle_seconds: int = 1800
    # Fuzzy catalogue-word matching ("lahemgha" → Lehenga): rapidfuzz ratio (0–100) a word needs against a category / product word.
    agent_fuzzy_min_ratio: int = 80
    # Human-like pause of an agent-v3 reply as a TARGET TOTAL latency (seconds, random between min and max): the pause is only
    # what is left after the work (and any batching wait) — a ₹0 fast path answers in ~0.3–0.8 s, a planner turn adds nothing.
    # The legacy flow's 2–4 s pause is MIN_REPLY_DELAY / MAX_REPLY_DELAY and stays before the work.
    agent_reply_delay_min_seconds: float = 0.3
    agent_reply_delay_max_seconds: float = 0.8

    # An out_of_scope decision needs at least this confidence to be refused with the boundary template;
    # below it the customer gets a clarifying question instead (never block a real customer on a guess).
    router_out_of_scope_threshold: float = 0.7
    # Cached shop profile (Client.shop_context) is rebuilt at least this often (KB topics have no other trigger
    # besides the KB endpoints calling shop_context_service.invalidate()).
    shop_context_ttl_seconds: int = 1800

    # ── Irrelevant-message guard (app/services/spam_guard.py): this many consecutive irrelevant messages
    # (emoji-only, off-topic) → one final notice, then the bot stays silent on free text (no LLM) for
    # `irrelevant_mute_minutes`. Per-client override of the limit: Client.off_topic_threshold. 0 minutes = never mute.
    irrelevant_streak_limit: int = 3
    irrelevant_mute_minutes: int = 15

    # ── Chat-flow hardening (app/services/order_recovery.py): a cancelled draft can be restored for this long;
    # a router cancel_order that was not explicit waits this long for the customer's Yes/No; a message of
    # "confidence >= router_cancel_min_confidence" that also contains an explicit cancel word cancels directly.
    draft_restore_ttl_minutes: int = 30
    cancel_confirm_ttl_minutes: int = 10
    router_cancel_min_confidence: float = 0.95
    # Orders above this total need the seller's explicit review before approval (Client.high_value_order_threshold
    # overrides it per shop).
    high_value_order_threshold: float = 50000.0

    # ── LLM cost tracking (llm_usage table; see app/services/llm_usage_service.py).
    # Prices are USD per 1M tokens as (input, cached_input, output) — a two-part (input, output) entry is accepted and
    # bills cached tokens at the input rate. VERIFY against the provider's pricing page. Override as JSON:
    # LLM_PRICE_USD_PER_1M='{"model-id": [in, cached_in, out]}'. Models not listed are priced at
    # llm_price_default_usd_per_1m (and warned about once).
    # USD_INR has NO default on purpose: set it in the environment (the app refuses to start without it).
    usd_inr: float
    llm_price_usd_per_1m: dict[str, tuple[float, ...]] = {
        "openai/gpt-oss-20b": (0.075, 0.0375, 0.30),      # cached rate = 50 % of input (Groq prompt caching) — verify
        "openai/gpt-oss-120b": (0.15, 0.075, 0.60),
        "llama-3.3-70b-versatile": (0.59, 0.59, 0.79),
        "llama-3.1-8b-instant": (0.05, 0.05, 0.08),
        "qwen/qwen3.8-27b": (0.29, 0.29, 0.59),   # placeholder: qwen3-32b's price — verify
        # Gemini image generation (photo enhancement): output is billed per image token (~1290/image).
        "gemini-2.5-flash-image": (0.30, 0.30, 30.0),
    }
    llm_price_default_usd_per_1m: tuple[float, ...] = (0.59, 0.59, 0.79)
    # Whisper is billed per audio hour, not tokens (Groq bills a 10 s minimum per request).
    llm_stt_usd_per_hour: float = 0.04
    # Day buckets in GET /usage/llm are cut in this timezone.
    usage_report_timezone: str = "Asia/Kolkata"

    # ── LLM cost-cascade tuning (tier thresholds) — tunable post-launch
    # without a redeploy. See app/routers/webhook.py ROUTE logging for tier
    # distribution measurement.
    catalog_match_threshold: float = 0.8   # Tier 1: confidence to auto-pin a single match
    catalog_suggest_threshold: float = 0.55  # Tier 1: confidence to surface a "did you mean" list
    # Min normalized fuzzy confidence (top score / 2 points per query keyword, 0-1) for
    # a name-match to open a numbered "which one?" menu (pending_choice_skus). Below
    # it the message falls through to normal routing instead of showing a menu.
    catalog_menu_min_confidence: float = 0.6
    reply_topk: int = 8         # Tier 3: max candidate SKUs injected into the prompt
    classify_cache_size: int = 2000  # Tier 2: normalized-phrase → intent LRU cache size

    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8")

    @field_validator("sellertalk24_billing_mock", mode="before")
    @classmethod
    def blank_billing_mock_means_auto(cls, v):
        """Treat an empty SELLERTALK24_BILLING_MOCK= line as unset (auto), not a parse error."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("razorpay_mode", mode="before")
    @classmethod
    def normalise_razorpay_mode(cls, v):
        """Accept RAZORPAY_MODE in any case/spacing ("Live ", "TEST"); blank means auto."""
        if isinstance(v, str):
            return v.strip().lower() or "auto"
        return "auto" if v is None else v

    @property
    def billing_mock_enabled(self) -> bool:
        """True when billing must use the mock gateway: forced by the flag, else when keys are empty."""
        if self.sellertalk24_billing_mock is not None:
            return self.sellertalk24_billing_mock
        return not (self.razorpay_key_id and self.razorpay_key_secret)

    @field_validator("database_url", mode="before")
    @classmethod
    def fix_postgres_scheme(cls, v: str) -> str:
        """Replace postgres:// or postgresql:// with postgresql+asyncpg:// for async driver."""
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+asyncpg://", 1)
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @field_validator("secret_key", mode="after")
    @classmethod
    def warn_insecure_secret_key(cls, v: str) -> str:
        """Warn loudly if the default insecure key is used."""
        if v == DEFAULT_SECRET_KEY:
            warnings.warn(
                "SECRET_KEY is set to the default insecure value. "
                "Set the SECRET_KEY environment variable in production.",
                stacklevel=2,
            )
        return v


DEFAULT_SECRET_KEY = "change-me-in-production"


def ensure_secret_key_is_safe(settings: Settings) -> None:
    """Raise RuntimeError if a non-development environment still uses the default SECRET_KEY."""
    if settings.environment.strip().lower() != "development" and settings.secret_key == DEFAULT_SECRET_KEY:
        raise RuntimeError(
            f"Refusing to start: SECRET_KEY is still the default value ('{DEFAULT_SECRET_KEY}') "
            f"while ENVIRONMENT='{settings.environment}'. JWTs signed with it can be forged by anyone. "
            "Set a long random SECRET_KEY, e.g. "
            "`python -c \"import secrets; print(secrets.token_hex(32))\"`, "
            "or set ENVIRONMENT=development for local use."
        )


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
