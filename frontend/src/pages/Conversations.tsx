import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  getConversation,
  getConversations,
  resumeConversation,
  sendHumanMessage,
  takeoverConversation,
} from "../api/client";
import Layout from "../components/Layout";
import type { ConversationDetail, ConversationSummary, Message } from "../types";
import { Search, Send, AlertTriangle, MessageSquare, ChevronLeft } from "lucide-react";

// ── Constants ─────────────────────────────────────────────────────────────────

const LEAD_COLORS: Record<string, string> = {
  hot: "bg-red-100 text-red-700",
  warm: "bg-amber-100 text-amber-700",
  cold: "bg-blue-100 text-blue-700",
};

const AVATAR_COLORS: Record<string, string> = {
  hot: "bg-red-500",
  warm: "bg-amber-500",
  cold: "bg-blue-500",
};

const CHANNEL_ICON: Record<string, string> = {
  whatsapp: "💬",
  instagram: "📸",
  website: "🌐",
};

const FILTER_TABS = [
  { key: "all", label: "All" },
  { key: "whatsapp", label: "WhatsApp" },
  { key: "instagram", label: "Instagram" },
  { key: "hot", label: "Hot" },
  { key: "warm", label: "Warm" },
  { key: "cold", label: "Cold" },
  { key: "takeover", label: "Takeover" },
];

// Sales funnel stages — ordered for progress bar
const FUNNEL_STAGES = [
  { key: "greeting",         label: "Greeting",    color: "bg-gray-400"   },
  { key: "product_inquiry",  label: "Inquiring",   color: "bg-blue-500"   },
  { key: "qualification",    label: "Qualifying",  color: "bg-amber-500"  },
  { key: "objection_handling", label: "Objection", color: "bg-orange-500" },
  { key: "offer_making",     label: "Offer Made",  color: "bg-orange-600" },
  { key: "order_collection", label: "Ordering",    color: "bg-red-500"    },
  { key: "payment",          label: "Payment",     color: "bg-green-500"  },
  { key: "completed",        label: "Done",        color: "bg-green-700"  },
];

const STAGE_BADGE_COLORS: Record<string, string> = {
  greeting:          "bg-gray-100 text-gray-600",
  product_inquiry:   "bg-blue-100 text-blue-700",
  qualification:     "bg-amber-100 text-amber-700",
  objection_handling:"bg-orange-100 text-orange-700",
  offer_making:      "bg-orange-100 text-orange-700",
  order_collection:  "bg-red-100 text-red-700",
  payment:           "bg-green-100 text-green-700",
  completed:         "bg-emerald-100 text-emerald-700",
  off_topic:         "bg-gray-100 text-gray-500",
};

// ── Helpers ───────────────────────────────────────────────────────────────────

function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "";
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days === 1) return "yesterday";
  return `${days}d ago`;
}

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
  });
}

function initials(phone: string): string {
  return phone.replace(/\D/g, "").slice(-2) || "??";
}

// ── Takeover modal ────────────────────────────────────────────────────────────

function TakeoverModal({ onConfirm, onCancel }: { onConfirm: (note: string) => void; onCancel: () => void }) {
  const [note, setNote] = useState("");
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onCancel} />
      <div className="relative bg-white rounded-2xl shadow-xl w-full max-w-sm p-6 z-10 animate-in fade-in zoom-in-95 duration-200">
        <h2 className="text-base font-semibold text-gray-900 mb-1">Pause AI for this chat?</h2>
        <p className="text-sm text-gray-500 mb-4">
          The AI will stop replying. Incoming messages are saved silently — reply directly on WhatsApp.
        </p>
        <textarea
          rows={2}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Reason (optional) — e.g. VIP customer, handling manually"
          className="w-full border border-gray-200 rounded-xl px-3 py-2 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-amber-400 mb-4"
        />
        <div className="flex gap-2">
          <button
            onClick={() => onConfirm(note)}
            className="flex-1 bg-amber-500 text-white py-2.5 rounded-xl text-sm font-semibold hover:bg-amber-600 transition-colors"
          >
            Pause AI & Take Over
          </button>
          <button
            onClick={onCancel}
            className="px-4 py-2 border border-gray-200 rounded-xl text-sm text-gray-600 hover:bg-gray-50 transition-colors"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Conversation list item ────────────────────────────────────────────────────

function ConvItem({ conv, selected, onClick }: { conv: ConversationSummary; selected: boolean; onClick: () => void }) {
  const aiOn = conv.ai_enabled !== false;
  const avatarColor = AVATAR_COLORS[conv.lead_status] ?? "bg-gray-400";

  return (
    <button
      onClick={onClick}
      className={`w-full text-left px-4 py-3.5 border-b border-gray-50 transition-all duration-150 ${
        selected ? "bg-indigo-50" : "hover:bg-gray-50"
      }`}
    >
      <div className="flex items-start gap-3">
        {/* Avatar */}
        <div className="relative shrink-0">
          <div className={`w-10 h-10 rounded-full ${avatarColor} flex items-center justify-center text-white text-xs font-bold`}>
            {initials(conv.phone_number)}
          </div>
          <span className="absolute -bottom-0.5 -right-0.5 text-xs leading-none">
            {CHANNEL_ICON[conv.channel] ?? "💬"}
          </span>
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-center justify-between gap-1 mb-0.5">
            <span className="font-semibold text-sm text-gray-900 truncate">{conv.phone_number}</span>
            <span className="text-[10px] text-gray-400 shrink-0">{timeAgo(conv.updated_at)}</span>
          </div>
          <p className="text-xs text-gray-500 truncate leading-relaxed">
            {conv.last_message || "No messages yet"}
          </p>
          <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
            <span
              className={`text-[10px] font-medium px-1.5 py-0.5 rounded-full capitalize ${
                LEAD_COLORS[conv.lead_status] ?? LEAD_COLORS.cold
              }`}
            >
              {conv.lead_status}
            </span>
            {conv.current_stage && conv.current_stage !== "greeting" && (
              <span
                className={`text-[10px] font-medium px-1.5 py-0.5 rounded-full capitalize ${
                  STAGE_BADGE_COLORS[conv.current_stage] ?? "bg-gray-100 text-gray-500"
                }`}
              >
                {FUNNEL_STAGES.find((s) => s.key === conv.current_stage)?.label ?? conv.current_stage}
              </span>
            )}
            {!aiOn && (
              <span className="text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-amber-100 text-amber-700">
                Takeover
              </span>
            )}
          </div>
        </div>
      </div>
    </button>
  );
}

// ── Chat bubble ───────────────────────────────────────────────────────────────

function ChatBubble({ msg }: { msg: Message }) {
  const { t } = useTranslation();
  const isCustomer = msg.role === "user";
  const isHuman = msg.role === "human";

  return (
    <div className={`flex flex-col ${isCustomer ? "items-start" : "items-end"} gap-1`}>
      <span className="text-[10px] text-gray-400 px-1">
        {isCustomer ? t("conversations.customer") : isHuman ? t("conversations.you") : t("conversations.ai_agent")}
      </span>
      <div
        className={`max-w-[75%] px-4 py-2.5 rounded-2xl text-sm leading-relaxed shadow-sm ${
          isCustomer
            ? "bg-white border border-gray-200 text-gray-800 rounded-tl-sm"
            : isHuman
            ? "bg-green-500 text-white rounded-tr-sm"
            : "bg-indigo-600 text-white rounded-tr-sm"
        }`}
      >
        {msg.original_type === "audio" ? (
          <span className="flex items-start gap-1.5">
            <span className="mt-0.5">🎤</span>
            <em className="not-italic">
              <span className="font-medium text-xs opacity-70 mr-1">Voice note:</span>
              {msg.content}
            </em>
          </span>
        ) : msg.original_type === "image" ? (
          <span className="flex items-start gap-1.5">
            <span className="mt-0.5">🖼️</span>
            <span>{msg.content}</span>
          </span>
        ) : (
          msg.content
        )}
      </div>
      <span className="text-[10px] text-gray-400 px-1">{fmtTime(msg.created_at)}</span>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function Conversations() {
  const { t } = useTranslation();
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const [showTakeover, setShowTakeover] = useState(false);
  const [actioning, setActioning] = useState(false);

  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);

  const [mobileView, setMobileView] = useState<"list" | "chat">("list");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    getConversations(200).then((data) => {
      setConversations(data);
      setListLoading(false);
    });
  }, []);

  async function selectConversation(id: number) {
    setSelectedId(id);
    setMobileView("chat");
    setDetailLoading(true);
    setDetail(null);
    try {
      const d = await getConversation(id);
      setDetail(d);
    } finally {
      setDetailLoading(false);
    }
  }

  useEffect(() => {
    if (detail) {
      setTimeout(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }), 50);
    }
  }, [detail?.messages?.length]);

  const filtered = conversations.filter((c) => {
    if (search && !c.phone_number.toLowerCase().includes(search.toLowerCase())) return false;
    if (filter === "whatsapp") return c.channel === "whatsapp";
    if (filter === "instagram") return c.channel === "instagram";
    if (filter === "hot") return c.lead_status === "hot";
    if (filter === "warm") return c.lead_status === "warm";
    if (filter === "cold") return c.lead_status === "cold";
    if (filter === "takeover") return c.ai_enabled === false;
    return true;
  });

  async function handleTakeover(note: string) {
    if (!selectedId) return;
    setActioning(true);
    setShowTakeover(false);
    try {
      const updated = await takeoverConversation(selectedId, note);
      setDetail(updated);
      setConversations((prev) => prev.map((c) => (c.id === selectedId ? { ...c, ai_enabled: false } : c)));
    } finally {
      setActioning(false);
    }
  }

  async function handleResume() {
    if (!selectedId) return;
    setActioning(true);
    try {
      const updated = await resumeConversation(selectedId);
      setDetail(updated);
      setConversations((prev) => prev.map((c) => (c.id === selectedId ? { ...c, ai_enabled: true } : c)));
    } finally {
      setActioning(false);
    }
  }

  async function handleLeadChange(newStatus: string) {
    if (!detail) return;
    setDetail((d) => (d ? { ...d, lead_status: newStatus } : d));
    setConversations((prev) =>
      prev.map((c) => (c.id === detail.id ? { ...c, lead_status: newStatus as "hot" | "warm" | "cold" } : c))
    );
  }

  async function handleSend(e: React.FormEvent) {
    e.preventDefault();
    if (!draft.trim() || !selectedId || !detail) return;
    setSending(true);
    const text = draft.trim();
    setDraft("");
    try {
      const msg = await sendHumanMessage(selectedId, text);
      setDetail((d) =>
        d ? { ...d, messages: [...d.messages, msg], message_count: d.message_count + 1 } : d
      );
    } finally {
      setSending(false);
    }
  }

  const aiEnabled = detail?.ai_enabled !== false;

  return (
    <Layout>
      {/* Mobile back button */}
      {mobileView === "chat" && (
        <div className="flex items-center gap-2 mb-3 lg:hidden">
          <button
            onClick={() => setMobileView("list")}
            className="flex items-center gap-1 text-sm text-indigo-600 font-medium"
          >
            <ChevronLeft size={16} /> Back
          </button>
        </div>
      )}

      <div className="flex gap-4" style={{ height: "calc(100vh - 5rem)" }}>
        {/* LEFT PANEL */}
        <div
          className={`flex flex-col bg-white rounded-xl border border-gray-100 shadow-sm overflow-hidden
            ${mobileView === "chat" ? "hidden lg:flex" : "flex"}
            w-full lg:w-[380px] xl:w-96 shrink-0`}
        >
          {/* Search */}
          <div className="p-3 border-b border-gray-50">
            <div className="relative">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
              <input
                type="text"
                placeholder={t("conversations.search")}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="w-full pl-8 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-400 bg-gray-50"
              />
            </div>
          </div>

          {/* Filter chips */}
          <div className="flex gap-1.5 px-3 py-2.5 border-b border-gray-50 overflow-x-auto scrollbar-hide shrink-0">
            {FILTER_TABS.map((tab) => (
              <button
                key={tab.key}
                onClick={() => setFilter(tab.key)}
                className={`shrink-0 text-xs px-3 py-1 rounded-full font-medium transition-all duration-150 ${
                  filter === tab.key
                    ? "bg-indigo-600 text-white shadow-sm"
                    : "border border-gray-200 text-gray-500 hover:border-indigo-300 hover:text-indigo-600"
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {/* List */}
          <div className="flex-1 overflow-y-auto">
            {listLoading ? (
              <div className="space-y-1 p-3">
                {[...Array(5)].map((_, i) => (
                  <div key={i} className="h-16 bg-gray-100 rounded-lg animate-pulse" />
                ))}
              </div>
            ) : filtered.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-12 text-center">
                <MessageSquare size={32} className="text-gray-200 mb-2" />
                <p className="text-sm text-gray-400">No conversations found.</p>
              </div>
            ) : (
              filtered.map((conv) => (
                <ConvItem
                  key={conv.id}
                  conv={conv}
                  selected={conv.id === selectedId}
                  onClick={() => selectConversation(conv.id)}
                />
              ))
            )}
          </div>
        </div>

        {/* RIGHT PANEL */}
        <div
          className={`flex-1 flex flex-col bg-white rounded-xl border border-gray-100 shadow-sm overflow-hidden min-w-0
            ${mobileView === "list" ? "hidden lg:flex" : "flex"}`}
        >
          {!selectedId ? (
            <div className="flex-1 flex flex-col items-center justify-center text-center p-8">
              <div className="w-16 h-16 bg-gray-100 rounded-2xl flex items-center justify-center mb-4">
                <MessageSquare size={28} className="text-gray-300" />
              </div>
              <p className="text-gray-500 font-medium">{t("conversations.no_conversation")}</p>
              <p className="text-sm text-gray-300 mt-1">Select a conversation from the left to view the thread</p>
            </div>
          ) : detailLoading ? (
            <div className="flex-1 flex items-center justify-center">
              <div className="w-8 h-8 border-3 border-indigo-200 border-t-indigo-600 rounded-full animate-spin" />
            </div>
          ) : detail ? (
            <>
              {/* Top bar */}
              <div className="px-5 py-3.5 border-b border-gray-100 flex items-center gap-3 flex-wrap bg-white">
                <div className="flex items-center gap-3 flex-1 min-w-0">
                  <div
                    className={`w-9 h-9 rounded-full ${AVATAR_COLORS[detail.lead_status] ?? "bg-gray-400"} flex items-center justify-center text-white text-xs font-bold shrink-0`}
                  >
                    {initials(detail.phone_number)}
                  </div>
                  <div className="min-w-0">
                    <div className="font-semibold text-gray-900 text-sm truncate">{detail.phone_number}</div>
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span
                        className={`text-xs font-medium px-2 py-0.5 rounded-full capitalize ${
                          detail.channel === "whatsapp"
                            ? "bg-green-100 text-green-700"
                            : detail.channel === "instagram"
                            ? "bg-purple-100 text-purple-700"
                            : "bg-blue-100 text-blue-700"
                        }`}
                      >
                        {detail.channel}
                      </span>
                      <span className="text-xs text-gray-400">{detail.message_count} msgs</span>
                      {detail.current_stage && (
                        <span
                          className={`text-xs font-semibold px-2 py-0.5 rounded-full capitalize ${
                            STAGE_BADGE_COLORS[detail.current_stage] ?? "bg-gray-100 text-gray-500"
                          }`}
                        >
                          {FUNNEL_STAGES.find((s) => s.key === detail.current_stage)?.label ?? detail.current_stage}
                        </span>
                      )}
                    </div>
                    {/* Funnel progress bar */}
                    {detail.current_stage && detail.current_stage !== "off_topic" && (
                      <div className="flex items-center gap-0.5 mt-1.5">
                        {FUNNEL_STAGES.map((s) => {
                          const stageIdx = FUNNEL_STAGES.findIndex((f) => f.key === detail.current_stage);
                          const thisIdx = FUNNEL_STAGES.findIndex((f) => f.key === s.key);
                          const active = thisIdx === stageIdx;
                          const done = thisIdx < stageIdx;
                          return (
                            <div
                              key={s.key}
                              title={s.label}
                              className={`h-1.5 flex-1 rounded-full transition-all ${
                                active ? s.color : done ? "bg-green-300" : "bg-gray-200"
                              }`}
                            />
                          );
                        })}
                      </div>
                    )}
                  </div>
                </div>

                <select
                  value={detail.lead_status}
                  onChange={(e) => handleLeadChange(e.target.value)}
                  className={`text-xs font-medium px-2.5 py-1.5 rounded-full border-0 capitalize cursor-pointer focus:outline-none focus:ring-2 focus:ring-indigo-400 ${
                    LEAD_COLORS[detail.lead_status] ?? LEAD_COLORS.cold
                  }`}
                >
                  <option value="hot">🔥 Hot</option>
                  <option value="warm">🌤 Warm</option>
                  <option value="cold">❄️ Cold</option>
                </select>

                {aiEnabled ? (
                  <button
                    onClick={() => setShowTakeover(true)}
                    disabled={actioning}
                    className="flex items-center gap-1.5 text-xs font-semibold px-3.5 py-2 rounded-lg bg-amber-500 text-white hover:bg-amber-600 disabled:opacity-50 transition-colors"
                  >
                    👤 {t("conversations.takeover")}
                  </button>
                ) : (
                  <button
                    onClick={handleResume}
                    disabled={actioning}
                    className="flex items-center gap-1.5 text-xs font-semibold px-3.5 py-2 rounded-lg bg-green-500 text-white hover:bg-green-600 disabled:opacity-50 transition-colors"
                  >
                    🤖 {t("conversations.resume_ai")}
                  </button>
                )}
              </div>

              {/* AI paused banner */}
              {!aiEnabled && (
                <div className="bg-amber-50 border-b border-amber-200 px-5 py-2.5 flex items-center gap-2">
                  <AlertTriangle size={16} className="text-amber-600 shrink-0" />
                  <div className="flex-1 min-w-0">
                    <span className="text-sm font-semibold text-amber-800">{t("conversations.ai_paused")}</span>
                    <span className="text-xs text-amber-600 ml-1.5">Reply directly on WhatsApp. Messages are saved here.</span>
                  </div>
                  {detail.taken_over_note && (
                    <span className="text-xs text-amber-500 italic truncate max-w-[160px]">
                      "{detail.taken_over_note}"
                    </span>
                  )}
                </div>
              )}

              {/* Messages */}
              <div className="flex-1 overflow-y-auto p-5 space-y-3 bg-gray-50">
                {detail.messages.length === 0 ? (
                  <p className="text-sm text-gray-400 text-center py-8">No messages yet.</p>
                ) : (
                  detail.messages.map((msg) => <ChatBubble key={msg.id} msg={msg} />)
                )}
                <div ref={bottomRef} />
              </div>

              {/* Human message input */}
              {!aiEnabled && (
                <form onSubmit={handleSend} className="border-t border-gray-100 bg-white px-4 py-3 flex items-end gap-2">
                  <textarea
                    rows={1}
                    value={draft}
                    onChange={(e) => {
                      setDraft(e.target.value);
                      e.target.style.height = "auto";
                      e.target.style.height = Math.min(e.target.scrollHeight, 96) + "px";
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        handleSend(e);
                      }
                    }}
                    placeholder={t("conversations.type_message")}
                    className="flex-1 border border-gray-200 rounded-xl px-3.5 py-2.5 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-indigo-400 bg-gray-50"
                    style={{ minHeight: "42px" }}
                  />
                  <button
                    type="submit"
                    disabled={!draft.trim() || sending}
                    className="w-11 h-11 rounded-xl bg-indigo-600 text-white flex items-center justify-center hover:bg-indigo-700 disabled:opacity-40 transition-colors shrink-0"
                  >
                    {sending ? (
                      <span className="w-4 h-4 border-2 border-white/40 border-t-white rounded-full animate-spin" />
                    ) : (
                      <Send size={16} />
                    )}
                  </button>
                </form>
              )}
            </>
          ) : null}
        </div>
      </div>

      {showTakeover && (
        <TakeoverModal onConfirm={handleTakeover} onCancel={() => setShowTakeover(false)} />
      )}
    </Layout>
  );
}
