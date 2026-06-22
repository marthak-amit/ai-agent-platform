# Fix Prompt — conv=52 run (lost ₹6,500 order: address wall + escalation trap)

Paste to your coding agent. Codebase: FastAPI webhook (`app.routers.webhook`,
`app.services.conversation_flow`, `app.routers.conversations`,
`app.services.order_service`). Model: `llama-3.3-70b-versatile`.

**Rules:** Show me the diff + file/function before each fix. Don't change price math or
`mark_order_paid`. Work P0 → P2. After each P0, replay the captured conv=52 lehenga
transcript and confirm the check passes.

**Context:** A real buyer ordering a ₹6,500 lehenga gave a valid Rajkot address, was
rejected 4 times, hit the attempt cap, got dumped to human takeover, and then got stuck
in a resume→re-escalate loop. The order died. This is the highest-severity class yet —
it directly loses paying customers. Fix the address + escalation handling first.

---

## P0-1 — Address validator rejects valid input + can't use profile address

**Symptom (L298-335):**
```
"Change to 100 somerest, rajkot"              → rejected (attempt 1)
"Pincode 35001"                               → rejected (attempt 2)
"New road, near shiv temple, rajkot 35001"    → rejected (attempt 3)  ← looks valid
"Please use my old address"                   → rejected (attempt 4)  ← should reuse profile
→ attempt cap → escalate to human, order dead
"...rajkot 350010, house new one"             → too late, already paused
```

**Fixes:**
1. **"change to X" / "change it to X" / "use X instead":** strip the change-intent prefix
   and validate the REMAINDER as the new address. Don't reject the whole message.
2. **"use my old address" / "same as before" / "previous address":** map to the customer's
   profile address (the buyer already had `702 Somerest, Ahmedabad` on file) and fill the
   slot. Never reject a request to reuse the known address.
3. **Pincode validation must be specific AND must tell the customer what's wrong.** `35001`
   is 5 digits → invalid. The reject message must say so: "That pincode looks short — I
   need a 6-digit pincode. What's the full address with pincode?" The buyer fixed it to
   `350010` one turn later — they would have succeeded if the message had told them the
   problem.
4. **Accept a real address.** "New road, near shiv temple, rajkot 350010" (street +
   landmark + city + 6-digit pincode) must PASS. Require: some street/area text + city + a
   valid 6-digit pincode. Don't demand a house number if street+landmark+city+pincode are
   present.
5. Address validation/rejection must be **deterministic — no LLM call** (the log shows a
   Groq call per address attempt; remove it).

**Check:** Replay the 5 address messages. "change to…" extracts the address; "use my old
address" fills the profile address; the 5-digit pincode reply explains the 6-digit need;
"…rajkot 350010" passes. No escalation. Order proceeds.

---

## P0-2 — Escalation trap: resume instantly re-escalates (dead-air loop)

**Symptom (L335, L358-389):** after the cap escalated to human and AI paused, the operator
resumed AI (`AI resumed for conversation 52`), but the very next customer message hit
`SLOT cap … attempts=4 … escalating to human` again and re-paused. The attempt counter
was never cleared on resume, so resume → re-escalate → pause, forever. The customer's
"yes", "Is choli available?" etc. were all "saved silently" into dead air.

**Fixes:**
1. On AI resume (`PATCH /conversations/{id}/resume`), **reset the per-slot attempt
   counter(s)** and clear the escalation flag for that conversation. Resume must give the
   bot a clean slate, or it instantly re-trips the cap.
2. After resume, the next customer message must be processed normally (re-prompt the
   pending slot), not measured against the old attempt count.
3. Add a guard: a conversation in `human_takeover`/paused state should not increment slot
   attempts at all while paused.

**Check:** Cap → escalate → operator resumes → customer sends a new address → bot processes
it (attempt counter starts fresh), no immediate re-escalation, no dead-air loop.

---

## P0-3 — Raise/soften the address cap so trying customers aren't cut off

**Symptom:** 4 address attempts → hard human escalation killed a live sale. The buyer was
clearly trying (each message was a real attempt), but the rejections didn't guide them, so
they burned attempts and got cut off.

**Fixes:**
1. Don't count an attempt as "wasted" if the rejection didn't tell the customer what to
   fix. Pair every rejection with a specific, actionable reason (see P0-1.3).
2. Raise the address attempt cap (e.g. 4 → 6) AND make escalation a soft handoff: keep the
   order alive and resumable, never a dead end.
3. If escalating, send the customer a clear message ("Let me connect you to the team — your
   order is saved") rather than silence.

**Check:** A customer who eventually gives a valid address within the cap completes the
order; if they do escalate, the order is preserved and resumable.

---

## P1-4 — Offer-stage variant message ignored + greedy re-pin wipes slots

**Symptom (L77-100, L105-131):** at the offer/product_inquiry stage (product already pinned
to LH10042), "Pink and xl" and then "Pink" each triggered `Name-match pin … (was LH10042,
score=1) — reset all order slots` and re-showed the SAME product card. The colour/size were
discarded. The buyer had to say Pink/XL three times and lost their XL choice (ended on M).

**Fixes:**
1. **Don't re-run product name-matching when a product is already pinned and the message is
   a variant answer or an affirmative.** At offer stage with a pinned SKU: if the message
   contains colour/size values for that SKU (or an affirmative), treat it as "start order +
   capture variants" (multi-slot extract, DB-validated), not as a new product search.
2. **Never reset order slots on a re-pin to the SAME SKU.** Only clear slots when the SKU
   actually CHANGES. A no-op re-pin (was LH10042 → LH10042) must not wipe state.
3. Make the name-match score threshold high enough that a bare variant word like "Pink"
   doesn't weakly re-match the current product (score=1) and trigger a reset.

**Check:** "Pink and xl" right after the card → order starts with colour=Pink, size=XL
filled in one turn (XL preserved), bot asks only Quantity. No card re-show, no slot reset.

---

## P2-5 — Remove LLM calls from deterministic paths (cost)

**Symptom:** offer-stage re-pins and every address attempt fired Groq calls (L80, L84,
L106, L110, L138, L299, L308, L317, L326) for work that is deterministic.

**Fix:** Address validation, variant capture, and re-pin decisions are all deterministic —
remove the LLM calls from these paths. Same recurring rule: delete the LLM detour, don't
add logic beside it.

**Check:** A clean lehenga order (incl. address change) makes zero LLM reply calls; only
genuine free-text/product-discovery may call the model.

---

### Suggested order
P0-1 (accept valid addresses + reuse profile) → P0-2 (kill resume re-escalate loop) →
P0-3 (soften cap, guide the customer) → P1-4 (offer-stage variants + greedy re-pin) →
P2-5 (strip LLM from deterministic paths).

### Note
P0-1/2/3 are about **not losing a paying customer**. The bot rejected a valid address,
gave no usable reason, cut the buyer off at the cap, then trapped them in a resume loop —
a ₹6,500 order lost to validation strictness + a state bug. Prioritise these over
everything: a lost sale costs far more than any LLM call.
