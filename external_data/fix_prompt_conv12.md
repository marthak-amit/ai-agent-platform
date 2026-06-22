# Fix Prompt — conv=52 run (JSON crash, OOS trap, path inconsistency)

Paste to your coding agent. Codebase: FastAPI webhook (`app.routers.webhook`,
`app.services.gemini_service`, `app.services.conversation_flow`,
`app.services.knowledge_service`, `app.services.order_service`).
Model: `llama-3.3-70b-versatile`, fallback `llama-3.1-8b-instant`.

**Rules:** Show me the diff + file/function before applying each fix. Don't change
order math or `mark_order_paid`. Work in the order below — P0 first. After each P0,
replay the captured conv=52 transcript and confirm the check passes.

---

## P0-1 — JSON response_format 400 crash (customer-facing failure)

**Symptom (L501-502):**
```
400: 'messages' must contain the word 'json' in some form, to use
'response_format' of type 'json_object'
ERROR: AI processing error
```
The new JSON-output path crashes because Groq/OpenAI requires the literal token
"json" somewhere in the prompt when `response_format={'type':'json_object'}` is set.
It also fired right after a 429 fallback, so BOTH the primary and fallback model paths
use the broken format and both 400.

**Fix:**
1. In every call site that sets `response_format=json_object`, ensure the system (or
   user) prompt contains the word "json" — e.g. end the system prompt with: `Respond
   ONLY with a valid JSON object.` Centralise this so it can't be missed on any path.
2. Apply the same to the fallback model call — it must carry the identical prompt +
   format, or explicitly drop `response_format` on fallback.
3. Wrap the JSON call: on 400/parse-fail, retry once; on second failure, fall back to a
   **deterministic re-prompt** of the current slot/state. NEVER surface a raw error or
   empty reply to the customer.
4. Add a startup/CI assert: if a request sets `response_format=json_object` and the
   prompt has no "json" token, fail fast in dev (not in front of a customer).

**Check:** Replay "444" / any message that routes to the JSON path → valid JSON or a
clean deterministic re-prompt. No `AI processing error`. No empty WhatsApp reply.

---

## P0-2 — Partial-SKU pick "444" not matched, then crashes

**Symptom (L493-502):** after the bot listed `Kurti best [KU23444]` and
`kurti new one [KU76326]`, the customer typed `444` to pick KU23444. No deterministic
match fired → routed to LLM → 429 → JSON 400 → error. A buying customer got nothing.

**Fix:**
1. When the previous bot message offered a numbered/SKU list, store those candidate SKUs
   on the conversation (`last_offered_skus`).
2. On the next message, deterministically resolve a pick BEFORE any LLM: match against
   `last_offered_skus` by (a) full SKU, (b) trailing digits / partial SKU ("444" →
   KU23444), (c) list position ("1"/"2"/"first"/"second"), (d) distinctive name word
   ("best"/"new"). Unambiguous → pin that SKU, go to `order_collection`.
3. Ambiguous or no match → re-show the same 2-option list via template (free), don't call
   the LLM.

**Check:** "444" after the kurti list → pins KU23444 deterministically, asks next slot.
No LLM call, no crash.

---

## P1-3 — OOS trap: customer loyal to a value gets walled (green loop)

**Symptom (L89, L104, L129-130):** "green xl" → green+XL is OOS. System KEEPS size=XL,
CLEARS colour, and only ever asks for colour. Customer retyped "Green" and got the exact
same rejection again — no way to keep green by changing size. They escaped only by
guessing "Pink".

**Fix:**
1. On a combo-OOS, do NOT silently keep one slot and force-change the other. Offer BOTH
   doors in one reply, rendered from DB:
   > "Green isn't available in XL. Green comes in: {sizes_for_green}. XL comes in:
   > {colours_for_XL}. Which way — keep Green (pick a size) or keep XL (pick a colour)?"
2. Detect repeat: if the customer re-sends the same OOS value, do NOT repeat the same
   wall — escalate to the both-doors message (or offer the closest in-stock combo).
3. Track which slot the customer is anchoring on (the value they keep repeating) and keep
   THAT one, clear the other.

**Check:** green+XL OOS → both-doors reply. Customer repeats "Green" → bot offers green's
available sizes, not the same rejection. No infinite loop.

---

## P1-4 — Identical multi-match handled two different ways (Choli vs Kurti)

**Symptom:** "Kurti" (2 matches) → deterministic 2-option list, free (L485-488). "Choli"
(2 matches) → routed to LLM, which silently collapsed to ONE product (L407, L442). Same
situation, two code paths, one of them paid + lossy.

**Fix:**
1. Route ALL `Name-match multi (N options)` results through the SAME deterministic option
   lister that handled "Kurti". Never hand a multi-match to the LLM to pick silently.
2. Store the listed SKUs as `last_offered_skus` (feeds P0-2).
3. Only one product surfaced anywhere — list path or single-match path — updates
   `last_shown_sku`, in one place.

**Check:** "Choli" with 2 matches → same 2-option template list as "Kurti", free, no LLM,
no silent single-pick.

---

## P1-5 — Off-topic handled inconsistently (Flutter free, Modi paid)

**Symptom:** "What is flutter?" → OFF_TOPIC idle short-circuit, free deflect (L351).
"Who is Narendra Modi?" → routed to LLM, paid ₹0.32 for an equivalent deflect (L360),
and political topics should never reach the model.

**Fix:**
1. Make the off-topic / non-shopping deflect deterministic and apply it to BOTH cases.
   The idle short-circuit that caught "flutter" must also catch general-knowledge /
   political / unrelated questions.
2. Add a hard rule: politics, news, public figures, and other clearly non-product topics
   → fixed deterministic deflect template, never an LLM call.
3. One deflect template, used everywhere, so wording is consistent.

**Check:** Both "What is flutter?" and "Who is Narendra Modi?" → same free deflect
template, zero LLM calls.

---

## P2-6 — available_sizes state inconsistent during OOS

**Symptom (L89-90):** at the same moment, the OOS dict reports sizes `['XL']` while
SLOT-DEBUG `variant_info.available_sizes` shows `['S']`; elsewhere the full list. The
available-options computation is producing conflicting values mid-OOS.

**Fix:**
1. Compute available colours/sizes from ONE function that reads in-stock variants for the
   pinned SKU given the currently-filled slots. Call it once per turn; use its output
   everywhere (SLOT-DEBUG, OOS dict, renderer).
2. Add a log assert that the OOS-dict options and the variant_info options match for the
   same turn.

**Check:** SLOT-DEBUG and the OOS message show the same option sets in a single turn.

---

## P2-7 — Verify delivery window "20 business days"

**Symptom:** earlier runs said "3–7 business days"; this run says "20 business days"
(L189, L421, L431). Confirm this is real client config, not a parse/config regression.

**Fix:** Trace where the delivery window is read. If it's client-configurable and 20 is
correct, leave it. If it flipped due to a bug/default, restore the correct value and
source it from one config field.

**Check:** Delivery window matches the client's configured value and is identical across
the aside-question answer, the order summary, and the success message.

---

### Suggested order
P0-1 (stop the crash) → P0-2 (the "444" buyer) → P1-3 (OOS trap) → P1-4 + P1-5 (kill the
two-path inconsistencies) → P2-6 → P2-7.

### Note
P0-1, P1-4, P1-5 are all the same disease: a second (LLM) path doing what a deterministic
path already does. Each fix should DELETE the LLM detour, not add logic beside it. Fewer
paths = fewer bugs = lower cost.
