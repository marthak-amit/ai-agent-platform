"""
Agent v3 — what a turn does when the planner is unusable (both models rate-limited / down) and the message is a bare yes / no / number.

The fast paths already took every yes / no / number that has an obvious reading. What reaches here is the rest, and the rule is: never
answer "Sorry, I didn't quite get that" to someone who just said "Yes" to our question (conv=60: a 429 on the planner turned a plain
"Yes" into an apology and lost the thread). Instead:

* a "yes" to a question that offered exactly ONE option takes that option;
* any other bare yes / no / number leaves the cart untouched and re-asks the question that is open (the renderer appends the next open
  step, or the browse prompt) — without the apology.

Everything else (free text) returns None and keeps the old behaviour: a catalogue word becomes a search, otherwise the open step is re-shown.
"""

from __future__ import annotations

from app.schemas.agent_actions import PlannerOutput
from app.services import agent_fast_paths as fp
from app.services import agent_text
from app.services import command_words as cw


def is_bare_reply(text: str) -> bool:
    """True when the whole message is a yes, a no or a number — the answers that only make sense against the question that is open."""
    t = (text or "").strip()
    return bool(t) and (cw.is_yes(t) or cw.matches_whole("no", t) or agent_text.is_bare_number(t) is not None)


def resolve(text: str, clarify: dict | None):
    """
    ``(PlannerOutput, grounded_by)`` for a bare reply the planner could not read, or None when the message is not one.

    The output has either the single offered option applied or no actions at all (= re-ask the open question).
    """
    if not is_bare_reply(text):
        return None
    clarify = clarify or {}
    options = clarify.get("options") or []
    if cw.is_yes(text) and len(options) == 1:
        path = fp._apply_option(clarify, options[0], "option")
        if path is not None:
            return PlannerOutput(actions=path.actions, reply_intent="none"), path.grounded_by
    return PlannerOutput(actions=[], reply_intent="none"), None
