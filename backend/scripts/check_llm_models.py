"""
Check that the configured Groq models actually exist.

Calls GET {GROQ_BASE_URL}/models, prints every available model id, then checks
each configured model (LLM_MODEL_REPLY / _CLASSIFIER / _VISION / _STT and
LLM_MODEL_REPLY_FALLBACKS) against that list.

Run from backend/:
    python scripts/check_llm_models.py

Reads GROQ_API_KEY and the LLM_MODEL_* values from the environment or backend/.env,
falling back to the defaults in app/config.py. Needs no database settings.

Exit code: 0 all configured models exist · 1 some missing · 2 could not query /models.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.services import llm_health  # noqa: E402

_ENV_VARS = {
    "groq_base_url": "GROQ_BASE_URL",
    "llm_model_reply": "LLM_MODEL_REPLY",
    "llm_model_classifier": "LLM_MODEL_CLASSIFIER",
    "llm_model_vision": "LLM_MODEL_VISION",
    "llm_model_stt": "LLM_MODEL_STT",
    "llm_model_reply_fallbacks": "LLM_MODEL_REPLY_FALLBACKS",
}


def load_config(env: dict[str, str] | None = None) -> tuple[str, object]:
    """Return (api_key, settings-like object) from `env`, defaulting to app/config.py defaults."""
    env = os.environ if env is None else env

    class _Cfg:
        """Plain attribute bag with the Settings fields this script needs."""

    cfg = _Cfg()
    for field, var in _ENV_VARS.items():
        setattr(cfg, field, env.get(var, Settings.model_fields[field].default))
    return env.get("GROQ_API_KEY", ""), cfg


async def run(api_key: str, cfg, fetch=llm_health.fetch_available_models, out=print) -> int:
    """Query /models, print the results and return the process exit code."""
    if not api_key:
        out("ERROR: GROQ_API_KEY is not set (env or backend/.env).")
        return 2
    try:
        available = await fetch(api_key, cfg.groq_base_url)
    except Exception as exc:
        out(f"ERROR: could not list models from {cfg.groq_base_url}/models: {type(exc).__name__}: {exc}")
        return 2

    out(f"Available models ({len(available)}):")
    for model_id in available:
        out(f"  {model_id}")

    configured = llm_health.configured_models(cfg)
    missing = llm_health.find_missing_models(configured, available)
    out("\nConfigured models:")
    for role, name in configured.items():
        out(f"  [{'MISSING' if role in missing else 'ok     '}] {role:<18} {name}")
    if "vision" not in configured:
        out("  [off    ] vision             (LLM_MODEL_VISION is empty — vision path disabled)")
    if missing:
        out("\nFAIL: set the LLM_MODEL_* env vars for the MISSING roles to ids from the list above.")
        return 1
    out("\nOK: every configured model exists.")
    return 0


def main() -> int:
    """CLI entry point."""
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    except ImportError:
        pass
    api_key, cfg = load_config()
    return asyncio.run(run(api_key, cfg))


if __name__ == "__main__":
    sys.exit(main())
