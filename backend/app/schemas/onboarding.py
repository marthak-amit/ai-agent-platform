"""Pydantic schemas for the dashboard-first onboarding endpoints (quick start, checklist, demo products, go live)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

QuickStartLanguage = Literal["en", "hi", "gu"]


class QuickStartRequest(BaseModel):
    """Body of POST /onboarding/quick-start: the one screen shown after the first login."""

    business_name: str = Field(min_length=1, max_length=120)
    language: QuickStartLanguage = "en"

    @field_validator("business_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        """Trim whitespace and reject a blank name."""
        v = v.strip()
        if not v:
            raise ValueError("business_name must not be blank")
        return v


class ChecklistItemOut(BaseModel):
    """One setup-checklist row. `done` is derived from real database state, never ticked by hand."""

    key: str = Field(description="add_products | setup_agent | connect_whatsapp | connect_instagram | test_agent | go_live")
    required: bool
    done: bool


class TrialOut(BaseModel):
    """Trial standing, present once the account has gone live on a trial."""

    active: bool
    started_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    conversations_used: int = 0
    conversation_limit: int = 0


class ChecklistOut(BaseModel):
    """GET /onboarding/checklist — everything the dashboard needs to render the setup card and the empty states."""

    items: list[ChecklistItemOut]
    done_count: int
    total: int
    all_required_done: bool
    hidden: bool = Field(description="True once the account is live (or every required item is done): hide the card")
    next_key: Optional[str] = Field(default=None, description="First unfinished required item — empty states point here")
    account_status: str = Field(description="setup | trial | active")
    bot_active: bool
    quick_start_completed: bool
    demo_products: int = Field(description="Sample products still in the catalogue (drives the 'Remove samples' button)")
    can_go_live: bool = Field(description="Every required item except go_live is done")
    go_live_mode: Literal["trial", "plan", "trial_used", "live"] = Field(
        description="What 'Go live' will do: start the free trial, use the running plan, be refused because this "
        "WhatsApp number already had its trial, or already live"
    )
    trial: Optional[TrialOut] = None
    trial_days: int = Field(description="Length of the free trial that 'Go live' will start (TRIAL_DAYS)")
    trial_conversation_limit: int = Field(description="Conversations included in the trial (TRIAL_CONVERSATION_LIMIT)")


class QuickStartOut(BaseModel):
    """Result of the quick-start screen."""

    business_name: str
    language: str
    demo_products_created: int


class RemoveDemoOut(BaseModel):
    """Result of DELETE /onboarding/demo-products."""

    removed: int


class GoLiveRequest(BaseModel):
    """Body of POST /onboarding/go-live."""

    remove_demo_products: bool = Field(
        default=False,
        description="Delete the remaining sample products as part of going live (required when any remain)",
    )


class GoLiveOut(BaseModel):
    """Result of POST /onboarding/go-live."""

    account_status: str
    bot_active: bool
    go_live_at: Optional[datetime] = None
    trial: Optional[TrialOut] = None
    demo_products_removed: int = 0
