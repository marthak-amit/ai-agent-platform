import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { api } from "../api/client";
import { fetchPaymentSettings, fetchPendingCount } from "../api/chat";
import { useAuth } from "./AuthContext";
import type { RealtimeEvent } from "../types/chat";
import {
  loadNotifyPrefs,
  playChime,
  saveNotifyPrefs,
  showBrowserNotification,
  type NotifyPrefs,
} from "../utils/notify";

type Handler = (event: RealtimeEvent) => void;
export type Connection = "connecting" | "live" | "polling";

interface RealtimeApi {
  /** Subscribe to live events; returns an unsubscribe function. */
  subscribe: (handler: Handler) => () => void;
  connection: Connection;
  /** Orders waiting for the seller to verify (sidebar badge / dashboard widget). */
  pendingCount: number;
  refreshPendingCount: () => void;
  /** A customer reached checkout while no UPI ID is configured. */
  setupAlert: boolean;
  clearSetupAlert: () => void;
  notifyPrefs: NotifyPrefs;
  setNotifyPrefs: (prefs: NotifyPrefs) => void;
}

const RealtimeContext = createContext<RealtimeApi | null>(null);

const BASE_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? "/api";
const EVENT_TYPES: RealtimeEvent["type"][] = [
  "new_message",
  "payment_submitted",
  "payment_reviewed",
  "conversation_updated",
  "payment_setup_required",
];
const POLL_MS = 5_000;
const SSE_RETRY_MS = 60_000;
const MAX_SSE_ERRORS = 3;
const STABLE_MS = 10_000;

/**
 * One shared realtime connection for the whole dashboard.
 *
 * Primary transport is SSE (`/events/stream`); after repeated failures (proxy
 * buffering, corporate firewalls) it degrades to polling `/events/poll` every
 * 5 s and retries SSE every minute, so the UI keeps updating either way.
 */
export function RealtimeProvider({ children }: { children: React.ReactNode }) {
  const { token, client } = useAuth();
  const { t } = useTranslation();
  const navigate = useNavigate();
  const handlers = useRef(new Set<Handler>());
  const [connection, setConnection] = useState<Connection>("connecting");
  const [pendingCount, setPendingCount] = useState(0);
  const [setupAlert, setSetupAlert] = useState(false);
  const [notifyPrefs, setNotifyPrefsState] = useState<NotifyPrefs>(loadNotifyPrefs);
  const prefsRef = useRef(notifyPrefs);
  prefsRef.current = notifyPrefs;

  const canVerify = !!client && (client.current_user.is_owner || client.current_user.permissions.includes("payment_verify"));

  const refreshPendingCount = useCallback(() => {
    if (!canVerify) return;
    fetchPendingCount().then(setPendingCount).catch(() => undefined);
  }, [canVerify]);

  const dispatch = useCallback(
    (event: RealtimeEvent) => {
      if (event.type === "payment_submitted" || event.type === "payment_reviewed") refreshPendingCount();
      if (event.type === "payment_setup_required") setSetupAlert(true);
      if (event.type === "payment_submitted" && !event.data.additional) {
        if (prefsRef.current.sound) playChime();
        if (prefsRef.current.browser && (document.hidden || !document.hasFocus())) {
          showBrowserNotification(t("payments.notify_title"), t("payments.notify_body"), () => navigate("/payments"));
        }
      }
      handlers.current.forEach((h) => {
        try {
          h(event);
        } catch (err) {
          console.error("realtime handler failed", err);
        }
      });
    },
    [refreshPendingCount, t, navigate],
  );
  const dispatchRef = useRef(dispatch);
  dispatchRef.current = dispatch;

  // Connection lifecycle (SSE → polling fallback).
  useEffect(() => {
    if (!token) return;
    let closed = false;
    let es: EventSource | null = null;
    let pollTimer: number | undefined;
    let retryTimer: number | undefined;
    let errors = 0;
    let openedAt = 0;
    let since = new Date().toISOString();

    const stopPolling = () => {
      if (pollTimer) window.clearInterval(pollTimer);
      pollTimer = undefined;
    };

    const poll = async () => {
      try {
        const { data } = await api.get<{ server_time: string; events: { type: string; data: unknown }[] }>(
          "/events/poll",
          { params: { since } },
        );
        since = data.server_time;
        data.events.forEach((e) => dispatchRef.current(e as RealtimeEvent));
      } catch {
        /* transient — next tick retries */
      }
    };

    const startPolling = () => {
      if (closed || pollTimer) return;
      setConnection("polling");
      void poll();
      pollTimer = window.setInterval(poll, POLL_MS);
      retryTimer = window.setTimeout(connectSse, SSE_RETRY_MS);
    };

    function connectSse() {
      if (closed) return;
      stopPolling();
      setConnection("connecting");
      es = new EventSource(`${BASE_URL}/events/stream?token=${encodeURIComponent(token as string)}`);
      es.onopen = () => {
        openedAt = Date.now();
        setConnection("live");
        // catch up on anything missed while disconnected/polling
        void poll();
      };
      EVENT_TYPES.forEach((type) =>
        es?.addEventListener(type, (e) => {
          try {
            dispatchRef.current({ type, data: JSON.parse((e as MessageEvent).data) } as RealtimeEvent);
          } catch {
            /* malformed frame */
          }
        }),
      );
      es.onerror = () => {
        // A stream that opened but dropped within STABLE_MS is flapping (proxy
        // buffering/killing it) — count it as a failure so we fall back to polling
        // instead of reconnecting forever. Only a long-lived stream resets the count.
        errors = openedAt && Date.now() - openedAt >= STABLE_MS ? 1 : errors + 1;
        openedAt = 0;
        if (errors >= MAX_SSE_ERRORS) {
          es?.close();
          es = null;
          startPolling();
        }
      };
    }

    connectSse();
    return () => {
      closed = true;
      es?.close();
      stopPolling();
      if (retryTimer) window.clearTimeout(retryTimer);
    };
  }, [token]);

  // Initial badge + setup-alert state, plus a slow safety refresh.
  useEffect(() => {
    if (!token || !client) return;
    refreshPendingCount();
    fetchPaymentSettings().then((s) => setSetupAlert(s.setup_alert_active)).catch(() => undefined);
    const id = window.setInterval(refreshPendingCount, 60_000);
    return () => window.clearInterval(id);
  }, [token, client?.id, refreshPendingCount]);

  const subscribe = useCallback((handler: Handler) => {
    handlers.current.add(handler);
    return () => {
      handlers.current.delete(handler);
    };
  }, []);

  const value = useMemo<RealtimeApi>(
    () => ({
      subscribe,
      connection,
      pendingCount,
      refreshPendingCount,
      setupAlert,
      clearSetupAlert: () => setSetupAlert(false),
      notifyPrefs,
      setNotifyPrefs: (prefs) => {
        saveNotifyPrefs(prefs);
        setNotifyPrefsState(prefs);
      },
    }),
    [subscribe, connection, pendingCount, refreshPendingCount, setupAlert, notifyPrefs],
  );

  return <RealtimeContext.Provider value={value}>{children}</RealtimeContext.Provider>;
}

export function useRealtime(): RealtimeApi {
  const ctx = useContext(RealtimeContext);
  if (!ctx) throw new Error("useRealtime must be used within RealtimeProvider");
  return ctx;
}

/** Subscribe to realtime events for the lifetime of a component. */
export function useRealtimeEvents(handler: Handler): void {
  const { subscribe } = useRealtime();
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => subscribe((e) => ref.current(e)), [subscribe]);
}
