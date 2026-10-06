"""Settings must read backend/.env regardless of CWD; startup reports SECRET_KEY default-ness only as a boolean."""

from __future__ import annotations

import logging
from pathlib import Path

from app import config, main


def test_env_file_is_absolute_and_anchored_to_backend():
    """ENV_FILE is backend/.env (absolute), not a CWD-relative '.env'."""
    assert config.ENV_FILE.is_absolute()
    assert config.ENV_FILE == Path(config.__file__).resolve().parent.parent / ".env"
    assert config.Settings.model_config["env_file"] == config.ENV_FILE


def test_secret_key_env_var_name_matches_field():
    """The env var SECRET_KEY maps to Settings.secret_key (no alias mismatch)."""
    field = config.Settings.model_fields["secret_key"]
    assert field.validation_alias is None and field.alias is None


def test_startup_logs_secret_key_default_flag_not_value(monkeypatch, caplog):
    """The banner prints 'SECRET_KEY default: True/False' and never the key itself."""
    s = config.get_settings().model_copy(update={"secret_key": "super-secret-value-xyz", "environment": "production"})
    monkeypatch.setattr(config, "get_settings", lambda: s)
    with caplog.at_level(logging.INFO):
        main._startup_checks()
    assert "SECRET_KEY default: False" in caplog.text
    assert "super-secret-value-xyz" not in caplog.text
