import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

interface Props {
  orderNumber: string;
  busy?: boolean;
  onConfirm: (reason: string) => void;
  onCancel: () => void;
}

const QUICK_REASONS = [
  { key: "amount_mismatch", i18n: "payments.reason_amount" },
  { key: "not_received", i18n: "payments.reason_not_received" },
  { key: "unclear", i18n: "payments.reason_unclear" },
] as const;

/** Small modal to reject a payment screenshot: quick-pick reasons + optional free text. */
export default function RejectModal({ orderNumber, busy, onConfirm, onCancel }: Props) {
  const { t } = useTranslation();
  const [reason, setReason] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center p-4" role="dialog" aria-modal="true">
      <div className="absolute inset-0 bg-black/50" onClick={onCancel} />
      <div className="relative bg-white rounded-2xl shadow-xl w-full max-w-sm p-5 z-10">
        <h2 className="text-base font-semibold text-brand-secondary">{t("payments.reject_title", { order: orderNumber })}</h2>
        <p className="text-xs text-gray-500 mt-1 mb-3">{t("payments.reject_hint")}</p>
        <div className="flex flex-wrap gap-2 mb-3">
          {QUICK_REASONS.map((r) => {
            const label = t(r.i18n);
            const active = reason === label;
            return (
              <button
                key={r.key}
                type="button"
                onClick={() => setReason(active ? "" : label)}
                className={`text-xs px-3 py-1.5 rounded-full border transition-colors ${
                  active
                    ? "bg-brand-secondary text-white border-brand-secondary"
                    : "border-gray-200 text-gray-600 hover:border-brand-secondary/40"
                }`}
              >
                {label}
              </button>
            );
          })}
        </div>
        <textarea
          ref={inputRef}
          rows={2}
          maxLength={300}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder={t("payments.reason_placeholder")}
          className="w-full border border-gray-200 rounded-xl px-3 py-2 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-brand-primary mb-4"
        />
        <div className="flex gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => onConfirm(reason.trim())}
            className="flex-1 bg-red-600 text-white py-2.5 rounded-xl text-sm font-semibold hover:bg-red-700 disabled:opacity-50 transition-colors"
          >
            {t("payments.reject_confirm")}
          </button>
          <button
            type="button"
            onClick={onCancel}
            className="px-4 py-2 border border-gray-200 rounded-xl text-sm text-gray-600 hover:bg-gray-50"
          >
            {t("payments.cancel")}
          </button>
        </div>
      </div>
    </div>
  );
}
