import { AlertTriangle, MessageSquare, Pause, RefreshCw, Search } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { InboxFilter, InboxItem } from "../../types/chat";
import { relativeTime } from "../../utils/time";
import ChannelIcon from "./ChannelIcon";

interface Props {
  items: InboxItem[];
  loading: boolean;
  error: boolean;
  selectedId: number | null;
  filter: InboxFilter;
  search: string;
  onFilter: (f: InboxFilter) => void;
  onSearch: (s: string) => void;
  onSelect: (id: number) => void;
  onRetry: () => void;
}

const FILTERS: { key: InboxFilter; i18n: string }[] = [
  { key: "all", i18n: "chat.filter_all" },
  { key: "unread", i18n: "chat.filter_unread" },
  { key: "payment_to_verify", i18n: "chat.filter_payment" },
  { key: "bot_paused", i18n: "chat.filter_paused" },
];

function displayName(c: InboxItem): string {
  return c.customer_name?.trim() || c.phone_number;
}

function initials(c: InboxItem): string {
  const n = c.customer_name?.trim();
  if (n) return n.split(/\s+/).slice(0, 2).map((p) => p[0]?.toUpperCase()).join("");
  return c.phone_number.replace(/\D/g, "").slice(-2) || "?";
}

function preview(c: InboxItem, t: (k: string) => string): string {
  const m = c.last_message;
  if (!m) return t("chat.no_messages");
  const who = m.direction === "inbound" ? "" : m.sender_type === "human" ? `${t("chat.you")}: ` : `${t("chat.ai")}: `;
  if (m.media_type === "image" && (!m.text || m.text === "[image]")) return `${who}📷 ${t("chat.photo")}`;
  if (m.media_type === "audio") return `${who}🎤 ${t("chat.voice_note")}`;
  return `${who}${m.text}`;
}

/** Left column of the inbox: search, filter chips, and the conversation rows. */
export default function ConversationList({
  items, loading, error, selectedId, filter, search, onFilter, onSearch, onSelect, onRetry,
}: Props) {
  const { t } = useTranslation();

  return (
    <div className="flex flex-col h-full bg-white">
      <div className="p-3 border-b border-gray-100">
        <div className="relative">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            type="search"
            value={search}
            onChange={(e) => onSearch(e.target.value)}
            placeholder={t("chat.search_placeholder")}
            aria-label={t("chat.search_placeholder")}
            className="w-full pl-8 pr-3 py-2 text-sm border border-gray-200 rounded-lg bg-brand-bg focus:outline-none focus:ring-2 focus:ring-brand-primary"
          />
        </div>
      </div>

      <div className="flex gap-1.5 px-3 py-2.5 border-b border-gray-100 overflow-x-auto scrollbar-hide shrink-0" role="tablist">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            role="tab"
            aria-selected={filter === f.key}
            onClick={() => onFilter(f.key)}
            className={`shrink-0 text-xs px-3 py-1.5 rounded-full font-medium transition-colors ${
              filter === f.key
                ? "bg-brand-secondary text-white"
                : "border border-gray-200 text-gray-600 hover:border-brand-secondary/40"
            }`}
          >
            {t(f.i18n)}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="space-y-1 p-3" aria-busy="true">
            {[...Array(6)].map((_, i) => (
              <div key={i} className="h-16 bg-gray-100 rounded-lg animate-pulse" />
            ))}
          </div>
        ) : error ? (
          <div className="flex flex-col items-center text-center py-12 px-6 gap-2">
            <AlertTriangle size={28} className="text-red-400" />
            <p className="text-sm text-gray-600">{t("chat.list_error")}</p>
            <button onClick={onRetry} className="flex items-center gap-1.5 text-sm font-medium text-brand-primaryDark hover:underline">
              <RefreshCw size={14} /> {t("chat.retry")}
            </button>
          </div>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center text-center py-12 px-6">
            <MessageSquare size={32} className="text-gray-200 mb-2" />
            <p className="text-sm text-gray-400">{search || filter !== "all" ? t("chat.no_results") : t("chat.empty_list")}</p>
          </div>
        ) : (
          items.map((c) => {
            const unread = c.unread_count > 0;
            return (
              <button
                key={c.id}
                onClick={() => onSelect(c.id)}
                aria-current={c.id === selectedId}
                className={`w-full text-left px-4 py-3 border-b border-gray-50 transition-colors ${
                  c.id === selectedId ? "bg-brand-primary/10" : "hover:bg-brand-bg"
                }`}
              >
                <div className="flex items-start gap-3">
                  <div className="relative shrink-0">
                    <div className="w-10 h-10 rounded-full bg-brand-secondary/90 text-white flex items-center justify-center text-xs font-bold">
                      {initials(c)}
                    </div>
                    <span className="absolute -bottom-1 -right-1 bg-white rounded-full p-0.5 shadow">
                      <ChannelIcon channel={c.channel} size={13} />
                    </span>
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between gap-2">
                      <span className={`text-sm truncate ${unread ? "font-bold text-brand-secondary" : "font-semibold text-gray-800"}`}>
                        {displayName(c)}
                      </span>
                      <span className={`text-[11px] shrink-0 ${unread ? "text-brand-primaryDark font-semibold" : "text-gray-400"}`}>
                        {relativeTime(c.last_message?.created_at ?? c.updated_at)}
                      </span>
                    </div>
                    <div className="flex items-center justify-between gap-2 mt-0.5">
                      <p className={`text-xs truncate ${unread ? "text-gray-800" : "text-gray-500"}`}>{preview(c, t)}</p>
                      {unread && (
                        <span
                          className="shrink-0 min-w-[20px] h-5 px-1.5 rounded-full bg-brand-primary text-white text-[11px] font-bold flex items-center justify-center"
                          aria-label={t("chat.unread_count", { n: c.unread_count })}
                        >
                          {c.unread_count > 99 ? "99+" : c.unread_count}
                        </span>
                      )}
                    </div>
                    {(c.payment_to_verify || c.bot_paused) && (
                      <div className="flex gap-1.5 mt-1.5">
                        {c.payment_to_verify && (
                          <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-brand-warning/20 text-amber-800">
                            {t("chat.payment_to_verify")}
                          </span>
                        )}
                        {c.bot_paused && (
                          <span className="flex items-center gap-0.5 text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-gray-100 text-gray-600">
                            <Pause size={9} /> {t("chat.ai_paused_short")}
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
