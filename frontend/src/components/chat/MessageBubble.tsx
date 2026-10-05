import { Check, RotateCcw, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { mediaSrc } from "../../api/chat";
import type { ChatMessage, OrderEvent, ProofCard } from "../../types/chat";
import { clockTime, dateTime, rupees } from "../../utils/time";

interface BubbleProps {
  msg: ChatMessage;
  onOpenImage: (url: string) => void;
  onRetry: (msg: ChatMessage) => void;
  onApprove: (proof: ProofCard) => void;
  onReject: (proof: ProofCard) => void;
  /** order id currently being approved/rejected (disables the buttons) */
  busyOrderId: number | null;
}

const PLACEHOLDER_TEXT = new Set(["[image]", "[voice note]"]);

/** Order lifecycle chip shown inline in the thread between messages. */
export function OrderChip({ event }: { event: OrderEvent }) {
  const { t } = useTranslation();
  const known = ["order_created", "pending_payment", "payment_submitted", "paid", "cancelled", "order_cancelled"];
  const key = known.includes(event.status) ? event.status : "other";
  const tone =
    event.status === "paid"
      ? "bg-emerald-50 text-emerald-800 border-emerald-200"
      : event.status === "payment_submitted"
      ? "bg-brand-warning/15 text-amber-900 border-brand-warning/40"
      : event.status === "cancelled" || event.status === "order_cancelled"
      ? "bg-red-50 text-red-700 border-red-200"
      : "bg-gray-100 text-gray-700 border-gray-200";
  return (
    <div className="flex justify-center my-1">
      <span className={`text-[11px] font-medium px-3 py-1 rounded-full border ${tone}`}>
        {t(`chat.order_event.${key}`, { order: event.order_number, status: event.status })} · {clockTime(event.at)}
      </span>
    </div>
  );
}

/** Review card shown under a payment screenshot: pending → Approve/Reject, reviewed → outcome. */
function ProofReviewCard({
  proof, onApprove, onReject, busy,
}: { proof: ProofCard; onApprove: () => void; onReject: () => void; busy: boolean }) {
  const { t } = useTranslation();
  return (
    <div className="mt-2 rounded-xl border border-brand-warning/50 bg-brand-warning/10 p-3 text-sm" data-testid="proof-card">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-[11px] uppercase tracking-wide text-amber-800/80">{t("chat.order")}</div>
          <div className="font-semibold text-brand-secondary">#{proof.order_number}</div>
        </div>
        <div className="text-right">
          <div className="text-[11px] uppercase tracking-wide text-amber-800/80">{t("chat.expected_amount")}</div>
          <div className="font-bold text-lg text-brand-secondary tabular-nums">{rupees(proof.amount_expected)}</div>
        </div>
      </div>

      {proof.status === "pending" ? (
        <div className="flex gap-2 mt-3">
          <button
            onClick={onApprove}
            disabled={busy}
            className="flex-1 flex items-center justify-center gap-1.5 bg-brand-primary text-white font-semibold py-2 rounded-lg hover:bg-brand-primaryDark disabled:opacity-50 transition-colors"
          >
            <Check size={15} /> {t("chat.approve")} ✅
          </button>
          <button
            onClick={onReject}
            disabled={busy}
            className="flex-1 flex items-center justify-center gap-1.5 bg-white text-red-600 border border-red-200 font-semibold py-2 rounded-lg hover:bg-red-50 disabled:opacity-50 transition-colors"
          >
            <X size={15} /> {t("chat.reject")} ✖
          </button>
        </div>
      ) : proof.status === "approved" ? (
        <p className="mt-2 text-emerald-700 font-medium">
          ✅ {t("chat.approved_by", { who: proof.reviewed_by ?? "—", at: dateTime(proof.reviewed_at) })}
        </p>
      ) : (
        <p className="mt-2 text-red-700 font-medium">
          ✖ {proof.reject_reason ? t("chat.rejected_reason", { reason: proof.reject_reason }) : t("chat.rejected")}
          {proof.reviewed_by && <span className="font-normal text-red-600/80"> · {proof.reviewed_by}, {dateTime(proof.reviewed_at)}</span>}
        </p>
      )}
    </div>
  );
}

/** One message row: left for the customer, right for AI/human, with images, proof cards and send state. */
export default function MessageBubble({ msg, onOpenImage, onRetry, onApprove, onReject, busyOrderId }: BubbleProps) {
  const { t } = useTranslation();
  const inbound = msg.direction === "inbound";
  const isHuman = msg.sender_type === "human";
  const isSystem = msg.sender_type === "system";

  const label = inbound
    ? msg.sender_name ?? t("chat.customer")
    : isHuman
    ? msg.sender_name ?? t("chat.you")
    : isSystem
    ? t("chat.auto")
    : t("chat.ai");

  const bubbleTone = inbound
    ? "bg-white border border-gray-200 text-gray-800 rounded-tl-sm"
    : isHuman
    ? "bg-brand-primaryDark text-white rounded-tr-sm"
    : "bg-brand-secondary text-white rounded-tr-sm";

  const hasProof = !!msg.payment_proof;
  const showText = msg.text && !PLACEHOLDER_TEXT.has(msg.text);
  const src = mediaSrc(msg.media_url);

  return (
    <div className={`flex flex-col gap-0.5 ${inbound ? "items-start" : "items-end"}`}>
      <span className="text-[10px] text-gray-400 px-1">{label}</span>
      <div
        className={`max-w-[85%] sm:max-w-[70%] rounded-2xl text-sm leading-relaxed shadow-sm overflow-hidden ${bubbleTone} ${
          hasProof ? "border-2 !border-brand-warning" : ""
        } ${msg.failed ? "ring-2 ring-red-400" : ""} ${msg.pending ? "opacity-60" : ""}`}
      >
        {msg.media_type === "image" && src && (
          <button onClick={() => onOpenImage(src)} className="block w-full" aria-label={t("chat.open_image")}>
            <img src={src} alt={t("chat.photo")} loading="lazy" className="block max-h-64 w-full object-cover cursor-zoom-in" />
          </button>
        )}
        {msg.media_type === "audio" && src && <audio controls src={src} className="max-w-full" />}
        {(showText || (!msg.media_url && msg.text)) && (
          <div className="px-3.5 py-2 whitespace-pre-wrap break-words">{msg.text}</div>
        )}
      </div>

      {msg.payment_proof && (
        <div className="w-full max-w-[85%] sm:max-w-[70%]">
          <ProofReviewCard
            proof={msg.payment_proof}
            busy={busyOrderId === msg.payment_proof.order_id}
            onApprove={() => onApprove(msg.payment_proof as ProofCard)}
            onReject={() => onReject(msg.payment_proof as ProofCard)}
          />
        </div>
      )}

      <div className="flex items-center gap-2 px-1 text-[10px] text-gray-400">
        {msg.pending ? (
          <span>{t("chat.sending")}</span>
        ) : msg.failed ? (
          <button
            onClick={() => onRetry(msg)}
            className="flex items-center gap-1 text-red-600 font-semibold hover:underline"
          >
            <RotateCcw size={11} /> {t("chat.failed_retry")}
          </button>
        ) : (
          <span>{clockTime(msg.created_at)}</span>
        )}
      </div>
    </div>
  );
}
