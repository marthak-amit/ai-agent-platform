import { useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Check, ChevronLeft, ChevronRight, Clock, MessageCircle, X } from "lucide-react";
import { mediaSrc } from "../../api/chat";
import type { PaymentRow } from "../../types/chat";
import { rupees, waitingLabel } from "../../utils/time";
import ChannelIcon from "../chat/ChannelIcon";

const OVERDUE_SECONDS = 2 * 3600;

interface Props {
  row: PaymentRow;
  active: boolean;
  now: number;
  onActivate: () => void;
  onOpenImages: (urls: string[], index: number) => void;
  onApprove: () => void;
  onReject: () => void;
}

/** One order awaiting verification: screenshot, expected amount, customer, waiting time and actions. */
export default function PaymentCard({ row, active, now, onActivate, onOpenImages, onApprove, onReject }: Props) {
  const { t } = useTranslation();
  const [idx, setIdx] = useState(0);
  const urls = row.proofs.map((p) => mediaSrc(p.media_url));
  const since = row.waiting_since ? new Date(row.waiting_since).getTime() : now;
  const waitingSeconds = Math.max(0, Math.round((now - since) / 1000));
  const overdue = waitingSeconds > OVERDUE_SECONDS;

  const step = (d: number) => setIdx((i) => (i + d + urls.length) % urls.length);

  return (
    <article
      onClick={onActivate}
      className={`bg-white rounded-2xl border shadow-sm overflow-hidden flex flex-col transition-shadow ${
        active ? "border-brand-primary ring-2 ring-brand-primary/40 shadow-md" : "border-gray-100"
      }`}
      aria-label={t("payments.order") + " " + row.order_number}
      data-testid="payment-card"
    >
      {/* screenshot (carousel when the customer sent several) */}
      <div className="relative bg-brand-bg h-56">
        {urls.length > 0 ? (
          <button
            onClick={(e) => { e.stopPropagation(); onOpenImages(urls, idx); }}
            className="block w-full h-full"
            aria-label={t("chat.open_image")}
          >
            <img src={urls[idx]} alt={t("payments.order") + " " + row.order_number} className="w-full h-full object-contain cursor-zoom-in" loading="lazy" />
          </button>
        ) : (
          <div className="w-full h-full flex items-center justify-center text-sm text-gray-400">—</div>
        )}
        {urls.length > 1 && (
          <>
            <button
              onClick={(e) => { e.stopPropagation(); step(-1); }}
              className="absolute left-2 top-1/2 -translate-y-1/2 p-1.5 rounded-full bg-black/50 text-white hover:bg-black/70"
              aria-label={t("chat.previous")}
            >
              <ChevronLeft size={16} />
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); step(1); }}
              className="absolute right-2 top-1/2 -translate-y-1/2 p-1.5 rounded-full bg-black/50 text-white hover:bg-black/70"
              aria-label={t("chat.next")}
            >
              <ChevronRight size={16} />
            </button>
            <span className="absolute bottom-2 left-1/2 -translate-x-1/2 text-[11px] text-white bg-black/60 rounded-full px-2.5 py-0.5">
              {t("payments.proof_of", { i: idx + 1, n: urls.length })}
            </span>
          </>
        )}
      </div>

      <div className="p-4 flex-1 flex flex-col gap-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="text-[11px] uppercase tracking-wide text-gray-400">{t("payments.expected")}</div>
            <div className="text-3xl font-extrabold text-brand-secondary tabular-nums leading-tight">{rupees(row.amount_expected)}</div>
          </div>
          <div className="text-right">
            <div className="text-[11px] uppercase tracking-wide text-gray-400">{t("payments.order")}</div>
            <div className="font-semibold text-brand-secondary">#{row.order_number}</div>
          </div>
        </div>

        <div className="flex items-center gap-2 text-sm">
          <ChannelIcon channel={row.channel} size={16} />
          <span className="font-medium text-gray-800 truncate">{row.customer_name}</span>
          <span className="text-gray-400">·</span>
          <span className="text-gray-500 truncate">{row.customer_phone}</span>
        </div>

        <div className="flex items-center justify-between gap-2 text-xs">
          <span className={`flex items-center gap-1 font-semibold ${overdue ? "text-red-600" : "text-gray-500"}`} title={t("payments.waiting")}>
            <Clock size={13} /> {waitingLabel(waitingSeconds, t)}
          </span>
          {row.seller_upi_id && (
            <span className="text-gray-400 truncate" title={t("payments.seller_upi")}>
              {t("payments.seller_upi")}: <span className="font-mono">{row.seller_upi_id}</span>
            </span>
          )}
        </div>

        <div className="mt-auto grid grid-cols-2 gap-2 pt-1">
          <button
            onClick={(e) => { e.stopPropagation(); onApprove(); }}
            className="flex items-center justify-center gap-1.5 bg-brand-primary text-white font-semibold py-2.5 rounded-xl hover:bg-brand-primaryDark transition-colors"
          >
            <Check size={16} /> {t("payments.approve")} ✅
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); onReject(); }}
            className="flex items-center justify-center gap-1.5 bg-white text-red-600 border border-red-200 font-semibold py-2.5 rounded-xl hover:bg-red-50 transition-colors"
          >
            <X size={16} /> {t("payments.reject")} ✖
          </button>
          {row.conversation_id && (
            <Link
              to={`/conversations?id=${row.conversation_id}`}
              onClick={(e) => e.stopPropagation()}
              className="col-span-2 flex items-center justify-center gap-1.5 py-2 border border-gray-200 text-gray-700 text-sm font-medium rounded-xl hover:bg-brand-bg transition-colors"
            >
              <MessageCircle size={15} /> {t("payments.open_chat")} 💬
            </Link>
          )}
        </div>
      </div>
    </article>
  );
}
