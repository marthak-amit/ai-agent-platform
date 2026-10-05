import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { AlertTriangle, CheckCircle2, MessageCircle, RefreshCw, Search } from "lucide-react";
import Layout from "../components/Layout";
import Lightbox from "../components/chat/Lightbox";
import ChannelIcon from "../components/chat/ChannelIcon";
import PaymentCard from "../components/payments/PaymentCard";
import RejectModal from "../components/payments/RejectModal";
import { useRealtime, useRealtimeEvents } from "../context/RealtimeContext";
import { useToast } from "../context/ToastContext";
import { useNow } from "../hooks/useNow";
import { apiError, approvePayment, fetchPayments, rejectPayment } from "../api/chat";
import type { PaymentRow, ProofStatus, RealtimeEvent } from "../types/chat";
import { dateTime, rupees } from "../utils/time";

/** How long an Approve can be undone. The API is only called AFTER this window (see commitApproval). */
const UNDO_MS = 5_000;

type Tab = ProofStatus;
const TABS: { key: Tab; i18n: string }[] = [
  { key: "pending", i18n: "payments.tab_verify" },
  { key: "approved", i18n: "payments.tab_approved" },
  { key: "rejected", i18n: "payments.tab_rejected" },
];

interface PendingApproval {
  row: PaymentRow;
  index: number;
  timer: number;
}

/** Newest submission first (a new screenshot lands at the top of the grid). */
function newestFirst(rows: PaymentRow[]): PaymentRow[] {
  return [...rows].sort((a, b) => (b.waiting_since ?? "").localeCompare(a.waiting_since ?? ""));
}

export default function Payments() {
  const { t } = useTranslation();
  const toast = useToast();
  const { refreshPendingCount } = useRealtime();
  const now = useNow(30_000);

  const [tab, setTab] = useState<Tab>("pending");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [rows, setRows] = useState<PaymentRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [activeIdx, setActiveIdx] = useState(0);
  const [lightbox, setLightbox] = useState<{ urls: string[]; index: number } | null>(null);
  const [rejecting, setRejecting] = useState<PaymentRow | null>(null);
  const [rejectBusy, setRejectBusy] = useState(false);

  const pendingApprovals = useRef(new Map<number, PendingApproval>());
  const rowsRef = useRef(rows);
  rowsRef.current = rows;
  const tabRef = useRef(tab);
  tabRef.current = tab;

  useEffect(() => {
    const id = window.setTimeout(() => setDebouncedSearch(search.trim()), 250);
    return () => window.clearTimeout(id);
  }, [search]);

  const load = useCallback(
    async (silent = false) => {
      if (!silent) {
        setLoading(true);
        setError(false);
      }
      try {
        const data = await fetchPayments({
          status: tab,
          from: from || undefined,
          to: to || undefined,
          search: debouncedSearch || undefined,
          limit: 100,
        });
        // never resurrect a card the seller has approved but not yet committed
        const hidden = pendingApprovals.current;
        const visible = tab === "pending" ? newestFirst(data.filter((r) => !hidden.has(r.order_id))) : data;
        setRows(visible);
        setActiveIdx((i) => Math.min(i, Math.max(0, visible.length - 1)));
      } catch {
        if (!silent) setError(true);
      } finally {
        setLoading(false);
      }
    },
    [tab, from, to, debouncedSearch],
  );

  useEffect(() => {
    void load();
  }, [load]);

  // A new screenshot (or any review elsewhere) refreshes the list without a page reload.
  useRealtimeEvents((event: RealtimeEvent) => {
    if (event.type === "payment_submitted" || event.type === "payment_reviewed") void load(true);
  });

  // ── approve: optimistic remove + 5 s undo; API is called only after the window ──
  //
  // Approve is irreversible server-side (it deducts stock and messages the
  // customer), and there is no "un-approve" endpoint. So rather than calling
  // approve and trying to revert, we hide the card instantly and delay the
  // request until the undo window has passed. Undo just cancels the timer —
  // nothing was ever sent. Pending approvals are flushed immediately if the
  // seller navigates away (unmount) or closes the tab (keepalive fetch), so an
  // approval can never be silently lost.

  const commitApproval = useCallback(
    async (orderId: number) => {
      const pending = pendingApprovals.current.get(orderId);
      if (!pending) return;
      window.clearTimeout(pending.timer);
      pendingApprovals.current.delete(orderId);
      try {
        const res = await approvePayment(orderId);
        toast.show(
          res.customer_notified === false ? t("payments.not_notified") : t("payments.approved_toast", { order: pending.row.order_number }),
          { kind: res.customer_notified === false ? "info" : "success" },
        );
      } catch (err) {
        const e = apiError(err);
        toast.show(e.code === "INVALID_STATE" ? t("payments.err_state_changed") : t("payments.err_review_failed"), { kind: "error" });
        if (e.code !== "INVALID_STATE" && tabRef.current === "pending") {
          // restore so the seller can retry
          setRows((prev) => {
            const copy = [...prev];
            copy.splice(Math.min(pending.index, copy.length), 0, pending.row);
            return copy;
          });
        }
      } finally {
        refreshPendingCount();
        void load(true);
      }
    },
    [load, refreshPendingCount, t, toast],
  );

  function approve(row: PaymentRow, index: number) {
    if (pendingApprovals.current.has(row.order_id)) return;
    setRows((prev) => prev.filter((r) => r.order_id !== row.order_id));
    setActiveIdx((i) => Math.max(0, Math.min(i, rowsRef.current.length - 2)));
    const timer = window.setTimeout(() => void commitApproval(row.order_id), UNDO_MS);
    pendingApprovals.current.set(row.order_id, { row, index, timer });
    toast.show(t("payments.approving_toast", { order: row.order_number }), {
      kind: "success",
      duration: UNDO_MS,
      action: {
        label: t("payments.undo"),
        onClick: () => {
          const p = pendingApprovals.current.get(row.order_id);
          if (!p) return;
          window.clearTimeout(p.timer);
          pendingApprovals.current.delete(row.order_id);
          setRows((prev) => {
            const copy = [...prev];
            copy.splice(Math.min(p.index, copy.length), 0, p.row);
            return copy;
          });
        },
      },
    });
  }

  // flush on unmount / tab close (stable effect: switching tabs/filters must NOT cut an undo window short)
  const commitRef = useRef(commitApproval);
  commitRef.current = commitApproval;
  useEffect(() => {
    const flushAll = () => [...pendingApprovals.current.keys()].forEach((id) => void commitRef.current(id));
    const onPageHide = () => {
      const token = localStorage.getItem("access_token");
      const base = (import.meta.env.VITE_API_URL as string | undefined) ?? "/api";
      pendingApprovals.current.forEach((p, id) => {
        window.clearTimeout(p.timer);
        void fetch(`${base}/orders/${id}/payment/approve`, {
          method: "POST",
          keepalive: true,
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        }).catch(() => undefined);
      });
      pendingApprovals.current.clear();
    };
    window.addEventListener("pagehide", onPageHide);
    return () => {
      window.removeEventListener("pagehide", onPageHide);
      flushAll();
    };
  }, []);

  async function confirmReject(reason: string) {
    if (!rejecting) return;
    const row = rejecting;
    setRejectBusy(true);
    try {
      await rejectPayment(row.order_id, reason);
      setRows((prev) => prev.filter((r) => r.order_id !== row.order_id));
      toast.show(t("payments.rejected_toast", { order: row.order_number }), { kind: "success" });
      setRejecting(null);
      refreshPendingCount();
    } catch (err) {
      const e = apiError(err);
      toast.show(e.code === "INVALID_STATE" ? t("payments.err_state_changed") : t("payments.err_review_failed"), { kind: "error" });
      if (e.code === "INVALID_STATE") {
        setRejecting(null);
        void load(true);
      }
    } finally {
      setRejectBusy(false);
    }
  }

  // ── keyboard: A approve · R reject · ← → move between cards ──
  const approveRef = useRef(approve);
  approveRef.current = approve;
  useEffect(() => {
    if (tab !== "pending") return;
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey || rejecting || lightbox) return;
      const list = rowsRef.current;
      if (list.length === 0) return;
      if (e.key === "ArrowRight" || e.key === "ArrowDown") {
        e.preventDefault();
        setActiveIdx((i) => Math.min(list.length - 1, i + 1));
      } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
        e.preventDefault();
        setActiveIdx((i) => Math.max(0, i - 1));
      } else if (e.key.toLowerCase() === "a") {
        const idx = Math.min(activeIdx, list.length - 1);
        approveRef.current(list[idx], idx);
      } else if (e.key.toLowerCase() === "r") {
        setRejecting(list[Math.min(activeIdx, list.length - 1)]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, activeIdx, rejecting, lightbox]);

  // keep the active card in view when navigating with the keyboard
  useEffect(() => {
    document.querySelectorAll('[data-testid="payment-card"]')[activeIdx]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [activeIdx]);

  const reviewed = tab !== "pending";
  const emptyKey = useMemo(
    () => (tab === "pending" ? "payments.empty_title" : tab === "approved" ? "payments.empty_approved" : "payments.empty_rejected"),
    [tab],
  );

  return (
    <Layout>
      <div className="max-w-[1200px] mx-auto space-y-4">
        <div>
          <h1 className="text-xl font-bold text-brand-secondary">{t("payments.title")}</h1>
          <p className="text-sm text-gray-500">{t("payments.subtitle")}</p>
        </div>

        {/* tabs + filters */}
        <div className="bg-white border border-gray-100 rounded-xl shadow-sm p-3 space-y-3">
          <div className="flex gap-1.5 overflow-x-auto scrollbar-hide" role="tablist">
            {TABS.map((tb) => (
              <button
                key={tb.key}
                role="tab"
                aria-selected={tab === tb.key}
                onClick={() => { setTab(tb.key); setActiveIdx(0); }}
                className={`shrink-0 px-4 py-2 rounded-lg text-sm font-semibold transition-colors ${
                  tab === tb.key ? "bg-brand-secondary text-white" : "text-gray-600 hover:bg-brand-bg"
                }`}
              >
                {t(tb.i18n)}
                {tb.key === "pending" && tab === "pending" && rows.length > 0 && (
                  <span className="ml-2 text-[11px] bg-brand-warning text-brand-secondary rounded-full px-1.5 py-0.5">{rows.length}</span>
                )}
              </button>
            ))}
          </div>
          <div className="flex flex-col sm:flex-row gap-2">
            <div className="relative flex-1">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
              <input
                type="search"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t("payments.search_placeholder")}
                aria-label={t("payments.search_placeholder")}
                className="w-full pl-8 pr-3 py-2 text-sm border border-gray-200 rounded-lg bg-brand-bg focus:outline-none focus:ring-2 focus:ring-brand-primary"
              />
            </div>
            <label className="flex items-center gap-2 text-xs text-gray-500">
              {t("payments.from")}
              <input type="date" value={from} max={to || undefined} onChange={(e) => setFrom(e.target.value)} className="border border-gray-200 rounded-lg px-2 py-1.5 text-sm text-gray-800" />
            </label>
            <label className="flex items-center gap-2 text-xs text-gray-500">
              {t("payments.to")}
              <input type="date" value={to} min={from || undefined} onChange={(e) => setTo(e.target.value)} className="border border-gray-200 rounded-lg px-2 py-1.5 text-sm text-gray-800" />
            </label>
            {(from || to) && (
              <button onClick={() => { setFrom(""); setTo(""); }} className="text-xs text-brand-primaryDark font-medium hover:underline">
                {t("payments.clear")}
              </button>
            )}
          </div>
          {tab === "pending" && <p className="hidden md:block text-[11px] text-gray-400">{t("payments.shortcuts")}</p>}
        </div>

        {/* body */}
        {loading ? (
          <div className={reviewed ? "space-y-2" : "grid gap-4 sm:grid-cols-2 xl:grid-cols-3"} aria-busy="true">
            {[...Array(reviewed ? 5 : 3)].map((_, i) => (
              <div key={i} className={`${reviewed ? "h-12" : "h-96"} bg-white border border-gray-100 rounded-2xl animate-pulse`} />
            ))}
          </div>
        ) : error ? (
          <div className="bg-white border border-gray-100 rounded-2xl p-10 flex flex-col items-center gap-2 text-center">
            <AlertTriangle size={30} className="text-red-400" />
            <p className="text-sm text-gray-600">{t("payments.error")}</p>
            <button onClick={() => void load()} className="flex items-center gap-1.5 text-sm font-medium text-brand-primaryDark hover:underline">
              <RefreshCw size={14} /> {t("payments.retry")}
            </button>
          </div>
        ) : rows.length === 0 ? (
          <div className="bg-white border border-gray-100 rounded-2xl p-12 flex flex-col items-center gap-2 text-center">
            <CheckCircle2 size={36} className="text-brand-primary" />
            <p className="text-lg font-semibold text-brand-secondary">{t(emptyKey)}</p>
            {tab === "pending" && <p className="text-sm text-gray-400">{t("payments.empty_sub")}</p>}
          </div>
        ) : tab === "pending" ? (
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {rows.map((row, i) => (
              <PaymentCard
                key={row.order_id}
                row={row}
                active={i === activeIdx}
                now={now}
                onActivate={() => setActiveIdx(i)}
                onOpenImages={(urls, index) => setLightbox({ urls, index })}
                onApprove={() => approve(row, i)}
                onReject={() => setRejecting(row)}
              />
            ))}
          </div>
        ) : (
          <div className="bg-white border border-gray-100 rounded-xl shadow-sm overflow-x-auto">
            <table className="w-full text-sm min-w-[640px]">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-wide text-gray-400 border-b border-gray-100">
                  <th className="px-4 py-3 font-semibold">{t("payments.th_order")}</th>
                  <th className="px-4 py-3 font-semibold">{t("payments.th_customer")}</th>
                  <th className="px-4 py-3 font-semibold text-right">{t("payments.th_amount")}</th>
                  <th className="px-4 py-3 font-semibold">{t("payments.th_reviewed_by")}</th>
                  <th className="px-4 py-3 font-semibold">{t("payments.th_time")}</th>
                  {tab === "rejected" && <th className="px-4 py-3 font-semibold">{t("payments.th_reason")}</th>}
                  <th className="px-2 py-3" />
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.order_id} className="border-b border-gray-50 last:border-0 hover:bg-brand-bg">
                    <td className="px-4 py-3 font-semibold text-brand-secondary">#{r.order_number}</td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <ChannelIcon channel={r.channel} size={14} />
                        <span className="text-gray-800">{r.customer_name}</span>
                        <span className="text-xs text-gray-400">{r.customer_phone}</span>
                      </div>
                    </td>
                    <td className="px-4 py-3 text-right font-semibold tabular-nums">{rupees(r.amount_expected)}</td>
                    <td className="px-4 py-3 text-gray-700">{r.reviewed_by ?? "—"}</td>
                    <td className="px-4 py-3 text-gray-500 whitespace-nowrap">{dateTime(r.reviewed_at)}</td>
                    {tab === "rejected" && <td className="px-4 py-3 text-gray-600">{r.reject_reason ?? "—"}</td>}
                    <td className="px-2 py-3">
                      {r.conversation_id && (
                        <Link to={`/conversations?id=${r.conversation_id}`} className="text-gray-400 hover:text-brand-primaryDark" aria-label={t("payments.open_chat")}>
                          <MessageCircle size={16} />
                        </Link>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {lightbox && <Lightbox images={lightbox.urls} startIndex={lightbox.index} onClose={() => setLightbox(null)} />}
      {rejecting && (
        <RejectModal
          orderNumber={rejecting.order_number}
          busy={rejectBusy}
          onConfirm={(r) => void confirmReject(r)}
          onCancel={() => setRejecting(null)}
        />
      )}
    </Layout>
  );
}
