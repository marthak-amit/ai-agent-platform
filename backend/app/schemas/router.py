"""Pydantic schemas for the LLM intent router's JSON output (ROUTER_V2)."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RouterAction(str, Enum):
    """Every action the router may pick; the engine owns what each one does."""

    GREETING = "greeting"
    ORDER_STATUS = "order_status"
    SEARCH_CATALOG = "search_catalog"
    SHOW_PRODUCT = "show_product"
    START_ORDER = "start_order"
    ANSWER_SLOT = "answer_slot"
    CHANGE_SLOT = "change_slot"
    CANCEL_ORDER = "cancel_order"
    FAQ = "faq"
    HANDOFF_HUMAN = "handoff_human"
    GENERAL_ANSWER = "general_answer"
    SMALLTALK = "smalltalk"


ROUTER_ACTIONS: tuple[str, ...] = tuple(a.value for a in RouterAction)
ROUTER_FOCUS = ("status", "delivery", "payment", "items")
ROUTER_LANGUAGES = ("en", "hi", "gu", "hinglish")

# Spellings models produce for the four allowed language codes.
_LANGUAGE_ALIASES = {
    "en": "en", "english": "en", "eng": "en",
    "hi": "hi", "hindi": "hi", "hindi_roman": "hi", "hindi_devanagari": "hi",
    "gu": "gu", "gujarati": "gu", "gujarati_roman": "gu", "gujarati_script": "gu",
    "hinglish": "hinglish", "mixed": "hinglish",
}
# Spellings for the focus enum; unknown values become "status" rather than an error.
_FOCUS_ALIASES = {
    "status": "status", "delivery": "delivery", "eta": "delivery", "shipping": "delivery",
    "payment": "payment", "pay": "payment", "items": "items", "item": "items", "contents": "items",
}


def _blank_to_none(value: Any) -> Any:
    """Treat '' / 'null' / 'none' strings as missing (models sometimes emit them for absent args)."""
    if isinstance(value, str) and value.strip().lower() in ("", "null", "none", "n/a"):
        return None
    return value


def _to_number(value: Any) -> Optional[float]:
    """Coerce '1000', '₹1,000', 1000 to a float; anything unparsable (or <0) becomes None."""
    value = _blank_to_none(value)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    cleaned = "".join(ch for ch in str(value) if ch.isdigit() or ch == ".")
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return number if number >= 0 else None


class RouterFilters(BaseModel):
    """Structured catalogue filters the customer expressed (all optional)."""

    model_config = ConfigDict(extra="ignore")

    color: Optional[str] = None
    size: Optional[str] = None
    max_price: Optional[float] = None
    min_price: Optional[float] = None
    category: Optional[str] = None

    @field_validator("color", "size", "category", mode="before")
    @classmethod
    def _clean_text(cls, value: Any) -> Any:
        """Blank/'null' strings become None; numbers (e.g. size 40) become strings."""
        value = _blank_to_none(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        return value.strip() if isinstance(value, str) else value

    @field_validator("max_price", "min_price", mode="before")
    @classmethod
    def _clean_price(cls, value: Any) -> Optional[float]:
        """Accept numbers or '₹1,000'-style strings."""
        return _to_number(value)

    def is_empty(self) -> bool:
        """True when no filter is set."""
        return not any(v not in (None, "") for v in self.model_dump().values())


class RouterArgs(BaseModel):
    """Action arguments. Only the fields relevant to the chosen action are used; extras are ignored."""

    model_config = ConfigDict(extra="ignore")

    sku: Optional[str] = None
    query: Optional[str] = None
    filters: RouterFilters = Field(default_factory=RouterFilters)
    slot: Optional[str] = None
    value: Optional[str] = None
    order_id: Optional[str] = None
    focus: Optional[str] = None

    @field_validator("filters", mode="before")
    @classmethod
    def _filters_default(cls, value: Any) -> Any:
        """null / [] / '' filters mean 'no filters'."""
        return {} if not value or not isinstance(value, dict) else value

    @field_validator("sku", "query", "slot", "order_id", mode="before")
    @classmethod
    def _clean_text(cls, value: Any) -> Any:
        """Blank/'null' strings become None; numbers become strings."""
        value = _blank_to_none(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        return value.strip() if isinstance(value, str) else value

    @field_validator("value", mode="before")
    @classmethod
    def _clean_value(cls, value: Any) -> Any:
        """Slot values may arrive as ints (quantity 2) or bools; always keep them as short strings."""
        value = _blank_to_none(value)
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, (int, float)):
            return str(int(value)) if float(value).is_integer() else str(value)
        return value.strip() if isinstance(value, str) else value

    @field_validator("focus", mode="before")
    @classmethod
    def _clean_focus(cls, value: Any) -> Any:
        """Map focus aliases onto the allowed set; unknown/blank → None (the handler defaults to status)."""
        value = _blank_to_none(value)
        if not isinstance(value, str):
            return None
        return _FOCUS_ALIASES.get(value.strip().lower())


class RouterDecision(BaseModel):
    """
    One validated router output: what the customer wants, never what to say.

    `action` must be one of RouterAction (anything else fails validation → retry → fallback).
    `reply_hint` is only ever used for general_answer / smalltalk, and only after the guard.
    """

    model_config = ConfigDict(extra="ignore")

    action: RouterAction
    args: RouterArgs = Field(default_factory=RouterArgs)
    language: str = "en"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reply_hint: Optional[str] = None

    @field_validator("args", mode="before")
    @classmethod
    def _args_default(cls, value: Any) -> Any:
        """null / [] / '' args mean 'no args'."""
        return {} if not value or not isinstance(value, dict) else value

    @field_validator("action", mode="before")
    @classmethod
    def _normalise_action(cls, value: Any) -> Any:
        """Lower-case and trim the action string (the enum check itself stays strict)."""
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("language", mode="before")
    @classmethod
    def _normalise_language(cls, value: Any) -> str:
        """Fold language spellings into en|hi|gu|hinglish; unknown values default to 'en'."""
        if not isinstance(value, str):
            return "en"
        return _LANGUAGE_ALIASES.get(value.strip().lower(), "en")

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: Any) -> float:
        """Accept '0.8', 80 (percent) and clamp to [0, 1]; unparsable → 0 (forces a clarify)."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        if number > 1.0 and number <= 100.0:
            number = number / 100.0
        return min(max(number, 0.0), 1.0)

    @field_validator("reply_hint", mode="before")
    @classmethod
    def _clean_hint(cls, value: Any) -> Any:
        """Blank/'null' → None."""
        value = _blank_to_none(value)
        return value.strip() if isinstance(value, str) else None


class RouterTrace(BaseModel):
    """Per-turn router outcome kept for the ROUTER log line and tests."""

    action: Optional[str] = None
    confidence: Optional[float] = None
    args: dict = Field(default_factory=dict)
    fast_path: Optional[str] = None
    fallback: Optional[str] = None
    ms: int = 0
