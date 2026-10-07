# SellerTalk24 billing — go-live checklist

Prepaid Razorpay Orders API + Checkout.js. Customers pay a plan up front (Starter / Growth / Pro, 30 days), renew manually, upgrade mid-cycle with pro-rata credit, and are refunded through Razorpay.

**What has been verified:** the whole flow in **mock mode** (`backend/tests/replay/test_sellertalk24_billing_e2e_mock.py` + `test_sellertalk24_billing_refunds_admin.py`, real Postgres, real app, real HMAC signatures).
**What has NOT been verified:** anything against real Razorpay — the real `razorpay` SDK calls (including `order.payments`, used by reconcile), real webhook payload shapes, Checkout.js in a browser. Section 4 exists to close that gap in test mode *before* any live key is set.

Run the suites any time:

```bash
cd backend
venv/bin/pytest tests/replay/test_sellertalk24_billing_e2e_mock.py -v -s        # the narrated end-to-end
venv/bin/pytest tests/replay/test_sellertalk24_billing_refunds_admin.py -v      # refunds, admin, modes, reconcile, health
venv/bin/pytest tests/replay/test_billing_migration_0065.py -v                  # data migration on a populated DB
```

---

## 0. Read first — three things that will bite

1. **The webhook URL has no `/api` prefix.** The backend registers `POST /billing/webhook`. `/api/...` only exists in the Vite dev proxy (it strips `/api`); `frontend/vercel.json` has no such rewrite, so on Railway `https://<backend-domain>/api/billing/webhook` is a **404**. Use `https://<backend-domain>/billing/webhook`. (Confirm with the curl in §3.)
2. **Test and live money are now kept apart in the database — but they still share one database.** Every payment order, invoice and credit note carries a `mode` (`test` for the mock gateway and `rzp_test_` keys, `live` for `rzp_live_` keys), stamped at checkout. A live process **refuses to activate a test order, and vice versa** (`409 mode_mismatch`). Invoice and credit-note numbers are separate series per mode and financial year: test numbers are `TEST-ST24/2026-27/0001` / `TEST-ST24-CN/2026-27/0001`, live numbers are `ST24/2026-27/0001` / `ST24-CN/2026-27/0001` and **start at 0001 regardless of how many test invoices exist**. Migration 0065 marked every pre-existing order/invoice as test and re-labelled their numbers with the `TEST-` prefix. What it does *not* do: tenants created during testing keep their (test) subscriptions — see "Known gaps".
3. **`SELLERTALK24_BILLING_ENFORCE` must stay `false` until every existing client has a plan or is `billing_exempt`.** When on, any client without a running plan past grace gets the fixed "assistant unavailable" template for new customers. Client 1 is exempt by migration 0063; nobody else is.

---

## 1. Razorpay dashboard

### 1.1 Account activation (live mode)
- [ ] Sign up / log in at dashboard.razorpay.com; complete **Account & Settings → Business details** (legal name, business type, PAN, GSTIN if registered, registered address).
- [ ] **KYC**: PAN, address proof, authorised-signatory ID, **bank account** (penny-drop verification). Live keys are issued only after activation is approved.
- [ ] **Website details** — Razorpay reviews the site. Public pages that already exist on the marketing site and should be listed/linked:
  | Razorpay field | URL |
  |---|---|
  | Website | https://sellertalk24.com |
  | Pricing | https://sellertalk24.com/pricing |
  | Terms & Conditions | https://sellertalk24.com/terms |
  | Privacy Policy | https://sellertalk24.com/privacy |
  | Refund / Cancellation Policy | https://sellertalk24.com/refund-policy |
  | Shipping / Delivery Policy | https://sellertalk24.com/shipping-policy |
  | Contact Us | https://sellertalk24.com/contact |
- [ ] Reviewer checklist: pricing shown **"+ GST"** matches what Checkout charges (₹4,599 + 18% = ₹5,426.82 for Starter); refund policy text matches what the code does (**a full refund cancels the plan immediately and leaves 3 days of grace**); contact page has a working email/phone; the site is live over HTTPS.
- [ ] Enable the payment methods you want (Cards, UPI, Netbanking, Wallets) under **Account & Settings → Payment methods**.

### 1.2 API keys
- [ ] **Test mode** (toggle top-left): Account & Settings → API Keys → Generate Test Key → `rzp_test_…` id + secret.
- [ ] **Live mode** (after activation): Generate Live Key → `rzp_live_…` id + secret. The secret is shown **once** — store it in a password manager, not in chat/git.

### 1.3 Webhook (configure once in Test mode, once in Live mode — they are separate)
Account & Settings → **Webhooks** → Add New Webhook:

- [ ] **Webhook URL:** `https://<backend-domain>/billing/webhook` &nbsp;(Railway backend service domain; **not** the frontend, **not** `/api/…`)
- [ ] **Secret:** type a long random string (e.g. `openssl rand -hex 32`). This is **`RAZORPAY_WEBHOOK_SECRET`** — it is *not* the API key secret.
- [ ] **Alert email:** a mailbox you read (Razorpay emails you when deliveries keep failing).
- [ ] **Active events** — tick exactly these four:
  | Event | What the app does |
  |---|---|
  | `payment.captured` | verifies with Razorpay's API, activates the plan (idempotent) |
  | `order.paid` | same activation path (arrives alongside `payment.captured`; deduped) |
  | `payment.failed` | marks the order `failed` + stores Razorpay's reason; a retry on the same order can still succeed; never overrides `paid` |
  | `refund.processed` | issues a **credit note** (one per Razorpay refund id). **Full** refund → order `refunded`, the subscription it bought is **revoked immediately**, tenant gets a dashboard alert + email and enters the normal 3-day grace counted from the refund. **Partial** refund → credit note only, plan untouched, internal admin flag raised |
- Other events are acknowledged and ignored.
- [ ] Behaviour to know: the endpoint answers `400` for a bad/missing signature (Razorpay retries those for ~24h — fix the secret and they succeed) and `200` for everything else, including events it could not handle (recorded in `payment_events.error`, see §6). **More than 5 bad signatures in 10 minutes emails `BILLING_ALERT_EMAIL`.**

---

## 2. Deployment + Railway environment variables

### 2.0 How the backend deploys (checked in the repo)
Railway builds with the **Dockerfile** (`railway.json` → `"builder": "DOCKERFILE"`, `backend/Dockerfile`). The deploy runs `deploy.startCommand` from `railway.json`:

```
alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 2
```

`&&` means **a failed migration stops the start** (Railway then restarts up to 3 times and reports the deploy failed — the old release keeps serving). Every other entry point is chained the same way: `docker-entrypoint.sh` runs `alembic upgrade head` under `set -e`, and `backend/Procfile` is now `web: alembic upgrade head && uvicorn …` (it used to skip migrations; it is only used if someone switches Railway to Nixpacks/Heroku-style).

Belt and braces: at startup the app compares `alembic_version` with the code's head and logs `ERROR MIGRATION MISMATCH: database is at [...] but the code expects head [...]` if they differ (it does not block startup). Alert on that line.

### 2.1 Apply with `railway variables` or the dashboard, then **redeploy** (config is read at process start). The frontend needs **no** Razorpay variable — `key_id` is returned by `POST /billing/checkout`.

#### Test phase (staging or production-with-test-keys)

| Variable | Value | Notes |
|---|---|---|
| `RAZORPAY_MODE` | `test` | must match the key prefix or billing refuses to start |
| `RAZORPAY_KEY_ID` | `rzp_test_…` | |
| `RAZORPAY_KEY_SECRET` | test secret | |
| `RAZORPAY_WEBHOOK_SECRET` | the test webhook's secret | **empty ⇒ every webhook is rejected** |
| `SELLERTALK24_BILLING_MOCK` | *(unset)* | with keys present, mock turns off automatically. Don't set it to an empty `=` line (unset is safest) |
| `PRICES_INCLUDE_GST` | `false` | listed prices are GST-exclusive |
| `GST_RATE_BPS` | `1800` | 18% |
| `SELLER_STATE_CODE` | `24` | Gujarat ⇒ CGST+SGST for same-state buyers, IGST otherwise |
| `SELLERTALK24_BILLING_ENFORCE` | `false` | see §0.3 |
| `GRACE_DAYS` | `3` | also the grace after a refund / admin revoke |
| `BILLING_LEGACY_COUNTING` | `true` | old calendar-month counter keeps running one release for comparison |
| `BILLING_ALERT_EMAIL` | your ops inbox | operator alerts: amount mismatch, webhook-signature burst, reconcile activation. Empty = logged at ERROR only |
| `EMAIL_PROVIDER` | `console` | emails are only logged — safe for testing |
| `ADMIN_SECRET_KEY` | long random, **not** the default | guards `/admin/billing/*` and `/health/billing` |
| `SECRET_KEY` | long random, **not** the default | JWT signing |

#### Live phase (the delta)

| Variable | Value | Notes |
|---|---|---|
| `RAZORPAY_MODE` | `live` | |
| `RAZORPAY_KEY_ID` | `rzp_live_…` | |
| `RAZORPAY_KEY_SECRET` | live secret | |
| `RAZORPAY_WEBHOOK_SECRET` | the **live** webhook's secret | different from test |
| `SELLERTALK24_BILLING_MOCK` | **unset / `false`** | mock + live ⇒ every billing call returns 503 by design |
| `SELLER_LEGAL_NAME` | registered legal name | printed on every tax invoice |
| `SELLER_GSTIN` | your 15-char GSTIN | **required** — unset prints "—" on invoices |
| `SELLER_ADDRESS` | registered address | **required** on invoices |
| `SELLER_STATE_NAME` | `Gujarat` | |
| `INVOICE_PREFIX` | `ST24` (default) | live numbers `ST24/<FY>/NNNN`, credit notes `ST24-CN/<FY>/NNNN` |
| `BILLING_ALERT_EMAIL` | a monitored inbox | **must** be set before taking real money |
| `EMAIL_PROVIDER` | `smtp` | plus `EMAIL_FROM`, `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS=true`. With `console`, customers **and the operator alerts** are only logged |
| `SELLERTALK24_BILLING_ENFORCE` | `false` first; `true` only per §5 step 9 | |

### 2.2 Deploy prerequisites
- [ ] Migrations **0062–0065** apply cleanly (`alembic upgrade head` is part of the deploy; check the deploy log for `Running upgrade 0064 -> 0065`).
- [ ] Frontend build has `VITE_API_URL` pointing at the backend (`frontend/vercel.json` only does the SPA rewrite — without `VITE_API_URL` the app calls `/api/...` on the Vercel domain and billing 404s).
- [ ] Backend is reachable publicly over HTTPS (Razorpay won't call HTTP or private addresses).
- [ ] `--workers 2` is fine: activation is row-locked/idempotent, and both billing scheduler jobs (maintenance, reconcile) take a Postgres advisory lock so one instance runs each tick.
- [ ] The old `POST /payments/webhook` receiver has been **removed** (order payments are customer→seller direct; nothing used it). Make sure no Razorpay webhook in the dashboard still points at `/payments/webhook`.

---

## 3. Smoke the plumbing before touching Razorpay

```bash
# 1) the route exists at the URL you will give Razorpay — expect 400 "Invalid signature." (NOT 404)
curl -i -X POST https://<backend-domain>/billing/webhook -H 'Content-Type: application/json' -d '{}'

# 2) the /api-prefixed URL is NOT served — expect 404 (confirms why §0.1 matters)
curl -i -X POST https://<backend-domain>/api/billing/webhook -d '{}'

# 3) plans are served (sign in on the dashboard, copy the JWT)
curl -s https://<backend-domain>/billing/plans -H "Authorization: Bearer <jwt>" | jq '.plans[] | {code, base_paise, total_paise}'
#    Starter 459900 / 542682, Growth 1199900 / 1415882, Pro 1799900 / 2123882

# 4) billing health (admin key) — expect status "ok" ~15 minutes after boot (both jobs must have run once), mode "test"/"live"
curl -s https://<backend-domain>/health/billing -H "X-Admin-Key: $ADMIN_SECRET_KEY" | jq
```

---

## 4. Test-mode run (real Razorpay, fake money)

Use a **throwaway tenant**. Keys = `rzp_test_…`, `RAZORPAY_MODE=test`, test webhook configured. Everything you create is stamped `mode='test'` and numbered `TEST-…`.

### 4.1 Happy path — card
1. Dashboard → Billing → choose **Starter** → Pay. The real Razorpay Checkout.js window opens. If you instead see the in-app mock "TEST MODE" payment panel, the keys are not being picked up and you are still on the mock gateway — stop and fix §2.1.
2. Card **`4111 1111 1111 1111`**, any future expiry, any 3-digit CVV, any name → on the bank page choose **Success**.
3. Expect, in order: Checkout closes → "plan active" → Billing page shows Starter, 0 / 1,500, 30 days left.
4. Razorpay dashboard → Webhooks → your endpoint → **delivery log**: `payment.captured` and `order.paid`, both **200**.
5. Check: `GET /admin/billing/payment-events?page_size=10` (header `X-Admin-Key`) → `payment.captured processed=true error=null`. Exactly **one** `client_subscriptions` row and **one** invoice numbered `TEST-ST24/<FY>/0001`; the invoice PDF downloads.

### 4.2 Happy path — UPI
Pay with UPI ID **`success@razorpay`** → success. Same expectations as 4.1.

### 4.3 Failure path
1. New checkout → card `4111 1111 1111 1111` → bank page **Failure** (or UPI `failure@razorpay`).
2. Expect: friendly "payment failed" in the UI, **no** subscription. Webhook log shows `payment.failed` 200. `GET /billing/payments` → order `failed` with Razorpay's reason.
3. Retry **in the same Checkout window** with a success → plan activates on that order.

### 4.4 Webhook-only activation (browser closed)
Start a payment, **close the tab right after paying**. Within a minute the webhook activates the plan; the dashboard picks it up on next load.

### 4.5 Signature tampering (against the deployed URL)
```bash
body='{"event":"payment.captured","payload":{"payment":{"entity":{"id":"pay_x","order_id":"order_x","amount":100}}}}'
curl -i -X POST https://<backend-domain>/billing/webhook -H 'Content-Type: application/json' \
     -H 'X-Razorpay-Signature: deadbeef' -H 'X-Razorpay-Event-Id: evt_manual_1' -d "$body"      # → 400
```
Nothing must appear in `payment_events`. Repeat it 6+ times within 10 minutes → exactly **one** email to `BILLING_ALERT_EMAIL` ("N webhook signature failures…") — this proves the operator email path works end to end.

### 4.6 Upgrade + credit
Starter active → **Upgrade to Growth**. Checkout amount must equal `(₹11,999 − credit) + 18% GST`; after payment: Growth active, **used count carried over**, 30 fresh days, Starter row `superseded`.

### 4.7 Refunds (the part that changed)
1. **Full refund:** Razorpay dashboard → Transactions → Payments → the test payment → **Refund** (full).
2. Expect: `refund.processed` 200; order `refunded`; **subscription `revoked`** (dashboard shows no plan + a red grace banner counting 3 days from now); a `credit_notes` row `TEST-ST24-CN/<FY>/0001` (kind `full`, amounts equal to the order); the tenant sees a "Plan cancelled after refund" alert and gets the email; backend log `billing: FULL REFUND of order …`.
3. Re-send that event from the Razorpay delivery log (**Resend**) → `{"status":"duplicate"}`, still one credit note, no second email.
4. **Partial refund** (another test payment, refund e.g. ₹100): order stays `paid`, subscription untouched, a `partial` credit note exists, **no** tenant email/alert, and an internal flag exists: `SELECT * FROM billing_alerts WHERE audience='admin'`.
5. Restore a refunded tenant if you want them back: `POST /admin/billing/clients/{id}/grant` (§7).

### 4.8 Duplicate delivery
Razorpay dashboard → Webhooks → delivery log → **Resend** a delivered `payment.captured`. Response `{"status":"duplicate"}`; no second subscription.

### 4.9 Reconcile (stuck-order recovery)
1. Start a checkout, pay it, but make the callback and webhook both fail: temporarily set a wrong `RAZORPAY_WEBHOOK_SECRET` (webhook → 400) and close the tab before the success callback.
2. `GET /health/billing?verify=true` → `stuck_orders: 1` after 30 minutes and `stuck_paid_at_razorpay: 1`, `status: degraded`.
3. Wait for the 15-minute `billing_reconcile` tick (or restart the service to run it sooner). Expect: order `paid`, subscription active, invoice issued, customer payment email, **and an operator email "Reconcile activated payment order N"**. Restore the correct secret afterwards.
4. An abandoned checkout older than 24 h becomes `failed` with `failure_reason = 'expired: no payment within 24 hours of checkout'` (a later payment on it would still activate).

### 4.10 Exit criteria for test mode
- [ ] 4.1–4.9 all behave as described
- [ ] `GET /health/billing` → `status: ok`, `webhook_errors_actionable_24h: 0`, `amount_mismatches_24h: 0`, `stuck_orders: 0`
- [ ] `SELECT count(*) FROM payment_orders WHERE status='paid' AND mode='test' AND id NOT IN (SELECT payment_order_id FROM invoices)` = 0 (apart from ₹0 admin grants)
- [ ] No `ERROR billing:` lines in the Railway logs other than the ones you provoked

---

## 5. Switch to live

Do this in a quiet hour. Have the Razorpay dashboard and Railway logs open.

1. [ ] §1.1 complete: account **activated**, live keys generated.
2. [ ] Record the cut-over time: `SELECT now();` (`GO_LIVE_AT`). Everything before it is test data — and is already marked `mode='test'`, so reports can filter on `mode='live'`.
3. [ ] **Test tenants:** deactivate throwaway tenants (`clients.is_active=false`) so they don't pollute reports. They keep their test subscriptions (see "Known gaps"). Do **not** delete `invoices`/`credit_notes` rows or touch counters; the live series is already independent (starts at `ST24/<FY>/0001`) and `billing_plans` must never be touched.
4. [ ] In **Live mode**, create the webhook (§1.3) with the live URL, secret and the four events.
5. [ ] Railway: apply the live delta in §2.1 (live keys, `RAZORPAY_MODE=live`, `RAZORPAY_WEBHOOK_SECRET`, `SELLER_*`, `BILLING_ALERT_EMAIL`, `EMAIL_PROVIDER=smtp`), **unset `SELLERTALK24_BILLING_MOCK`**, keep `SELLERTALK24_BILLING_ENFORCE=false`. Redeploy.
6. [ ] Startup log: `Razorpay    : configured`; no `MOCK mode` warning; no `RAZORPAY_MODE=live but … other mode's prefix` error; `Migrations: database is at head`.
7. [ ] `GET /health/billing` → `"mode": "live", "mock": false` (wait 15 min for `status: ok`).
8. [ ] **Live ₹ test:** buy **Starter for real** (₹5,426.82). Verify as 4.1: order `mode='live'`, invoice **`ST24/<FY>/0001`** (no `TEST-` prefix) with your real GSTIN/address, emails received, webhook 200s. Then **refund it in full** in the Razorpay dashboard: check §4.7 expectations — credit note **`ST24-CN/<FY>/0001`**, subscription revoked (this one is real money — the refund returns it).
9. [ ] Watch `/health/billing` and the logs (§6) for the first hour and again after 24 h.
10. [ ] **Enforcement (separate decision, later):** before setting `SELLERTALK24_BILLING_ENFORCE=true`, make sure every paying/legacy client has a plan or `PUT /admin/billing/clients/{id}/billing-exempt`. `SELECT id, business_name FROM clients c WHERE billing_exempt = false AND NOT EXISTS (SELECT 1 FROM client_subscriptions s WHERE s.client_id=c.id AND s.status IN ('active','pending'))` — everyone listed would go quiet after grace.
11. [ ] Announce / open the pricing page CTA.

### Rollback

| Situation | Action |
|---|---|
| Customers hit "assistant unavailable" unexpectedly | `SELLERTALK24_BILLING_ENFORCE=false` + redeploy. Instant, no data change. |
| Live checkout is broken (e.g. wrong key) but nobody has paid | Set test keys + `RAZORPAY_MODE=test` + the **test** webhook secret, redeploy. Nothing real is lost. Live-mode orders created meanwhile will be refused (`mode_mismatch`) until you return to live keys. |
| Customers **have** paid live and something is wrong with activation | **Do not swap keys** (live webhooks would then fail the signature check, and live orders would be refused as the wrong mode). Fix forward: reconcile activates paid-at-Razorpay orders within 15 minutes; for a specific stuck payment use `POST /admin/billing/payment-events/{id}/reprocess`; if no event exists, `POST /admin/billing/clients/{id}/grant` and refund/ignore as appropriate. |
| A customer must be cut off or credited manually | `POST /admin/billing/subscriptions/{id}/revoke` or `…/extend` (§7). |
| Need billing fully off for a while | Remove the webhook's active events in the Razorpay dashboard, leave `ENFORCE=false`. Existing subscriptions keep working; the maintenance job keeps expiring/alerting. |
| Bad deploy of billing code | Redeploy the previous image. Migrations 0062–0065 are additive; **do not downgrade the DB with paying customers** (0065's downgrade refuses while any `mode='live'` order exists). |

---

## 6. Monitoring

### 6.1 One endpoint: `GET /health/billing` (header `X-Admin-Key`)
Public `/health` (Railway's check) is unchanged. This one is the operator view; poll it from an uptime monitor that can match a JSON field (alert when `status != "ok"`).

| Field | Meaning |
|---|---|
| `status` / `problems[]` | `ok` or `degraded` with plain-English reasons |
| `mode`, `mock` | what this process charges in (`live`/`test`); `mock:true` in production is a misconfiguration |
| `last_webhook_received_at` | newest `payment_events` row — silence for days while customers pay = webhook broken |
| `webhook_errors_24h` / `webhook_errors_actionable_24h` | events with `error` set; "actionable" excludes known-benign notes (stale `payment.failed`, not our order, …) |
| `amount_mismatches_24h` | Razorpay reported a different amount than we charged — investigate immediately |
| `bad_signatures_10m` | rejected webhook deliveries; > 5 also emails `BILLING_ALERT_EMAIL` |
| `stuck_orders` | orders still `created`/`attempted` after 30 min (last 3 days) |
| `stuck_paid_at_razorpay` | with `?verify=true`: of those, how many Razorpay says are **PAID** (read-only; reconcile will activate them) |
| `reconcile`, `maintenance` | last run, status, counters; `overdue:true` if no run for 45 min (should be every 15) |

### 6.2 Operator emails (`BILLING_ALERT_EMAIL`, sent via `EMAIL_PROVIDER`; once per incident)
- **Amount mismatch** on a payment order (also logs `ERROR billing: AMOUNT MISMATCH`)
- **Webhook signature burst** (> 5 in 10 minutes, at most one email per 10-minute window)
- **Reconcile activated an order** (a callback path is broken — check webhook delivery in Razorpay)

With `EMAIL_PROVIDER=console` or an empty address these are only logged as `ERROR BILLING ADMIN ALERT [kind] …`; search the Railway logs for that string.

### 6.3 Webhook health — `payment_events`
```
GET /admin/billing/payment-events?has_error=true        (X-Admin-Key)   → must be empty of real faults
GET /admin/billing/payment-events?processed=false       (X-Admin-Key)   → must be empty
POST /admin/billing/payment-events/{id}/reprocess       re-runs a failed/unfinished event (idempotent)
```
Expected, *not* faults: `payment.failed for an already paid order`, `order_not_found: … (not a SellerTalk24 order)`, `refund for an order that was never activated…`.
Real faults: `amount_mismatch`, `payment_order_mismatch`, `mode_mismatch`, `gateway_error`, `unhandled …`.

### 6.4 Money-integrity SQL (run daily for the first fortnight)
```sql
-- paid order with no subscription (activation half-failed)
SELECT o.id, o.client_id, o.razorpay_order_id FROM payment_orders o
 WHERE o.status='paid' AND NOT EXISTS (SELECT 1 FROM client_subscriptions s WHERE s.source_payment_id=o.id);

-- paid, non-₹0 order with no invoice (self-heals; or POST /admin/billing/invoices/backfill)
SELECT o.id, o.client_id FROM payment_orders o
 WHERE o.status='paid' AND o.amount_paise > 0 AND NOT EXISTS (SELECT 1 FROM invoices i WHERE i.payment_order_id=o.id);

-- refunded (fully) but the customer still has a running plan  → should never return rows now
SELECT o.id, o.client_id, s.id AS sub_id, s.status FROM payment_orders o
  JOIN client_subscriptions s ON s.source_payment_id=o.id
 WHERE o.status='refunded' AND s.status IN ('active','pending');

-- partial refunds waiting for a human decision
SELECT client_id, title, message, created_at FROM billing_alerts WHERE audience='admin' AND read_at IS NULL ORDER BY id DESC;

-- refund total vs credit notes (should agree per order)
SELECT payment_order_id, sum(total_paise) FROM credit_notes WHERE mode='live' GROUP BY 1;

-- live revenue reconciliation (compare with Razorpay → Payments → captured)
SELECT count(*), sum(amount_paise) FROM payment_orders WHERE mode='live' AND status IN ('paid','refunded') AND amount_paise > 0;

-- who did what by hand
SELECT created_at, actor, action, client_id, reason FROM billing_admin_log ORDER BY id DESC LIMIT 50;
```

### 6.5 Log lines worth an alert (Railway log search)
| Search for | Means |
|---|---|
| `MIGRATION MISMATCH` | the DB is not at the code's migration head |
| `BILLING ADMIN ALERT` | an operator alert was raised (see §6.2) |
| `AMOUNT MISMATCH` | paid amount ≠ order amount |
| `FULL REFUND of order` / `PARTIAL REFUND of` | a refund was processed (full → plan revoked) |
| `billing: order … was created in test mode but this process runs in live mode` | a test order tried to activate in live (or vice versa) — refused |
| `billing reconcile: ACTIVATED order` | reconcile rescued a paid order |
| `billing webhook: invalid signature rejected` | wrong secret, or probing. A burst right after a deploy = wrong `RAZORPAY_WEBHOOK_SECRET` |
| `RAZORPAY_WEBHOOK_SECRET is not set` | all webhooks are being rejected |
| `Razorpay create_order failed` | keys wrong / Razorpay outage — checkout returns 502 |
| `billing: invoice creation failed` | seller config problem; customer still activated; fix then `POST /admin/billing/invoices/backfill` |
| `billing maintenance:` / `billing reconcile:` with `errors=` > 0 | a job hit per-client / per-order errors |
| `billing: conversation counting failed` / `entitlement check failed — failing OPEN` | counting / gating bug (bot unaffected) |
| `billing ADMIN …` | a manual admin action (also in `billing_admin_log`) |

### 6.6 Razorpay-side
- Dashboard → Webhooks → delivery log: any non-200 streak; Razorpay disables a webhook after prolonged failure.
- Dashboard → Payments vs the live-revenue SQL above — reconcile weekly; Settlements for payout timing.
- Dashboard → Disputes: chargebacks are **not** handled by the app (revoke by hand if you lose one, §7).

---

## 7. Admin endpoints

All need `X-Admin-Key: $ADMIN_SECRET_KEY`. Identify yourself with the optional `X-Admin-User: you@sellertalk24.com` header — it is stored as `actor` in `billing_admin_log` (default `admin-key`). **Every mutating call needs a `reason`** (min 3 chars; the old field name `note` is still accepted) and writes an audit row (`actor`, `reason`, client, subscription, detail). Failed calls write nothing.

| Endpoint | Body | Effect |
|---|---|---|
| `POST /admin/billing/clients/{client_id}/grant` | `{plan_code, days?, reason, amount_paise?}` | Offline payment: stacks a period of the plan (starts now if nothing is running). Creates a **paid ₹0 payment order** (`offline_…`) — or, when `amount_paise` (GST-inclusive, what you actually received, ≥ 100) is given, an order for that amount **and a tax invoice** in the current mode's series. `POST /admin/billing/subscriptions/grant` is the same with `client_id` in the body |
| `POST /admin/billing/subscriptions/{id}/extend` | `{days, reason}` | Pushes an active/pending period's end out; periods queued behind it shift too |
| `POST /admin/billing/subscriptions/{id}/revoke` | `{reason}` | Cuts an active/pending period short **now** (`status='revoked'`); the tenant gets the normal grace counted from this moment. 409 if the period is not active/pending |
| `PUT /admin/billing/clients/{id}/billing-exempt` | `{exempt, reason}` | Sets/clears `billing_exempt` |
| `GET /admin/billing/subscriptions` | `?client_id=&status=` | All periods across tenants |
| `GET /admin/billing/payment-events`, `POST …/{id}/reprocess` | | §6.3 |
| `POST /admin/billing/invoices/backfill` | | Issues invoices for paid orders that lack one |
| `GET /health/billing` | `?verify=true` | §6.1 |

---

## Known gaps

1. **Real Razorpay never exercised** — §4 is the only protection against SDK / payload-shape surprises (notably `order.payments`, used by the reconcile job, and the `refund.processed` payload).
2. **Test-mode subscriptions survive go-live.** Orders, invoices and credit notes are separated by `mode`; `client_subscriptions` has no `mode`, so a throwaway tenant's test subscription still grants service in live until it expires (deactivate those tenants — §5 step 3).
3. **No tenant-facing credit-note page or PDF.** Credit notes exist as database rows (`credit_notes`) and are mentioned in the refund email; there is no download endpoint or PDF yet.
4. A full refund revokes only the subscription that the refunded order bought. If that period had a **paid, queued renewal behind it**, the queued period is left pending (it starts when its start date arrives) — review such cases by hand. Chargebacks/disputes are not handled at all.
5. Billing emails go out via `EMAIL_PROVIDER`; the default is `console` (nothing is sent) — set SMTP before live, or customers and the operator get nothing. A failed send is not retried.
6. `SELLER_GSTIN` / `SELLER_ADDRESS` default to empty → invoices print "—" (only a log warning).
7. The legacy `POST /payments/qr` and `GET /payments/{id}/invoice` routes remain, but with the webhook receiver gone a QR created there can never be marked paid — they are orphaned (nothing in the app calls them). `backend/tests/payment_simulator.py` and `full_flow_test.py` still try to call the removed `/payments/webhook`. Both are candidates for deletion once you've confirmed no Razorpay QR flow is wanted.
8. Instagram gating still follows the legacy `plans.channels` table, not `billing_plans.features.instagram`.
