import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { fetchSubscription } from "../api/billing";
import type { SubscriptionState } from "../types/billing";
import { useAuth } from "./AuthContext";

interface BillingContextValue {
  /** Latest subscription state; null until the first load completes (or if it failed). */
  state: SubscriptionState | null;
  loading: boolean;
  error: boolean;
  /** Re-fetch GET /billing/subscription. Resolves to the new state, or null on failure. */
  refresh: () => Promise<SubscriptionState | null>;
  /** Replace the state with one we already hold (the /verify and /mock/complete responses). */
  applyState: (next: SubscriptionState) => void;
}

const BillingContext = createContext<BillingContextValue | null>(null);

/** Minimum gap between focus-triggered refreshes. */
const FOCUS_REFRESH_MS = 60_000;

/** Shared subscription state for the billing page, the global banners and the checkout flow. */
export function BillingProvider({ children }: { children: React.ReactNode }) {
  const { client } = useAuth();
  const clientId = client?.id ?? null;
  const [state, setState] = useState<SubscriptionState | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const lastLoadedAt = useRef(0);
  const requestSeq = useRef(0);

  const refresh = useCallback(async (): Promise<SubscriptionState | null> => {
    const seq = ++requestSeq.current;
    setLoading(true);
    try {
      const next = await fetchSubscription();
      if (seq === requestSeq.current) {
        setState(next);
        setError(false);
        lastLoadedAt.current = Date.now();
      }
      return next;
    } catch {
      if (seq === requestSeq.current) setError(true);
      return null;
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, []);

  const applyState = useCallback((next: SubscriptionState) => {
    requestSeq.current++; // a slower in-flight fetch must not overwrite this newer state
    setState(next);
    setError(false);
    setLoading(false);
    lastLoadedAt.current = Date.now();
  }, []);

  // Load when a user signs in; clear when they sign out so the next user never sees stale banners.
  useEffect(() => {
    if (clientId === null) {
      requestSeq.current++;
      setState(null);
      setError(false);
      setLoading(false);
      return;
    }
    void refresh();
  }, [clientId, refresh]);

  // Usage and expiry move while the tab sits open — refresh when the user comes back to it.
  useEffect(() => {
    if (clientId === null) return;
    function onFocus() {
      if (Date.now() - lastLoadedAt.current >= FOCUS_REFRESH_MS) void refresh();
    }
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [clientId, refresh]);

  const value = useMemo(
    () => ({ state, loading, error, refresh, applyState }),
    [state, loading, error, refresh, applyState],
  );
  return <BillingContext.Provider value={value}>{children}</BillingContext.Provider>;
}

export function useBilling(): BillingContextValue {
  const ctx = useContext(BillingContext);
  if (!ctx) throw new Error("useBilling must be used within BillingProvider");
  return ctx;
}
