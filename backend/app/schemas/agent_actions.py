"""
Agent v3 action schema — the CLOSED set of things the planner may ask the engine to do.

The planner (an LLM) never answers the customer. It returns ``{"actions": [...], "reply_intent": "..."}`` and the
executor validates every action against the database before anything changes. Replies are rendered by
``agent_renderer`` from templates and real data, so the planner cannot state a price, a stock level or a delivery date.

Two shapes of the same schema exist on purpose:

* the pydantic discriminated union below (``Action``) — strict, what the executor works with;
* ``wire_schema()`` — the flat JSON schema sent to Groq's structured-output mode (every key present, unused ones
  ``null``, ``additionalProperties: false``). ``parse_planner_output`` turns the flat form back into ``Action`` objects,
  validating **per action**, so one malformed action becomes a structured error for the repair loop instead of
  discarding the whole plan.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# Topics `answer_question` may be asked about (side_questions.build_answer + order_status).
QUESTION_TOPICS: tuple[str, ...] = (
    "returns", "cod", "payment_methods", "delivery_time", "delivery_charge", "delivery_area", "warranty", "quality",
    "trust", "location", "contact", "size_guide", "bulk", "discount", "order_status",
)
# What a clarify question is about — lets the engine count "asked twice about the same thing".
CLARIFY_SLOTS: tuple[str, ...] = (
    "product", "color", "size", "material", "quantity", "name", "address", "payment", "split", "cancel", "change", "remove", "other",
)
REPLY_INTENTS: tuple[str, ...] = ("none", "greeting", "smalltalk", "thanks", "goodbye", "reaction", "decline")
ACTION_TYPES: tuple[str, ...] = (
    "search_catalog", "browse_catalog", "show_product", "send_images", "add_line", "update_line", "remove_line",
    "split_line", "set_customer", "set_payment", "ask_clarify", "request_confirmation", "confirm_order", "cancel",
    "handoff_human", "answer_question",
)


class _Strict(BaseModel):
    """Base: unknown keys are errors (the schema is closed)."""

    model_config = ConfigDict(extra="forbid")


class Variant(_Strict):
    """A (possibly partial) variant selection. Only values the customer gave — never a guess."""

    color: Optional[str] = None
    size: Optional[str] = None
    material: Optional[str] = None

    def given(self) -> dict[str, str]:
        """The attributes that are set, as ``{attr: value}``."""
        return {k: v for k, v in (("color", self.color), ("size", self.size), ("material", self.material)) if v}


class LineFields(_Strict):
    """The fields ``update_line`` may change on an existing line."""

    color: Optional[str] = None
    size: Optional[str] = None
    material: Optional[str] = None
    qty: Optional[int] = Field(default=None, ge=1, le=10_000)

    def given(self) -> dict[str, Any]:
        """The fields that are set, as ``{field: value}``."""
        return {k: v for k, v in self.model_dump().items() if v not in (None, "")}


class SplitPart(_Strict):
    """One share of a split line: a variant and how many pieces."""

    variant: Variant
    qty: int = Field(ge=1, le=10_000)


class SearchCatalog(_Strict):
    """Look for products matching free text (``null`` = nothing specific, same as browse)."""

    type: Literal["search_catalog"]
    query: Optional[str] = None


class BrowseCatalog(_Strict):
    """Show what the shop sells — a category, or (``null``) the shop in general. Use for generic "show me products"."""

    type: Literal["browse_catalog"]
    category: Optional[str] = None
    sort: Optional[Literal["bestseller"]] = None      # "bestseller" = what actually sold most; null = the shop in general


class ShowProduct(_Strict):
    """Show one product's card."""

    type: Literal["show_product"]
    sku: str


class SendImages(_Strict):
    """Send the photos of one product (at most 3). "images / photo / pic / dikhao / batao" about a product in focus."""

    type: Literal["send_images"]
    sku: str


class AddLine(_Strict):
    """Put a product in the cart. ``variant`` / ``qty`` only if the customer said them; unknown stays ``null``."""

    type: Literal["add_line"]
    sku: str
    variant: Variant = Field(default_factory=Variant)
    qty: Optional[int] = Field(default=None, ge=1, le=10_000)


class UpdateLine(_Strict):
    """Change fields of one existing cart line."""

    type: Literal["update_line"]
    line_id: str
    fields: LineFields


class RemoveLine(_Strict):
    """
    Delete cart line(s): one line by ``line_id``, or EVERY line of a product by ``sku`` ("kurti nathi joti" with two kurti lines).
    At least one of the two is required.
    """

    type: Literal["remove_line"]
    line_id: Optional[str] = None
    sku: Optional[str] = None

    @model_validator(mode="after")
    def _needs_a_target(self) -> "RemoveLine":
        """A removal without a line_id and without a sku points at nothing."""
        if not (self.line_id or self.sku):
            raise ValueError("remove_line needs a line_id or a sku")
        return self


class SplitLine(_Strict):
    """Split one line into several (e.g. 3 kurtis → 2 red + 1 green). The parts' qty must add up to the line's qty."""

    type: Literal["split_line"]
    line_id: str
    parts: list[SplitPart] = Field(min_length=1)


class SetCustomer(_Strict):
    """Record the customer's name, delivery address and/or contact number, exactly as they wrote them."""

    type: Literal["set_customer"]
    name: Optional[str] = None
    address: Optional[str] = None
    mobile: Optional[str] = None      # only needed on channels where the sender id is not a phone number (Instagram)


class SetPayment(_Strict):
    """Record the payment method the customer chose."""

    type: Literal["set_payment"]
    method: Literal["COD", "UPI"]


class AskClarify(_Strict):
    """Ask the customer one question. ``options`` must be real catalogue values (the engine replaces invented ones)."""

    type: Literal["ask_clarify"]
    question: str = Field(default="", max_length=300)
    options: list[str] = Field(default_factory=list, max_length=12)
    slot: Optional[Literal[CLARIFY_SLOTS]] = None  # type: ignore[valid-type]


class RequestConfirmation(_Strict):
    """Show the order summary and ask for confirmation (only when the cart is complete)."""

    type: Literal["request_confirmation"]


class ConfirmOrder(_Strict):
    """The customer explicitly confirmed the summary that is on screen."""

    type: Literal["confirm_order"]


class Cancel(_Strict):
    """The customer wants to drop the current cart (or, with an empty cart, the order awaiting payment)."""

    type: Literal["cancel"]


class HandoffHuman(_Strict):
    """Pass the conversation to a human."""

    type: Literal["handoff_human"]
    reason: str = Field(default="", max_length=200)


class AnswerQuestion(_Strict):
    """Answer a policy / facts question from the shop's own data."""

    type: Literal["answer_question"]
    topic: Literal[QUESTION_TOPICS]  # type: ignore[valid-type]


Action = Annotated[
    Union[
        SearchCatalog, BrowseCatalog, ShowProduct, SendImages, AddLine, UpdateLine, RemoveLine, SplitLine, SetCustomer,
        SetPayment, AskClarify, RequestConfirmation, ConfirmOrder, Cancel, HandoffHuman, AnswerQuestion,
    ],
    Field(discriminator="type"),
]

_ACTION_CLASSES: dict[str, type[_Strict]] = {
    "search_catalog": SearchCatalog, "browse_catalog": BrowseCatalog, "show_product": ShowProduct, "send_images": SendImages, "add_line": AddLine,
    "update_line": UpdateLine, "remove_line": RemoveLine, "split_line": SplitLine, "set_customer": SetCustomer,
    "set_payment": SetPayment, "ask_clarify": AskClarify, "request_confirmation": RequestConfirmation,
    "confirm_order": ConfirmOrder, "cancel": Cancel, "handoff_human": HandoffHuman, "answer_question": AnswerQuestion,
}


class PlannerOutput(_Strict):
    """What the planner returns: the actions to run (in order) and a coarse intent for turns with no action."""

    actions: list[Action] = Field(default_factory=list, max_length=8)
    reply_intent: Literal[REPLY_INTENTS] = "none"  # type: ignore[valid-type]

    @field_validator("actions")
    @classmethod
    def _at_most_one_terminal(cls, actions: list) -> list:
        """A plan may end the turn at most once (confirm / cancel / handoff are mutually exclusive)."""
        terminal = [a for a in actions if a.type in ("confirm_order", "cancel", "handoff_human")]
        if len(terminal) > 1:
            raise ValueError("at most one of confirm_order / cancel / handoff_human per plan")
        return actions


class ActionIssue(BaseModel):
    """A structured, planner-readable problem with ONE action (also what the repair loop sends back)."""

    index: int
    action_type: str
    code: str
    message: str
    valid_options: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form for the planner prompt / logs (empty parts omitted)."""
        out: dict[str, Any] = {"action_index": self.index, "action": self.action_type, "code": self.code, "message": self.message}
        if self.valid_options:
            out["valid_options"] = self.valid_options
        if self.details:
            out["details"] = self.details
        return out


# ── wire format ──────────────────────────────────────────────────────────────

def _nullable(kind: str) -> dict[str, Any]:
    """``{"type": [kind, "null"]}`` — structured-output strict mode needs every key present, so optional = nullable."""
    return {"type": [kind, "null"]}


def _variant_schema() -> dict[str, Any]:
    """Flat variant object (all three keys present, nullable)."""
    return {
        "type": ["object", "null"], "additionalProperties": False, "required": ["color", "size", "material"],
        "properties": {"color": _nullable("string"), "size": _nullable("string"), "material": _nullable("string")},
    }


def wire_schema() -> dict[str, Any]:
    """
    JSON schema for the planner's structured output (Groq ``response_format: json_schema``, strict).

    One flat object per action: ``type`` picks the action, the keys that action does not use are ``null``.
    """
    fields_schema = {
        "type": ["object", "null"], "additionalProperties": False, "required": ["color", "size", "material", "qty"],
        "properties": {
            "color": _nullable("string"), "size": _nullable("string"), "material": _nullable("string"),
            "qty": _nullable("integer"),
        },
    }
    part_schema = {
        "type": "object", "additionalProperties": False, "required": ["variant", "qty"],
        "properties": {"variant": _variant_schema(), "qty": {"type": "integer"}},
    }
    props: dict[str, Any] = {
        "type": {"type": "string", "enum": list(ACTION_TYPES)},
        "query": _nullable("string"), "category": _nullable("string"), "sort": {"type": ["string", "null"], "enum": ["bestseller", None]},
        "sku": _nullable("string"),
        "variant": _variant_schema(), "qty": _nullable("integer"), "line_id": _nullable("string"),
        "fields": fields_schema,
        "parts": {"type": ["array", "null"], "items": part_schema},
        "name": _nullable("string"), "address": _nullable("string"), "mobile": _nullable("string"),
        "method": {"type": ["string", "null"], "enum": ["COD", "UPI", None]},
        "question": _nullable("string"),
        "options": {"type": ["array", "null"], "items": {"type": "string"}},
        "slot": {"type": ["string", "null"], "enum": [*CLARIFY_SLOTS, None]},
        "reason": _nullable("string"),
        "topic": {"type": ["string", "null"], "enum": [*QUESTION_TOPICS, None]},
    }
    action = {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}
    return {
        "type": "object", "additionalProperties": False, "required": ["actions", "reply_intent"],
        "properties": {
            "actions": {"type": "array", "items": action},
            "reply_intent": {"type": "string", "enum": list(REPLY_INTENTS)},
        },
    }


def _s(**extra: Any) -> dict[str, Any]:
    """{"type": "string", …}."""
    return {"type": "string", **extra}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    """A closed object with every property required (strict structured output)."""
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def _grouped_branches(branches: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """
    One ``anyOf`` branch per distinct field set: actions with identical fields (show_product / send_images → ``sku``;
    request_confirmation / confirm_order / cancel → nothing) share a branch whose ``type`` enum lists them — a smaller schema
    (the schema is part of every planner prompt and counts against the provider's tokens-per-minute cap) that allows exactly the same plans.
    """
    groups: dict[str, tuple[list[str], dict[str, Any]]] = {}
    for name, props in branches.items():
        names, _ = groups.setdefault(json.dumps(props, sort_keys=True), ([], props))
        names.append(name)
    return [_obj({"type": _s(enum=names), **props}) for names, props in groups.values()]


def union_wire_schema() -> dict[str, Any]:
    """
    The typed form of the plan schema: ``actions`` is an ``anyOf`` with ONE branch per action, each requiring exactly its own
    fields (non-null where the engine needs them).

    The flat ``wire_schema`` lets the model emit ``{"type": "split_line"}`` with every other key null — valid for the schema,
    useless to the engine (observed live: INVALID_ACTION@0 on "I want to buy 2 xl and 3 M size"). Here a ``split_line`` without
    ``line_id`` and ``parts`` is not a valid generation at all. Used first; ``call_model`` falls back to the flat schema, then to
    plain JSON mode, if the provider refuses it.
    """
    nul_s = {"type": ["string", "null"]}
    variant = _obj({"color": nul_s, "size": nul_s, "material": nul_s})
    fields = _obj({"color": nul_s, "size": nul_s, "material": nul_s, "qty": {"type": ["integer", "null"]}})
    part = _obj({"variant": variant, "qty": {"type": "integer"}})
    branches: dict[str, dict[str, Any]] = {
        "search_catalog": {"query": nul_s},
        "browse_catalog": {"category": nul_s, "sort": {"type": ["string", "null"], "enum": ["bestseller", None]}},
        "show_product": {"sku": _s()},
        "send_images": {"sku": _s()},
        "add_line": {"sku": _s(), "variant": variant, "qty": {"type": ["integer", "null"]}},
        "update_line": {"line_id": _s(), "fields": fields},
        "remove_line": {"line_id": nul_s, "sku": nul_s},
        "split_line": {"line_id": _s(), "parts": {"type": "array", "minItems": 1, "items": part}},
        "set_customer": {"name": nul_s, "address": nul_s, "mobile": nul_s},
        "set_payment": {"method": _s(enum=["COD", "UPI"])},
        "ask_clarify": {"question": _s(), "options": {"type": "array", "items": _s()}, "slot": _s(enum=list(CLARIFY_SLOTS))},
        "request_confirmation": {},
        "confirm_order": {},
        "cancel": {},
        "handoff_human": {"reason": _s()},
        "answer_question": {"topic": _s(enum=list(QUESTION_TOPICS))},
    }
    assert set(branches) == set(ACTION_TYPES), "every action needs a branch"
    return {
        "type": "object", "additionalProperties": False, "required": ["actions", "reply_intent"],
        "properties": {
            "actions": {"type": "array", "items": {"anyOf": _grouped_branches(branches)}},
            "reply_intent": _s(enum=list(REPLY_INTENTS)),
        },
    }


def _strip_nulls(value: Any) -> Any:
    """Recursively drop ``None`` values from dicts (the flat wire form carries them for unused keys)."""
    if isinstance(value, dict):
        return {k: _strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_nulls(v) for v in value]
    return value


def _drop_empty_extras(item: dict[str, Any], cls: type[_Strict]) -> dict[str, Any]:
    """
    Remove keys the action does not have when they carry nothing (``None`` / ``{}`` / ``[]`` / ``""``).

    The flat fallback schema makes the model fill every key; an empty object for a key this action lacks
    (``"fields": {}`` on a search) is noise, not an error. A key with real content is still rejected by the closed schema.
    """
    allowed = set(cls.model_fields)
    return {k: v for k, v in item.items() if k in allowed or v not in (None, {}, [], "")}


def parse_planner_output(raw: Any) -> tuple[PlannerOutput, list[ActionIssue]]:
    """
    Turn the model's JSON into a ``PlannerOutput`` plus per-action issues.

    Valid actions are kept (in order); each malformed one yields an ``ActionIssue(code="INVALID_ACTION")`` naming its
    index so the repair loop can tell the planner exactly what to fix. A non-object / missing ``actions`` is one issue
    at index -1.
    """
    issues: list[ActionIssue] = []
    if not isinstance(raw, dict) or not isinstance(raw.get("actions"), list):
        return PlannerOutput(), [ActionIssue(
            index=-1, action_type="plan", code="INVALID_PLAN", message='Return {"actions": [...], "reply_intent": "..."}.',
        )]
    actions: list[Any] = []
    for i, item in enumerate(raw["actions"]):
        item = _strip_nulls(item) if isinstance(item, dict) else item
        kind = item.get("type") if isinstance(item, dict) else None
        cls = _ACTION_CLASSES.get(kind)
        if cls is not None:
            item = _drop_empty_extras(item, cls)
        if cls is None:
            issues.append(ActionIssue(
                index=i, action_type=str(kind), code="INVALID_ACTION", message=f"Unknown action type {kind!r}.",
                valid_options=list(ACTION_TYPES),
            ))
            continue
        try:
            actions.append(cls.model_validate(item))
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ())) or "action"
            issues.append(ActionIssue(
                index=i, action_type=kind, code="INVALID_ACTION", message=f"{where}: {first.get('msg', 'invalid')}",
            ))
    intent = raw.get("reply_intent") if raw.get("reply_intent") in REPLY_INTENTS else "none"
    try:
        return PlannerOutput(actions=actions[:8], reply_intent=intent), issues
    except ValidationError as exc:
        issues.append(ActionIssue(index=-1, action_type="plan", code="INVALID_PLAN", message=exc.errors()[0].get("msg", "invalid")))
        return PlannerOutput(actions=[], reply_intent=intent), issues
