# Improvement Prompt — WhatsApp Order Bot (conv=52 log review)

Paste this to your coding agent. Codebase: FastAPI webhook (`app.routers.webhook`,
`app.services.gemini_service`, `app.services.knowledge_service`,
`app.services.learning_service`, `app.services.order_service`, `app.services.cost_log`).
Model in use: `llama-3.3-70b-versatile` via Groq.

**Ground rules for the agent**
- Show me the diff and the file/function before applying each change.
- Do NOT change order math, pricing, or `mark_order_paid` logic.
- Work the list in priority order (P0 first). Each item has an acceptance check.
- After each fix, re-run the conv=52 transcript mentally and confirm the check passes.

---

## P0-1 — Cost report is undercounting (meter ALL LLM calls)

**Problem:** Every order-stage message triggers a Groq call to classify intent /
extract the slot (a `POST .../groq...` appears right before each
`intent=ANSWER next_slot=...`). The cost report does NOT count these — it tags the
message `TEMPLATE` with `TOK=-` and `₹0.0000`. Example: ORD-2026-0013 reports
`tokens: 31782 in / 104 out, total ₹1.56`, but the slot-extraction calls for
"gold", "L", "4", "yes", "orange" are invisible. Each reuses the same ~6,300-token
input prompt, so real cost is roughly 2x reported.

**Fix:**
1. Instrument EVERY Groq call site (intent classifier, slot extractor, final reply
   generator) to emit `{call_kind, model, in_tok, out_tok, cost}` where `call_kind ∈
   {classify, extract, reply}`. Pull `in_tok/out_tok` from `usage.prompt_tokens /
   usage.completion_tokens` on the Groq response — do not estimate.
2. Append each to the per-conversation cost log, not just the final reply.
3. In the report, a row's `PATH` = `TEMPLATE` only if ZERO Groq calls happened for
   that inbound message. If a classify/extract call fired, show it (e.g.
   `TEMPLATE+CLASSIFY`) with real tokens/₹. Add a footer line:
   `classify calls: N (₹X) | extract calls: N (₹X) | reply calls: N (₹X)`.

**Check:** ORD-0013 report total must rise to include the per-slot classify/extract
calls; no order-stage message that hit Groq may show `₹0.0000`.

---

## P0-2 — Stop sending trivial slot inputs to the LLM (kill classify/extract calls)

**Problem:** Single-token slot answers ("gold", "L", "4", "orange", "yes") each fire
an LLM classify+extract call. These are deterministic.

**Fix:** Before any LLM call in `order_collection`, run a deterministic resolver:
- **color:** case-insensitive match against `available_colors`. No match → re-prompt
  with the OOS template (already exists). Never call LLM for color.
- **size:** match against `available_sizes` (handle "xl"/"XL", "large"→"L" via a small
  alias map). No match → re-prompt.
- **quantity:** parse integer from the message (strip words, accept "2 joi che" → 2).
  Fail → re-prompt.
- **yes/no/confirm:** keyword set (`yes/ha/haan/ok/okay/confirm/done/sure/ho` etc.).
- Only fall through to the LLM extractor when the deterministic pass is ambiguous
  (free-text address, or a slot answer mixed with a question).

**Check:** A clean happy-path order (color→size→qty→address→confirm→paid) makes **zero**
LLM calls except, at most, one for the free-text address. The conv=52 second order
(gold/L/4) should show 0 classify calls after the fix.

---

## P0-3 — Second-order break: lost product pin + affirmative routed to LLM

**Problem:** After order completion, reset clears the pinned SKU. On the next order the
user sees a product card and replies "yes", but bare "yes" at `product_inquiry` routes
to `LLM (reason=open_browsing)`, the model loses the product, and replies generic
"Which item are you interested in?" (transcript L487). User must re-type the product
name to recover. This is the "memory" symptom.

**Fix:**
1. Persist `last_shown_sku` on the conversation across the completion reset (separate
   from order slots, which you may still clear).
2. When stage is `product_inquiry`/`greeting` AND `last_shown_sku` is set AND the
   message is an affirmative (yes/haan/sure/order karo…): deterministically pin that
   SKU, move to `order_collection`, and emit the colour prompt. **Never** send a bare
   affirmative to the LLM when a product is on screen.
3. Route ALL product-name/SKU messages through the existing `Name-match pin` path
   FIRST (it's deterministic and free — see `reason=fix1_pinned_known_fact`). LLM only
   if name-match scores below threshold.

**Check:** Replay second order: card shown → "yes" → bot asks "Colour?" with no LLM
call and no "Which item?" detour.

---

## P0-4 — Browsing-stage guard fires AFTER paying for the LLM

**Problem:** `Browsing-stage transactional output blocked` (L433) discards the LLM reply
*after* the call already cost tokens.

**Fix:** Make the guard a pre-check. Decide "this message in this stage must not produce
transactional output" BEFORE calling the LLM, and serve the deterministic reply instead.
If the LLM must be called, constrain via prompt so the blocked-and-replaced path becomes
rare.

**Check:** No log line shows a blocked/replaced reply that was preceded by a Groq reply
call for the same message.

---

## P1-5 — Change-address drops the first new-address message

**Problem:** "I want to change my address" → bot asks for address and sets address=None
+ reverts stage. The next message with the new address is dropped once: bot re-shows the
OLD address ("deliver to: 702 Somerest?" at L726), and the user must resend the same
address (L733) before it sticks.

**Fix:** On change-address intent, set a flag `awaiting_new_address=True` and stage to
the address-collection step. The very next inbound must fill `delivery_address` directly
(run it through the validator in P1-6) — do NOT bounce back to a confirmation that
re-reads the previous address. Clear the flag once filled.

**Check:** change-address → send new address once → bot confirms with the NEW address. No
double send.

---

## P1-6 — Address validator: strip junk, validate pincode, reject intents

**Problem:** Stored `address='okay, 800 somerest, ahmedabad, 350012'` — the "okay,"
prefix was saved verbatim, and `350012` is not a valid Ahmedabad pincode. Separately,
"change to 800 somerest, ahmedabad" was (correctly) rejected as a change-intent, but the
cleanup is inconsistent.

**Fix:**
- Strip leading filler before storing: `^(okay|ok|yes|sure|haan|ha|change( it)?( to)?|its|it's)[ ,:-]*`.
- Require: house/number + area + city + a 6-digit pincode. Missing pincode → re-prompt
  (you already have the Gujarati re-prompt template).
- Validate pincode is 6 digits and numeric; optional: warn if first 3 digits don't match
  the city's known prefix (Ahmedabad = `38x`), but don't hard-block on region.
- Reject messages that are questions or pure intents ("how much delivery?", "change it
  to…") — store nothing, re-prompt.

**Check:** "okay, 800 somerest, ahmedabad, 350012" → stored as
`800 somerest, ahmedabad, 350012` (no "okay,"), and an invalid/short pincode re-prompts
instead of saving.

---

## P1-7 — Multi-slot message: parse all slots in one pass

**Problem:** "green and xl size" filled only color=Green; size was dropped and later
mis-set to S, and `available_sizes` collapsed to `['S']` (L7–L18).

**Fix:** In the deterministic resolver (P0-2), scan the whole message for every slot
present — color AND size AND quantity — and fill all matches in one turn. Then prompt
only for the still-empty slots. Never collapse `available_sizes` based on a partial
parse.

**Check:** "green and xl size" → color=Green, size=XL filled together → bot asks only
"Quantity?".

---

## P2-8 — Lock conversation language

**Problem:** Language flips english→gujarati_roman→hindi_roman within one conversation
(confirmation in Gujarati at L104/L106, success in Hindi at L220). Looks inconsistent.

**Fix:** Detect language once with confidence on the first 1–2 substantive user messages,
store `conv.lang`, and reuse it for all templated replies. Re-detect only if the user
clearly switches for several consecutive messages.

**Check:** A single order's templated replies all use one language.

---

## P2-9 — Expand reusable templates (the cost lever)

Route these through deterministic templates / known-fact lookups, never the LLM:
- greeting / welcome-back
- thanks & closing ("thank you" → "You're welcome 😊")
- catalog link request
- product card by name or SKU (via Name-match pin)
- price / availability / "is it good quality" → known-fact from product KB
- all slot prompts + OOS / invalid-option re-prompts

**Check:** In a replay of conv=52, "thank you", "give me cataloge", "kurti", "I want to
buy choli", "yes but Is it good in quality?" all resolve without an LLM reply call.

---

## P2-10 — Cut the per-call input size (single biggest ₹ lever)

**Problem:** Every LLM call sends ~6,300 input tokens (`history_len:10` + a fat system
prompt). Output is tiny; input dominates the bill.

**Fix:** Trim the system prompt to essentials; cap history to the last 4–6 turns or send
a short running summary instead of raw history; drop redundant product context when it's
already pinned deterministically. Measure in/out tokens before vs after via the P0-1
instrumentation.

**Check:** Median LLM `in_tok` drops well below 6,300 with no quality regression on the
genuine free-text answers.

---

### Suggested order of work
P0-1 (so you can SEE real cost) → P0-2 & P0-3 (kill most calls + fix the break) → P0-4
→ P1-5/6/7 → P2-8/9/10. Re-run the conv=52 transcript after each P0 item and paste me the
new cost report so we confirm the numbers moved.
