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
    # "test" or "live"; must match the key prefix (rzp_test_ / rzp_live_). Mock billing is refused in live.
    razorpay_mode: Literal["test", "live"] = "test"
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

    # ── LLM cost tracking (llm_usage table; see app/services/llm_usage_service.py).
    # Prices are USD per 1M tokens as (input, output) — VERIFY against the provider's
    # pricing page. Override as JSON: LLM_PRICE_USD_PER_1M='{"model-id": [in, out]}'.
    # Models not listed are priced at llm_price_default_usd_per_1m (and warned about once).
    usd_inr: float = 83.0
    llm_price_usd_per_1m: dict[str, tuple[float, float]] = {
        "openai/gpt-oss-20b": (0.075, 0.30),
        "openai/gpt-oss-120b": (0.15, 0.60),
        "llama-3.3-70b-versatile": (0.59, 0.79),
        "llama-3.1-8b-instant": (0.05, 0.08),
        "qwen/qwen3.8-27b": (0.29, 0.59),   # placeholder: qwen3-32b's price — verify
        # Gemini image generation (photo enhancement): output is billed per image token (~1290/image).
        "gemini-2.5-flash-image": (0.30, 30.0),
    }
    llm_price_default_usd_per_1m: tuple[float, float] = (0.59, 0.79)
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
        """Accept RAZORPAY_MODE in any case/spacing ("Live ", "TEST")."""
        return v.strip().lower() if isinstance(v, str) else v

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
