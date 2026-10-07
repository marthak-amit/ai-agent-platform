import { useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronLeft, ChevronRight, FileText, Loader2, Receipt } from "lucide-react";
import { downloadInvoicePdf } from "../../api/billing";
import { useToast } from "../../context/ToastContext";
import type { PaymentList } from "../../types/billing";
import { formatDate, formatPaise, paymentStatusTone, type StatusTone } from "../../utils/billing";

const TONE: Record<StatusTone, string> = {
  success: "bg-brand-primary/15 text-brand-primaryDark",
  danger: "bg-brand-accent/15 text-brand-accent",
  pending: "bg-brand-warning/20 text-amber-800",
  neutral: "bg-gray-100 text-gray-600",
};

interface Props {
  data: PaymentList | null;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
  onPage: (page: number) => void;
}

/** Payment history table (date, plan, amount, GST, status, Razorpay payment id) with loading/empty/error states. */
export default function PaymentHistory({ data, loading, error, onRetry, onPage }: Props) {
  const { t, i18n } = useTranslation();
  const toast = useToast();
  const [downloading, setDownloading] = useState<number | null>(null);
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  async function download(id: number, number: string) {
    if (downloading !== null) return;
    setDownloading(id);
    const ok = await downloadInvoicePdf(id, number);
    setDownloading(null);
    if (!ok) toast.show(t("billing.invoice_error"), { kind: "error" });
  }

  return (
    <section className="rounded-2xl bg-white border border-gray-200 overflow-hidden">
      <div className="px-6 py-4 border-b border-gray-100 flex items-center gap-2">
        <Receipt size={18} className="text-gray-400" />
        <h2 className="text-base font-bold text-brand-secondary">{t("billing.history_title")}</h2>
      </div>

      {!data && loading && (
        <div className="p-6 flex flex-col gap-3" aria-busy="true" aria-label={t("billing.loading")}>
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-9 rounded-lg bg-gray-100 animate-pulse" />
          ))}
        </div>
      )}

      {!data && !loading && error && (
        <div className="p-6 text-center">
          <p className="text-sm text-gray-600">{t("billing.history_error")}</p>
          <button type="button" onClick={onRetry} className="mt-3 text-sm font-semibold text-brand-primaryDark hover:underline">
            {t("billing.retry")}
          </button>
        </div>
      )}

      {data && data.items.length === 0 && (
        <div className="p-10 text-center">
          <p className="text-sm font-medium text-gray-700">{t("billing.history_empty")}</p>
          <p className="text-xs text-gray-400 mt-1">{t("billing.history_empty_hint")}</p>
        </div>
      )}

      {data && data.items.length > 0 && (
        <>
          <div className={`overflow-x-auto ${loading ? "opacity-60" : ""}`}>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wide text-gray-400 border-b border-gray-100">
                  <th className="px-6 py-3 font-medium">{t("billing.col_date")}</th>
                  <th className="px-3 py-3 font-medium">{t("billing.col_plan")}</th>
                  <th className="px-3 py-3 font-medium text-right">{t("billing.col_amount")}</th>
                  <th className="px-3 py-3 font-medium text-right">{t("billing.col_gst")}</th>
                  <th className="px-3 py-3 font-medium">{t("billing.col_status")}</th>
                  <th className="px-3 py-3 font-medium">{t("billing.col_payment_id")}</th>
                  <th className="px-6 py-3 font-medium">{t("billing.col_invoice")}</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((p) => (
                  <tr key={p.id} className="border-b border-gray-50 last:border-0">
                    <td className="px-6 py-3 whitespace-nowrap text-gray-600">
                      {formatDate(p.paid_at ?? p.created_at, i18n.language)}
                    </td>
                    <td className="px-3 py-3 whitespace-nowrap">
                      <span className="font-medium text-brand-secondary">{p.plan_name}</span>
                      <span className="ml-1.5 text-xs text-gray-400">{t(`billing.purpose_${p.purpose}`)}</span>
                    </td>
                    <td className="px-3 py-3 text-right font-medium text-brand-secondary whitespace-nowrap">
                      {formatPaise(p.amount_paise)}
                    </td>
                    <td className="px-3 py-3 text-right text-gray-500 whitespace-nowrap">{formatPaise(p.gst_paise)}</td>
                    <td className="px-3 py-3 whitespace-nowrap">
                      <span
                        title={p.failure_reason ?? undefined}
                        className={`inline-flex rounded-full px-2.5 py-0.5 text-xs font-semibold ${TONE[paymentStatusTone(p.status)]}`}
                      >
                        {t(`billing.pay_status_${p.status}`)}
                      </span>
                    </td>
                    <td className="px-3 py-3 font-mono text-xs text-gray-500 whitespace-nowrap">
                      {p.razorpay_payment_id ?? "—"}
                    </td>
                    <td className="px-6 py-3 whitespace-nowrap">
                      {p.invoice_id !== null && p.invoice_number ? (
                        <button
                          type="button"
                          onClick={() => void download(p.invoice_id as number, p.invoice_number as string)}
                          disabled={downloading !== null}
                          aria-label={t("billing.invoice_download", { number: p.invoice_number })}
                          className="inline-flex items-center gap-1.5 text-xs font-semibold text-brand-primaryDark hover:underline disabled:opacity-60"
                        >
                          {downloading === p.invoice_id ? <Loader2 size={13} className="animate-spin" /> : <FileText size={13} />}
                          {p.invoice_number}
                        </button>
                      ) : (
                        <span className="text-gray-300">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {pages > 1 && (
            <div className="flex items-center justify-between px-6 py-3 border-t border-gray-100 text-sm text-gray-500">
              <span>{t("billing.page_of", { page: data.page, pages })}</span>
              <div className="flex items-center gap-1">
                {loading && <Loader2 size={14} className="animate-spin mr-2" />}
                <button
                  type="button"
                  onClick={() => onPage(data.page - 1)}
                  disabled={data.page <= 1 || loading}
                  aria-label={t("billing.prev")}
                  className="p-1.5 rounded-lg border border-gray-200 hover:bg-gray-50 disabled:opacity-40"
                >
                  <ChevronLeft size={16} />
                </button>
                <button
                  type="button"
                  onClick={() => onPage(data.page + 1)}
                  disabled={data.page >= pages || loading}
                  aria-label={t("billing.next")}
                  className="p-1.5 rounded-lg border border-gray-200 hover:bg-gray-50 disabled:opacity-40"
                >
                  <ChevronRight size={16} />
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
