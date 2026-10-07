import { useCallback, useEffect, useRef, useState } from "react";
import { createCheckout, fetchSubscription, mockComplete, parseApiError, verifyPayment } from "../api/billing";
import { useBilling } from "../context/BillingContext";
import type {
  CheckoutResponse,
  PaymentPurpose,
  SubscriptionState,
  VerifyPayload,
} from "../types/billing";
import { subscriptionFingerprint } from "../utils/billing";
import { clearPendingPlan } from "../utils/pendingPlan";

const CHECKOUT_SRC = "https://checkout.razorpay.com/v1/checkout.js";
const THEME_COLOR = "#25D366";
/** After a network failure on /verify the webhook may still activate the plan: poll this long, this often. */
const POLL_WINDOW_MS = 30_000;
const POLL_INTERVAL_MS = 3_000;

export type CheckoutPhase =
  | "idle"
  | "creating"
  | "paying"
  | "mock"
  | "verifying"
  | "confirming"
  | "success"
  | "error";

export type CheckoutErrorKind =
  | "downgrade"
  | "rate_limited"
  | "forbidden"
  | "unavailable"
  | "gateway"
  | "network"
  | "script"
  | "cancelled"
  | "payment_failed"
  | "verify_failed"
  | "verify_timeout"
  | "generic";

export interface CheckoutError {
  kind: CheckoutErrorKind;
  /** Extra, already-human text from Razorpay (why the payment failed). */
  detail?: string;
}

export interface CheckoutSuccess {
  planCode: string;
  purpose: PaymentPurpose;
  state: SubscriptionState;
}

/** Errors after which paying again could double-charge: the money may already have been taken. */
export const NO_RETRY_KINDS: readonly CheckoutErrorKind[] = ["verify_failed", "verify_timeout"];

let scriptPromise: Promise<void> | null = null;

/** Injects checkout.js once; a failed load is forgotten so the next attempt can retry. */
function loadCheckoutScript(): Promise<void> {
  if (window.Razorpay) return Promise.resolve();
  if (scriptPromise) return scriptPromise;
  scriptPromise = new Promise<void>((resolve, reject) => {
    const el = document.createElement("script");
    el.src = CHECKOUT_SRC;
    el.async = true;
    const fail = () => {
      scriptPromise = null;
      el.remove();
      reject(new Error("checkout.js failed to load"));
    };
    el.onload = () => (window.Razorpay ? resolve() : fail());
    el.onerror = fail;
    document.head.appendChild(el);
  });
  return scriptPromise;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

/** Maps a failure of POST /billing/checkout to a user-facing kind. */
function checkoutErrorFrom(err: unknown): CheckoutError {
  const p = parseApiError(err);
  if (p.isNetwork) return { kind: "network" };
  if (p.code === "downgrade_not_allowed") return { kind: "downgrade" };
  if (p.code === "rate_limited" || p.status === 429) return { kind: "rate_limited" };
  if (p.code === "gateway_error" || p.status === 502) return { kind: "gateway" };
  if (p.status === 403) return { kind: "forbidden" };
  if (p.status === 503) return { kind: "unavailable" };
  return { kind: "generic" };
}

/**
 * Drives the whole purchase: create the order → open Razorpay Checkout.js (or the TEST MODE dialog when the
 * backend runs the mock gateway) → verify → refresh the subscription.
 *
 * Safety properties: a synchronous in-flight guard stops double orders; after a /verify network or 5xx failure we
 * poll /subscription for 30 s (the webhook may activate the plan) before showing an error; and errors that could
 * mean "already paid" never offer a retry.
 */
export function useRazorpayCheckout(options: { onSuccess?: (state: SubscriptionState) => void } = {}) {
  const billing = useBilling();
  const [phase, setPhase] = useState<CheckoutPhase>("idle");
  const [planCode, setPlanCode] = useState<string | null>(null);
  const [error, setError] = useState<CheckoutError | null>(null);
  const [success, setSuccess] = useState<CheckoutSuccess | null>(null);
  const [mockOrder, setMockOrder] = useState<CheckoutResponse | null>(null);
  const [mockBusy, setMockBusy] = useState(false);

  const inFlight = useRef(false);
  const mounted = useRef(true);
  const baseline = useRef("none");
  const latestState = useRef(billing.state);
  const onSuccessRef = useRef(options.onSuccess);
  const { applyState, refresh } = billing;

  useEffect(() => {
    latestState.current = billing.state;
    onSuccessRef.current = options.onSuccess;
  });
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const fail = useCallback((err: CheckoutError) => {
    inFlight.current = false;
    if (!mounted.current) return;
    setMockOrder(null);
    setError(err);
    setPhase("error");
  }, []);

  const succeed = useCallback(
    (state: SubscriptionState, order: CheckoutResponse) => {
      inFlight.current = false;
      clearPendingPlan(); // the plan picked on the marketing site has now been dealt with
      applyState(state);
      if (!mounted.current) return;
      setMockOrder(null);
      setSuccess({ planCode: order.plan_code, purpose: order.purpose, state });
      setPhase("success");
      onSuccessRef.current?.(state);
    },
    [applyState],
  );

  /** True once /subscription shows the purchased plan (changed from before checkout). */
  const activated = useCallback((state: SubscriptionState, order: CheckoutResponse) => {
    if (subscriptionFingerprint(state) === baseline.current) return false;
    return (
      state.subscription?.plan.code === order.plan_code ||
      state.queued.some((q) => q.plan_code === order.plan_code)
    );
  }, []);

  const pollForActivation = useCallback(
    async (order: CheckoutResponse) => {
      setPhase("confirming");
      const deadline = Date.now() + POLL_WINDOW_MS;
      while (Date.now() < deadline) {
        await sleep(POLL_INTERVAL_MS);
        if (!mounted.current) return;
        try {
          const state = await fetchSubscription();
          if (activated(state, order)) {
            succeed(state, order);
            return;
          }
        } catch {
          // still offline / server hiccup — keep polling until the window closes
        }
      }
      fail({ kind: "verify_timeout" });
    },
    [activated, fail, succeed],
  );

  const verify = useCallback(
    async (order: CheckoutResponse, payload: VerifyPayload) => {
      setPhase("verifying");
      try {
        succeed(await verifyPayment(payload), order);
      } catch (err) {
        const p = parseApiError(err);
        if (p.isNetwork || (p.status !== null && p.status >= 500)) {
          await pollForActivation(order);
        } else {
          fail({ kind: "verify_failed" });
        }
      }
    },
    [fail, pollForActivation, succeed],
  );

  const openRazorpay = useCallback(
    (order: CheckoutResponse) => {
      if (!window.Razorpay) {
        fail({ kind: "script" });
        return;
      }
      let handled = false;
      let lastFailure: string | undefined;
      try {
        const rzp = new window.Razorpay({
          key: order.key_id,
          order_id: order.razorpay_order_id,
          amount: order.amount,
          currency: order.currency,
          name: order.name,
          description: order.description,
          prefill: order.prefill,
          theme: { color: THEME_COLOR },
          handler: (response) => {
            handled = true;
            void verify(order, response);
          },
          modal: {
            // Fires when the customer closes the window without paying (not after a successful payment).
            ondismiss: () => {
              if (handled) return;
              fail(lastFailure ? { kind: "payment_failed", detail: lastFailure } : { kind: "cancelled" });
            },
          },
        });
        // Razorpay keeps its own retry UI open after a failed attempt, so only remember the reason here and
        // report it if the customer then closes the window.
        rzp.on("payment.failed", (resp) => {
          lastFailure = resp.error.description ?? resp.error.reason;
        });
        setPhase("paying");
        rzp.open();
      } catch {
        fail({ kind: "generic" });
      }
    },
    [fail, verify],
  );

  const start = useCallback(
    async (code: string) => {
      if (inFlight.current) return;
      inFlight.current = true;
      setError(null);
      setSuccess(null);
      setPlanCode(code);
      setPhase("creating");
      baseline.current = subscriptionFingerprint(latestState.current);

      let order: CheckoutResponse;
      try {
        order = await createCheckout(code);
      } catch (err) {
        fail(checkoutErrorFrom(err));
        return;
      }
      if (!mounted.current) return;

      if (order.mock) {
        setMockOrder(order);
        setPhase("mock");
        return;
      }
      try {
        await loadCheckoutScript();
      } catch {
        fail({ kind: "script" });
        return;
      }
      if (!mounted.current) return;
      openRazorpay(order);
    },
    [fail, openRazorpay],
  );

  const mockPay = useCallback(async () => {
    if (!mockOrder || mockBusy) return;
    setMockBusy(true);
    try {
      succeed(await mockComplete(mockOrder.razorpay_order_id), mockOrder);
    } catch {
      fail({ kind: "verify_failed" });
    } finally {
      if (mounted.current) setMockBusy(false);
    }
  }, [fail, mockBusy, mockOrder, succeed]);

  /** The TEST MODE "Fail" button. The mock gateway can only simulate success, so a failure is local: the order
   *  simply stays unpaid, exactly like a customer abandoning a real payment. */
  const mockFail = useCallback(() => fail({ kind: "payment_failed" }), [fail]);
  const mockCancel = useCallback(() => fail({ kind: "cancelled" }), [fail]);

  const dismissError = useCallback(() => {
    setError(null);
    setPhase("idle");
  }, []);

  const dismissSuccess = useCallback(() => {
    setSuccess(null);
    setPhase("idle");
  }, []);

  /** Re-run the last attempt (never offered for errors that may mean the money was already taken). */
  const retry = useCallback(() => {
    if (planCode) void start(planCode);
  }, [planCode, start]);

  /** For the "might already be paid" errors: close the dialog and re-read the truth from the server. */
  const checkStatus = useCallback(async () => {
    dismissError();
    await refresh();
  }, [dismissError, refresh]);

  const busy = phase === "creating" || phase === "paying" || phase === "mock" || phase === "verifying" || phase === "confirming";

  return {
    phase,
    busy,
    /** Plan code of the attempt in progress / last attempted (for the per-card spinner). */
    planCode,
    error,
    success,
    mockOrder,
    mockBusy,
    start,
    retry,
    checkStatus,
    dismissError,
    dismissSuccess,
    mockPay,
    mockFail,
    mockCancel,
  };
}

export type RazorpayCheckout = ReturnType<typeof useRazorpayCheckout>;
