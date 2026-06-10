"""
Application settings loaded from environment variables.

All secrets are read from .env via pydantic-settings.
Access the singleton via `get_settings()`.
"""

import warnings
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    cloudinary_cloud_name: str = ""
    cloudinary_api_key: str = ""
    cloudinary_api_secret: str = ""
    frontend_url: str = "http://localhost:5173"
    secret_key: str = "change-me-in-production"
    admin_secret_key: str = "change-me-admin-secret"
    catalogue_base_url: str = "https://agentlyai.in/shop"
    min_reply_delay: float = 2.0
    max_reply_delay: float = 4.0
    environment: str = "development"  # set to "production" in Railway env vars

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

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
        if v == "change-me-in-production":
            warnings.warn(
                "SECRET_KEY is set to the default insecure value. "
                "Set the SECRET_KEY environment variable in production.",
                stacklevel=2,
            )
        return v


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
