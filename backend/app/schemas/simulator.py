"""Pydantic schemas for the in-dashboard chat simulator (/simulator/*)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class SimulatorMessageRequest(BaseModel):
    """
    One customer turn. Exactly one of: typed `text`, a tapped quick-reply button (`button_id` as received), or a
    tapped list row (`list_row_id` as received). `title` is the label shown on the tapped control.
    """

    text: Optional[str] = Field(default=None, max_length=2000)
    button_id: Optional[str] = Field(default=None, max_length=300)
    list_row_id: Optional[str] = Field(default=None, max_length=300)
    title: Optional[str] = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _exactly_one(self) -> "SimulatorMessageRequest":
        """Require exactly one input kind, and non-blank typed text."""
        given = [v for v in (self.text, self.button_id, self.list_row_id) if v is not None]
        if len(given) != 1:
            raise ValueError("send exactly one of text, button_id, list_row_id")
        if self.text is not None and not self.text.strip():
            raise ValueError("text must not be blank")
        return self


class SimButtonOut(BaseModel):
    """A quick-reply button; `id` is sent back verbatim as `button_id` when tapped."""

    id: str
    title: str


class SimListRowOut(BaseModel):
    """A row of a list message; `id` is sent back verbatim as `list_row_id` when picked."""

    id: str
    title: str
    description: Optional[str] = None


class SimBubbleOut(BaseModel):
    """One message the customer would see, in delivery order."""

    kind: Literal["text", "image"] = "text"
    text: Optional[str] = None
    image_url: Optional[str] = None
    buttons: list[SimButtonOut] = []
    list_header: Optional[str] = None
    list_button_text: Optional[str] = None
    list_rows: list[SimListRowOut] = []


class SimulatorReplyOut(BaseModel):
    """The bot's answer to one simulated turn, plus the diagnostics owners use to judge their agent."""

    bubbles: list[SimBubbleOut]
    stage: str
    language: Optional[str] = None
    response_time_ms: int
    test_passed: bool = Field(description="True once the simulator has answered enough turns (checklist 'Test your agent')")
    llm_available: bool = Field(default=True, description="False while the AI model is down: replies are outage templates and do not count toward the test")
    silent: bool = Field(default=False, description="True when the engine intentionally sent nothing (e.g. paused)")


class SimulatorTurnOut(BaseModel):
    """One stored message of the simulator transcript."""

    role: Literal["customer", "bot"]
    text: str
    created_at: Optional[datetime] = None


class SimulatorSessionOut(BaseModel):
    """GET /simulator/session: where the simulator chat stands (for resuming after a page reload)."""

    messages: list[SimulatorTurnOut]
    stage: Optional[str] = None
    test_passed: bool
    llm_available: bool = True
    turns_required: int
    router_enabled: bool = Field(description="Whether the LLM intent router (vs the keyword flow) answers for this shop")
    sample_products: int
    daily_limit: int = Field(description="0 = unlimited")
    sent_today: int


class SimulatorResetOut(BaseModel):
    """POST /simulator/reset."""

    status: str = "reset"
    orders_removed: int = 0
