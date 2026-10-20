"""
WhatsApp interactive-message label limits, applied in ONE place so a long product name never gets cut mid-word.

Meta rejects (or silently clips) anything longer: a reply button title ≤ 20 characters, a list row title ≤ 24, a list row
description ≤ 72, a list's button text ≤ 20 and its header ≤ 60. ``fit`` shortens at a word boundary and ends with "…"; text that
already fits is returned untouched.
"""

from __future__ import annotations

BUTTON_MAX = 20
ROW_TITLE_MAX = 24
ROW_DESCRIPTION_MAX = 72
LIST_BUTTON_MAX = 20
LIST_HEADER_MAX = 60
ELLIPSIS = "…"
# A word-boundary cut is used only when it keeps at least this share of the allowed length (else a hard cut: one very long word).
_MIN_KEEP_SHARE = 0.5


def fit(text: str | None, limit: int) -> str:
    """`text` shortened to at most `limit` characters: whole words + "…" (a single over-long word is cut and gets the "…" too)."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    if limit <= 1:
        return ELLIPSIS[:limit]
    head = text[: limit - 1]
    cut = head.rfind(" ")
    if cut >= int(limit * _MIN_KEEP_SHARE):
        head = head[:cut]
    return head.rstrip(" ,.;:-–—/(") + ELLIPSIS


def button(text: str | None) -> str:
    """A reply-button title (≤ 20)."""
    return fit(text, BUTTON_MAX)


def row_title(text: str | None) -> str:
    """A list-row title (≤ 24)."""
    return fit(text, ROW_TITLE_MAX)


def row_description(text: str | None) -> str:
    """A list-row description (≤ 72)."""
    return fit(text, ROW_DESCRIPTION_MAX)


def list_button(text: str | None) -> str:
    """The label of the button that opens a list (≤ 20)."""
    return fit(text, LIST_BUTTON_MAX)


def list_header(text: str | None) -> str:
    """A list header (≤ 60)."""
    return fit(text, LIST_HEADER_MAX)


# ── unique titles ────────────────────────────────────────────────────────────
# Meta rejects an interactive message whose button titles (or row titles) repeat — error #131009 — and two long names that start the
# same ("Floral Print Cotton Saree", "Floral Print Silk Saree") become the same string once each is shortened on its own. ``unique_labels``
# is the one place that guarantees distinct titles, so a caller never has to think about it.

_DISTINGUISH_SEP = " – "
_TRIM_EDGE = " ,.;:-–—/("


def _numbered(base: str, n: int, limit: int) -> str:
    """`base` shortened so that ' n' still fits within `limit`."""
    suffix = f" {n}"
    return fit(base, limit - len(suffix)).rstrip(ELLIPSIS) + suffix if len(base) + len(suffix) > limit else base + suffix


def _common_word_prefix(texts: list[str]) -> list[str]:
    """The words every text starts with."""
    split = [t.split() for t in texts]
    prefix: list[str] = []
    for words in zip(*split):
        if len({w.lower() for w in words}) != 1:
            break
        prefix.append(words[0])
    return prefix


def unique_labels(titles: list[str | None], limit: int) -> list[str]:
    """
    Titles (≤ `limit` characters each) that are pairwise different, case-insensitively.

    Titles that already fit and differ are returned untouched. Titles that collide only after shortening keep what tells them apart
    ("Floral Print… – Navy" / "Floral Print… – Purple"); titles that are identical, or that cannot be told apart in the space, get
    " 1", " 2" … appended.
    """
    originals = [" ".join((t or "").split()) for t in titles]
    out = [fit(t, limit) for t in originals]
    for _ in range(2):          # distinguish pass, then numbering pass
        groups: dict[str, list[int]] = {}
        for i, t in enumerate(out):
            groups.setdefault(t.lower(), []).append(i)
        clashes = [idx for idx in groups.values() if len(idx) > 1]
        if not clashes:
            return out
        for idx in clashes:
            members = [originals[i] for i in idx]
            if _ == 0 and len({m.lower() for m in members}) == len(members):
                prefix = _common_word_prefix(members)
                tails = [" ".join(m.split()[len(prefix):]).strip(_TRIM_EDGE) for m in members]
                head = " ".join(prefix).strip(_TRIM_EDGE)
                if head and all(tails) and len({t.lower() for t in tails}) == len(tails):
                    tails = [fit(t, max(6, limit // 2)) for t in tails]
                    room = limit - len(_DISTINGUISH_SEP) - max(len(t) for t in tails)
                    if room >= 4:
                        for i, tail in zip(idx, tails):
                            out[i] = fit(head, room) + _DISTINGUISH_SEP + tail
                        continue
            for n, i in enumerate(idx, start=1):
                out[i] = _numbered(originals[i] or out[i], n, limit)
    # Last resort (should be unreachable): numbering made them unique by construction, but keep the guarantee explicit.
    seen: set[str] = set()
    for i, t in enumerate(out):
        key = t.lower()
        n = 2
        while key in seen:
            t = _numbered(out[i], n, limit)
            key, n = t.lower(), n + 1
        out[i] = t
        seen.add(key)
    return out


def unique_buttons(titles: list[str | None]) -> list[str]:
    """Reply-button titles: ≤ 20 characters each and all different."""
    return unique_labels(titles, BUTTON_MAX)


def unique_row_titles(titles: list[str | None]) -> list[str]:
    """List-row titles: ≤ 24 characters each and all different."""
    return unique_labels(titles, ROW_TITLE_MAX)


# ── numbered text fallback ───────────────────────────────────────────────────

_NUMBER_HINT = {
    "english": "Reply with the number.",
    "hindi_roman": "Number bhejkar jawab dein.",
    "hindi_devanagari": "नंबर भेजकर जवाब दें।",
    "gujarati_roman": "Number mokline jawab aapo.",
    "gujarati_script": "નંબર મોકલીને જવાબ આપો.",
}


def numbered_fallback(body: str | None, labels: list[str], language: str | None = None) -> str:
    """
    The body of an interactive message as plain text with its options numbered — what the customer gets when WhatsApp refuses the
    buttons / list (error #131009 or any other 400). The options are not repeated when the body already names every one of them.
    """
    body = (body or "").strip()
    labels = [" ".join((label or "").split()) for label in labels if label]
    if not labels:
        return body
    lowered = body.lower()
    listed = "\n".join(f"{i}. {label}" for i, label in enumerate(labels, start=1))
    hint = _NUMBER_HINT.get((language or "english").lower(), _NUMBER_HINT["english"])
    already = all(label.lower().rstrip(ELLIPSIS) in lowered for label in labels) and any(f"{i}." in body for i in range(1, len(labels) + 1))
    return f"{body}\n\n{hint}" if already else f"{body}\n\n{listed}\n{hint}"
