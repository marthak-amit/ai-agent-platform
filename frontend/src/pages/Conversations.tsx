import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { AlertTriangle, ChevronLeft, Info, MessageSquare, Pause, Play, RefreshCw, X } from "lucide-react";
import Layout from "../components/Layout";
import ChannelIcon from "../components/chat/ChannelIcon";
import ConversationList from "../components/chat/ConversationList";
import CustomerPanel from "../components/chat/CustomerPanel";
import Lightbox from "../components/chat/Lightbox";
import MessageBubble, { OrderChip } from "../components/chat/MessageBubble";
import ReplyBox from "../components/chat/ReplyBox";
import RejectModal from "../components/payments/RejectModal";
import { useRealtime, useRealtimeEvents } from "../context/RealtimeContext";
import { useToast } from "../context/ToastContext";
import {
  apiError,
  approvePayment,
  fetchApprovedTemplates,
  fetchChat,
  fetchInbox,
  markChatRead,
  pauseBot,
  rejectPayment,
  resumeBot,
  sendChatMessage,
  uploadChatMedia,
  type SendPayload,
} from "../api/chat";
import type {
  ApprovedTemplate,
  ChatDetail,
  ChatMessage,
  InboxFilter,
  InboxItem,
  OrderEvent,
  ProofCard,
  RealtimeEvent,
} from "../types/chat";
import { clockTime, dayLabel } from "../utils/time";

// ── helpers ───────────────────────────────────────────────────────────────────

type TimelineEntry =
  | { kind: "day"; key: string; label: string }
  | { kind: "event"; key: string; event: OrderEvent }
  | { kind: "message"; key: string; message: ChatMessage };

function newClientId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** Insert or replace a message by id, keeping unsent local messages at the end. */
function upsertMessage(list: ChatMessage[], msg: ChatMessage): ChatMessage[] {
  const idx = list.findIndex((m) => m.id === msg.id && !m.localId);
  if (idx >= 0) {
    const copy = [...list];
    copy[idx] = { ...copy[idx], ...msg };
    return copy;
  }
  const server = list.filter((m) => !m.localId);
  const local = list.filter((m) => m.localId);
  return [...server, msg, ...local];
}

function buildTimeline(messages: ChatMessage[], events: OrderEvent[]): TimelineEntry[] {
  const items: { at: string; entry: TimelineEntry }[] = [
    ...messages.map((m) => ({ at: m.created_at, entry: { kind: "message", key: `m-${m.localId ?? m.id}`, message: m } as TimelineEntry })),
    ...events.map((e, i) => ({ at: e.at, entry: { kind: "event", key: `e-${e.order_id}-${e.status}-${i}`, event: e } as TimelineEntry })),
  ];
  // messages keep server order; events slot in by timestamp relative to neighbours
  items.sort((a, b) => {
    const ta = new Date(a.at).getTime();
    const tb = new Date(b.at).getTime();
    return ta === tb ? 0 : ta - tb;
  });
  const out: TimelineEntry[] = [];
  let lastDay = "";
  for (const it of items) {
    const label = dayLabel(it.at);
    if (label !== lastDay) {
      out.push({ kind: "day", key: `d-${label}-${out.length}`, label });
      lastDay = label;
    }
    out.push(it.entry);
  }
  return out;
}

function toChatMessage(raw: Record<string, unknown>): ChatMessage {
  return {
    id: raw.id as number,
    direction: raw.direction as ChatMessage["direction"],
    sender_type: raw.sender_type as ChatMessage["sender_type"],
    sender_name: null,
    text: (raw.text as string) ?? "",
    media_url: (raw.media_url as string | null) ?? null,
    media_type: (raw.media_type as string | null) ?? null,
    created_at: (raw.created_at as string) ?? new Date().toISOString(),
    payment_proof: null,
  };
}

// ── page ──────────────────────────────────────────────────────────────────────

export default function Conversations() {
  const { t } = useTranslation();
  const toast = useToast();
  const { connection, refreshPendingCount } = useRealtime();
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("id") ? Number(params.get("id")) : null;

  const [filter, setFilter] = useState<InboxFilter>("all");
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [items, setItems] = useState<InboxItem[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState(false);

  const [detail, setDetail] = useState<ChatDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);

  const [templates, setTemplates] = useState<ApprovedTemplate[]>([]);
  const [templatesLoading, setTemplatesLoading] = useState(false);
  const templatesLoaded = useRef(false);

  const [lightbox, setLightbox] = useState<string | null>(null);
  const [rejecting, setRejecting] = useState<ProofCard | null>(null);
  const [busyOrderId, setBusyOrderId] = useState<number | null>(null);
  const [botBusy, setBotBusy] = useState(false);
  const [showPanel, setShowPanel] = useState(false);

  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const selectedRef = useRef<number | null>(selectedId);
  selectedRef.current = selectedId;
  const detailRef = useRef<ChatDetail | null>(null);
  detailRef.current = detail;

  // ── inbox list ──────────────────────────────────────────────────────────────

  useEffect(() => {
    const id = window.setTimeout(() => setDebouncedSearch(search.trim()), 250);
    return () => window.clearTimeout(id);
  }, [search]);

  const loadInbox = useCallback(
    async (silent = false) => {
      if (!silent) setListLoading(true);
      try {
        setItems(await fetchInbox({ filter, search: debouncedSearch || undefined, limit: 100 }));
        setListError(false);
      } catch {
        if (!silent) setListError(true);
      } finally {
        setListLoading(false);
      }
    },
    [filter, debouncedSearch],
  );

  useEffect(() => {
    void loadInbox();
  }, [loadInbox]);

  // ── thread ──────────────────────────────────────────────────────────────────

  const loadDetail = useCallback(
    async (id: number, silent = false) => {
      if (!silent) {
        setDetailLoading(true);
        setDetail(null);
        setDetailError(false);
      }
      try {
        const d = await fetchChat(id);
        if (selectedRef.current !== id) return;
        setDetail((prev) => {
          // keep unsent/failed optimistic messages across a silent refresh
          const locals = silent && prev?.id === id ? prev.messages.filter((m) => m.localId) : [];
          return { ...d, messages: [...d.messages, ...locals] };
        });
        void markChatRead(id).catch(() => undefined);
        setItems((prev) => prev.map((c) => (c.id === id ? { ...c, unread_count: 0 } : c)));
      } catch {
        if (!silent) setDetailError(true);
      } finally {
        if (!silent) setDetailLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    stickToBottom.current = true;
    setShowPanel(false);
    if (selectedId) void loadDetail(selectedId);
    else setDetail(null);
  }, [selectedId, loadDetail]);

  // lazily load approved templates the first time a window is closed
  useEffect(() => {
    if (!detail || detail.window.open || templatesLoaded.current || detail.channel !== "whatsapp") return;
    templatesLoaded.current = true;
    setTemplatesLoading(true);
    fetchApprovedTemplates()
      .then(setTemplates)
      .catch(() => undefined)
      .finally(() => setTemplatesLoading(false));
  }, [detail]);

  // keep the newest message in view unless the user scrolled up — also when
  // content grows after the fact (images finishing loading, proof cards updating)
  const contentRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (stickToBottom.current) scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [detail?.messages.length, detail?.id]);
  useEffect(() => {
    const content = contentRef.current;
    if (!content || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      if (stickToBottom.current) scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
    });
    ro.observe(content);
    return () => ro.disconnect();
  }, [detail?.id]);

  function onScroll() {
    const el = scrollRef.current;
    if (el) stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  }

  async function loadOlder() {
    if (!detail || !detail.has_more || loadingOlder) return;
    const firstId = detail.messages.find((m) => !m.localId)?.id;
    if (!firstId) return;
    setLoadingOlder(true);
    try {
      const older = await fetchChat(detail.id, firstId);
      stickToBottom.current = false;
      setDetail((d) => (d ? { ...d, messages: [...older.messages, ...d.messages], has_more: older.has_more } : d));
    } catch {
      toast.show(t("chat.load_older_failed"), { kind: "error" });
    } finally {
      setLoadingOlder(false);
    }
  }

  // ── realtime ────────────────────────────────────────────────────────────────

  useRealtimeEvents((event: RealtimeEvent) => {
    switch (event.type) {
      case "new_message": {
        const { conversation_id: cid, message } = event.data;
        const msg = toChatMessage(message as unknown as Record<string, unknown>);
        const open = selectedRef.current === cid;
        if (open) {
          setDetail((d) => (d && d.id === cid ? { ...d, messages: upsertMessage(d.messages, msg), message_count: d.message_count + 1 } : d));
          if (msg.direction === "inbound") void markChatRead(cid).catch(() => undefined);
        }
        setItems((prev) => {
          const idx = prev.findIndex((c) => c.id === cid);
          if (idx < 0) {
            void loadInbox(true); // a conversation we haven't listed yet
            return prev;
          }
          const item = prev[idx];
          const updated: InboxItem = {
            ...item,
            last_message: { text: msg.text, direction: msg.direction, sender_type: msg.sender_type, media_type: msg.media_type, created_at: msg.created_at },
            unread_count: msg.direction === "inbound" && !open ? item.unread_count + 1 : item.unread_count,
            updated_at: msg.created_at,
          };
          return [updated, ...prev.filter((_, i) => i !== idx)];
        });
        break;
      }
      case "payment_submitted":
      case "payment_reviewed": {
        void loadInbox(true);
        const cid = event.data.conversation_id;
        if (cid && selectedRef.current === cid) void loadDetail(cid, true);
        break;
      }
      case "conversation_updated": {
        const { conversation_id: cid, bot_paused } = event.data;
        setItems((prev) => prev.map((c) => (c.id === cid ? { ...c, bot_paused, ai_enabled: !bot_paused } : c)));
        if (selectedRef.current === cid) void loadDetail(cid, true);
        break;
      }
      default:
        break;
    }
  });

  // ── sending (optimistic, idempotent retry) ─────────────────────────────────

  function patchLocal(localId: string, patch: Partial<ChatMessage>) {
    setDetail((d) => (d ? { ...d, messages: d.messages.map((m) => (m.localId === localId ? { ...m, ...patch } : m)) } : d));
  }

  async function deliver(convId: number, localId: string, payload: SendPayload) {
    try {
      const res = await sendChatMessage(convId, payload);
      setDetail((d) => {
        if (!d || d.id !== convId) return d;
        const without = d.messages.filter((m) => m.localId !== localId);
        const withServer = upsertMessage(without, { ...res.message, payment_proof: null });
        return { ...d, messages: withServer, bot_paused: res.bot_paused, ai_enabled: !res.bot_paused, auto_resume_at: res.auto_resume_at, bot_pause_source: res.bot_paused ? "human_send" : d.bot_pause_source };
      });
      setItems((prev) =>
        prev.map((c) => (c.id === convId ? { ...c, bot_paused: res.bot_paused, ai_enabled: !res.bot_paused } : c)),
      );
    } catch (err) {
      const e = apiError(err);
      patchLocal(localId, { pending: false, failed: true, failureCode: e.code });
      if (e.code === "WINDOW_CLOSED") {
        setTemplates(e.templates ?? []);
        templatesLoaded.current = true;
        setDetail((d) => (d ? { ...d, window: { open: false, closes_at: d.window.closes_at, seconds_left: 0 } } : d));
        toast.show(t("chat.err_window_closed"), { kind: "error" });
      } else if (e.code === "OPTED_OUT" || e.code === "BLOCKED") {
        setDetail((d) => (d ? { ...d, opted_out: true } : d));
        toast.show(t("chat.err_opted_out"), { kind: "error" });
      } else {
        toast.show(t("chat.err_send_failed"), { kind: "error" });
      }
    }
  }

  function optimistic(partial: Partial<ChatMessage>, localId: string): ChatMessage {
    return {
      id: -Date.now(),
      localId,
      direction: "outbound",
      sender_type: "human",
      sender_name: null,
      text: "",
      media_url: null,
      media_type: null,
      created_at: new Date().toISOString(),
      payment_proof: null,
      pending: true,
      ...partial,
    };
  }

  function addLocal(msg: ChatMessage) {
    stickToBottom.current = true;
    setDetail((d) => (d ? { ...d, messages: [...d.messages, msg] } : d));
  }

  function sendText(text: string) {
    if (!detail) return;
    const localId = newClientId();
    addLocal(optimistic({ text }, localId));
    void deliver(detail.id, localId, { text, client_msg_id: localId });
  }

  async function sendImage(file: File, caption: string) {
    if (!detail) return;
    const convId = detail.id;
    const localId = newClientId();
    addLocal(optimistic({ text: caption, media_type: "image", media_url: URL.createObjectURL(file) }, localId));
    try {
      const up = await uploadChatMedia(convId, file);
      patchLocal(localId, { media_url: up.media_url });
      await deliver(convId, localId, { media_url: up.media_url, caption: caption || undefined, client_msg_id: localId });
    } catch (err) {
      patchLocal(localId, { pending: false, failed: true, failureCode: apiError(err).code });
      toast.show(t("chat.err_upload_failed"), { kind: "error" });
    }
  }

  function sendTemplate(tpl: ApprovedTemplate, variables: string[]) {
    if (!detail) return;
    const localId = newClientId();
    let preview = tpl.body;
    variables.forEach((v, i) => { preview = preview.replace(`{{${i + 1}}}`, v); });
    addLocal(optimistic({ text: preview }, localId));
    void deliver(detail.id, localId, { template: { name: tpl.name, language: tpl.language, variables }, client_msg_id: localId });
  }

  function retry(msg: ChatMessage) {
    if (!detail || !msg.localId) return;
    patchLocal(msg.localId, { pending: true, failed: false, failureCode: undefined });
    const base: SendPayload = { client_msg_id: msg.localId };
    if (msg.media_type === "image" && msg.media_url && !msg.media_url.startsWith("blob:")) {
      void deliver(detail.id, msg.localId, { ...base, media_url: msg.media_url, caption: msg.text || undefined });
    } else if (msg.media_type === "image") {
      // upload never completed (blob: preview only) — user must re-attach
      patchLocal(msg.localId, { pending: false, failed: true });
      toast.show(t("chat.err_reattach"), { kind: "error" });
    } else {
      void deliver(detail.id, msg.localId, { ...base, text: msg.text });
    }
  }

  // ── payment review from the thread ──────────────────────────────────────────

  function setProofStatus(orderId: number, patch: Partial<ProofCard>) {
    setDetail((d) =>
      d
        ? { ...d, messages: d.messages.map((m) => (m.payment_proof?.order_id === orderId && m.payment_proof.status === "pending" ? { ...m, payment_proof: { ...m.payment_proof, ...patch } } : m)) }
        : d,
    );
  }

  async function approve(proof: ProofCard) {
    setBusyOrderId(proof.order_id);
    const snapshot = detailRef.current;
    setProofStatus(proof.order_id, { status: "approved", reviewed_by: t("chat.you"), reviewed_at: new Date().toISOString() });
    try {
      const res = await approvePayment(proof.order_id);
      toast.show(
        res.customer_notified === false ? t("chat.approved_not_notified") : t("chat.approved_toast", { order: proof.order_number }),
        { kind: res.customer_notified === false ? "info" : "success" },
      );
      refreshPendingCount();
      if (selectedRef.current) void loadDetail(selectedRef.current, true);
      void loadInbox(true);
    } catch (err) {
      if (snapshot) setDetail(snapshot);
      const e = apiError(err);
      toast.show(e.code === "INVALID_STATE" ? t("chat.err_state_changed") : t("chat.err_review_failed"), { kind: "error" });
      if (selectedRef.current) void loadDetail(selectedRef.current, true);
    } finally {
      setBusyOrderId(null);
    }
  }

  async function confirmReject(reason: string) {
    if (!rejecting) return;
    const proof = rejecting;
    setBusyOrderId(proof.order_id);
    try {
      await rejectPayment(proof.order_id, reason);
      setRejecting(null);
      setProofStatus(proof.order_id, { status: "rejected", reject_reason: reason || null, reviewed_by: t("chat.you"), reviewed_at: new Date().toISOString() });
      toast.show(t("chat.rejected_toast", { order: proof.order_number }), { kind: "success" });
      refreshPendingCount();
      if (selectedRef.current) void loadDetail(selectedRef.current, true);
      void loadInbox(true);
    } catch (err) {
      const e = apiError(err);
      toast.show(e.code === "INVALID_STATE" ? t("chat.err_state_changed") : t("chat.err_review_failed"), { kind: "error" });
    } finally {
      setBusyOrderId(null);
    }
  }

  // ── bot toggle ──────────────────────────────────────────────────────────────

  async function toggleBot() {
    if (!detail) return;
    setBotBusy(true);
    try {
      const updated = detail.bot_paused ? await resumeBot(detail.id) : await pauseBot(detail.id);
      setDetail((d) => (d ? { ...d, ...updated, messages: d.messages } : d));
      setItems((prev) => prev.map((c) => (c.id === detail.id ? { ...c, bot_paused: updated.bot_paused, ai_enabled: updated.ai_enabled } : c)));
    } catch {
      toast.show(t("chat.err_bot_toggle"), { kind: "error" });
    } finally {
      setBotBusy(false);
    }
  }

  // ── render ──────────────────────────────────────────────────────────────────

  const timeline = useMemo(() => (detail ? buildTimeline(detail.messages, detail.events) : []), [detail]);
  const selectConversation = (id: number) => setParams({ id: String(id) });
  const closeConversation = () => setParams({});
  const title = detail?.customer_name?.trim() || detail?.phone_number || "";

  return (
    <Layout>
      <div className="flex gap-3 h-[calc(100dvh-9rem)] lg:h-[calc(100vh-4.5rem)]">
        {/* LEFT — conversation list */}
        <aside
          className={`${selectedId ? "hidden lg:block" : "block"} w-full lg:w-80 xl:w-[340px] shrink-0 rounded-xl border border-gray-100 shadow-sm overflow-hidden`}
        >
          <ConversationList
            items={items}
            loading={listLoading}
            error={listError}
            selectedId={selectedId}
            filter={filter}
            search={search}
            onFilter={setFilter}
            onSearch={setSearch}
            onSelect={selectConversation}
            onRetry={() => void loadInbox()}
          />
        </aside>

        {/* MIDDLE — thread */}
        <section
          className={`${selectedId ? "flex" : "hidden lg:flex"} flex-1 min-w-0 flex-col rounded-xl border border-gray-100 shadow-sm overflow-hidden bg-brand-bg`}
        >
          {!selectedId ? (
            <div className="flex-1 flex flex-col items-center justify-center text-center p-8 bg-white">
              <div className="w-16 h-16 bg-brand-bg rounded-2xl flex items-center justify-center mb-4">
                <MessageSquare size={28} className="text-gray-300" />
              </div>
              <p className="text-brand-secondary font-medium">{t("chat.select_conversation")}</p>
              <p className="text-sm text-gray-400 mt-1">{t("chat.select_conversation_hint")}</p>
            </div>
          ) : detailLoading ? (
            <div className="flex-1 flex items-center justify-center bg-white" aria-busy="true">
              <div className="w-8 h-8 border-[3px] border-brand-primary/20 border-t-brand-primary rounded-full animate-spin" />
            </div>
          ) : detailError || !detail ? (
            <div className="flex-1 flex flex-col items-center justify-center gap-2 bg-white text-center p-8">
              <AlertTriangle size={28} className="text-red-400" />
              <p className="text-sm text-gray-600">{t("chat.thread_error")}</p>
              <div className="flex gap-3">
                <button onClick={() => selectedId && void loadDetail(selectedId)} className="flex items-center gap-1.5 text-sm font-medium text-brand-primaryDark hover:underline">
                  <RefreshCw size={14} /> {t("chat.retry")}
                </button>
                <button onClick={closeConversation} className="text-sm text-gray-500 hover:underline lg:hidden">{t("chat.back")}</button>
              </div>
            </div>
          ) : (
            <>
              {/* header */}
              <header className="bg-white border-b border-gray-100 px-3 sm:px-4 py-3 flex items-center gap-3">
                <button onClick={closeConversation} className="lg:hidden p-1.5 -ml-1 text-brand-secondary" aria-label={t("chat.back")}>
                  <ChevronLeft size={20} />
                </button>
                <div className="w-9 h-9 rounded-full bg-brand-secondary text-white flex items-center justify-center text-xs font-bold shrink-0">
                  {title.slice(0, 2).toUpperCase()}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="font-semibold text-sm text-brand-secondary truncate">{title}</div>
                  <div className="flex items-center gap-1.5 text-xs text-gray-500">
                    <ChannelIcon channel={detail.channel} size={12} />
                    <span className="truncate">{detail.phone_number}</span>
                  </div>
                </div>

                <div className="flex items-center gap-2">
                  <div
                    className={`hidden sm:flex items-center gap-1.5 text-xs font-medium px-2.5 py-1.5 rounded-full ${
                      detail.bot_paused ? "bg-brand-warning/20 text-amber-900" : "bg-brand-primary/15 text-brand-primaryDark"
                    }`}
                  >
                    <span className={`w-2 h-2 rounded-full ${detail.bot_paused ? "bg-brand-warning" : "bg-brand-primary animate-pulse"}`} />
                    {detail.bot_paused ? t("chat.ai_paused") : t("chat.ai_replying")}
                  </div>
                  <button
                    onClick={toggleBot}
                    disabled={botBusy}
                    className={`flex items-center gap-1.5 text-xs font-semibold px-3 py-2 rounded-lg disabled:opacity-50 transition-colors ${
                      detail.bot_paused
                        ? "bg-brand-primary text-white hover:bg-brand-primaryDark"
                        : "border border-gray-200 text-gray-700 hover:bg-brand-bg"
                    }`}
                  >
                    {detail.bot_paused ? <Play size={13} /> : <Pause size={13} />}
                    {detail.bot_paused ? t("chat.resume_ai") : t("chat.pause_ai")}
                  </button>
                  <button
                    onClick={() => setShowPanel(true)}
                    className="xl:hidden p-2 rounded-lg border border-gray-200 text-gray-600 hover:bg-brand-bg"
                    aria-label={t("chat.customer_info")}
                  >
                    <Info size={16} />
                  </button>
                </div>
              </header>

              {detail.bot_paused && (
                <div className="bg-brand-warning/15 border-b border-brand-warning/40 px-4 py-2 text-xs text-amber-900 flex flex-wrap items-center gap-x-2">
                  <Pause size={12} />
                  <span className="font-semibold">{t("chat.ai_paused")}</span>
                  {detail.bot_pause_source === "human_send" && detail.auto_resume_at && (
                    <span className="text-amber-800/80">{t("chat.auto_resume_at", { time: clockTime(detail.auto_resume_at) })}</span>
                  )}
                  {detail.taken_over_note && <span className="italic text-amber-800/70 truncate">“{detail.taken_over_note}”</span>}
                </div>
              )}
              {connection === "polling" && (
                <div className="bg-gray-100 px-4 py-1 text-[11px] text-gray-500 text-center">{t("chat.polling_mode")}</div>
              )}

              {/* messages */}
              <div ref={scrollRef} onScroll={onScroll} className="flex-1 overflow-y-auto px-3 sm:px-5 py-4">
               <div ref={contentRef} className="space-y-3">
                {detail.has_more && (
                  <div className="text-center">
                    <button
                      onClick={loadOlder}
                      disabled={loadingOlder}
                      className="text-xs text-brand-primaryDark font-medium hover:underline disabled:opacity-50"
                    >
                      {loadingOlder ? t("chat.loading") : t("chat.load_earlier")}
                    </button>
                  </div>
                )}
                {timeline.length === 0 ? (
                  <p className="text-sm text-gray-400 text-center py-10">{t("chat.no_messages_yet")}</p>
                ) : (
                  timeline.map((entry) =>
                    entry.kind === "day" ? (
                      <div key={entry.key} className="flex justify-center">
                        <span className="text-[11px] text-gray-500 bg-white border border-gray-200 rounded-full px-3 py-0.5">{entry.label}</span>
                      </div>
                    ) : entry.kind === "event" ? (
                      <OrderChip key={entry.key} event={entry.event} />
                    ) : (
                      <MessageBubble
                        key={entry.key}
                        msg={entry.message}
                        onOpenImage={setLightbox}
                        onRetry={retry}
                        onApprove={approve}
                        onReject={setRejecting}
                        busyOrderId={busyOrderId}
                      />
                    ),
                  )
                )}
               </div>
              </div>

              <ReplyBox
                detail={detail}
                templates={templates}
                templatesLoading={templatesLoading}
                onSendText={sendText}
                onSendImage={(f, c) => void sendImage(f, c)}
                onSendTemplate={sendTemplate}
              />
            </>
          )}
        </section>

        {/* RIGHT — customer panel (xl+) */}
        <aside className="hidden xl:block w-[300px] shrink-0 rounded-xl border border-gray-100 shadow-sm overflow-hidden">
          {detail && selectedId ? (
            <CustomerPanel detail={detail} />
          ) : (
            <div className="h-full bg-white flex items-center justify-center text-sm text-gray-300 p-6 text-center">
              {t("chat.customer_panel_empty")}
            </div>
          )}
        </aside>
      </div>

      {/* customer panel drawer for narrower screens */}
      {showPanel && detail && (
        <div className="fixed inset-0 z-50 xl:hidden">
          <div className="absolute inset-0 bg-black/40" onClick={() => setShowPanel(false)} />
          <div className="absolute right-0 top-0 h-full w-[min(20rem,90vw)] bg-white shadow-xl">
            <button onClick={() => setShowPanel(false)} className="absolute top-3 right-3 z-10 p-1.5 rounded-full bg-brand-bg" aria-label={t("chat.close")}>
              <X size={16} />
            </button>
            <CustomerPanel detail={detail} />
          </div>
        </div>
      )}

      {lightbox && <Lightbox images={[lightbox]} onClose={() => setLightbox(null)} />}
      {rejecting && (
        <RejectModal
          orderNumber={rejecting.order_number}
          busy={busyOrderId === rejecting.order_id}
          onConfirm={(r) => void confirmReject(r)}
          onCancel={() => setRejecting(null)}
        />
      )}
    </Layout>
  );
}
