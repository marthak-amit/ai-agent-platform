import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle, CheckCircle2, FlaskConical, Loader2, X } from "lucide-react";
import { NO_RETRY_KINDS, type RazorpayCheckout } from "../../hooks/useRazorpayCheckout";
import { formatDate, formatPaise } from "../../utils/billing";

function Modal({
  title,
  onClose,
  children,
  labelledBy,
}: {
  title?: string;
  /** Omit for modals that must not be dismissed (a payment is being confirmed). */
  onClose?: () => void;
  children: React.ReactNode;
  labelledBy: string;
}) {
  useEffect(() => {
    if (!onClose) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose?.();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} aria-hidden="true" />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        className="relative w-full max-w-md bg-white rounded-2xl shadow-xl p-6"
      >
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="absolute top-3 right-3 text-gray-400 hover:text-gray-600"
          >
            <X size={18} />
          </button>
        )}
        {title && (
          <h2 id={labelledBy} className="sr-only">
            {title}
          </h2>
        )}
        {children}
      </div>
    </div>
  );
}

/** Every dialog of the purchase flow: TEST MODE, "confirming payment", success and error (with retry). */
export default function CheckoutModals({ checkout }: { checkout: RazorpayCheckout }) {
  const { t, i18n } = useTranslation();
  const { phase, mockOrder, mockBusy, error, success } = checkout;

  if (mockOrder && (phase === "mock" || mockBusy)) {
    return (
      <Modal title={t("billing.test_mode")} onClose={mockBusy ? undefined : checkout.mockCancel} labelledBy="checkout-mock-title">
        <div className="flex flex-col gap-4">
          <div className="flex items-center gap-2">
            <span className="inline-flex items-center gap-1.5 rounded-full bg-brand-warning/20 text-amber-800 text-xs font-bold px-2.5 py-1 tracking-wide">
              <FlaskConical size={13} />
              {t("billing.test_mode")}
            </span>
          </div>
          <div>
            <h3 className="text-lg font-bold text-brand-secondary">{mockOrder.description}</h3>
            <p className="text-sm text-gray-500 mt-1">{t("billing.test_mode_desc")}</p>
          </div>
          <div className="rounded-xl bg-brand-bg border border-gray-100 p-4 flex items-center justify-between">
            <span className="text-sm text-gray-500">{t("billing.pay_now")}</span>
            <span className="text-xl font-bold text-brand-secondary">{formatPaise(mockOrder.amount)}</span>
          </div>
          <div className="flex gap-3">
            <button
              type="button"
              onClick={() => void checkout.mockPay()}
              disabled={mockBusy}
              className="flex-1 inline-flex items-center justify-center gap-2 rounded-lg bg-brand-primary hover:bg-brand-primaryDark disabled:opacity-60 text-white font-semibold text-sm py-2.5 transition-colors"
            >
              {mockBusy && <Loader2 size={15} className="animate-spin" />}
              {t("billing.test_pay")}
            </button>
            <button
              type="button"
              onClick={checkout.mockFail}
              disabled={mockBusy}
              className="flex-1 rounded-lg border border-brand-accent text-brand-accent hover:bg-brand-accent/10 disabled:opacity-60 font-semibold text-sm py-2.5 transition-colors"
            >
              {t("billing.test_fail")}
            </button>
          </div>
        </div>
      </Modal>
    );
  }

  if (phase === "verifying" || phase === "confirming") {
    return (
      <Modal title={t("billing.confirming_title")} labelledBy="checkout-confirming-title">
        <div className="flex flex-col items-center text-center gap-3 py-4" role="status" aria-live="polite">
          <Loader2 size={32} className="animate-spin text-brand-primaryDark" />
          <p className="font-semibold text-brand-secondary">{t("billing.confirming_title")}</p>
          <p className="text-sm text-gray-500">
            {phase === "confirming" ? t("billing.confirming_slow") : t("billing.confirming_desc")}
          </p>
        </div>
      </Modal>
    );
  }

  if (phase === "success" && success) {
    const sub = success.state.subscription;
    const queued = success.state.queued.find((q) => q.plan_code === success.planCode);
    const planName = sub?.plan.name ?? success.planCode;
    return (
      <Modal title={t("billing.success_title")} onClose={checkout.dismissSuccess} labelledBy="checkout-success-title">
        <div className="flex flex-col items-center text-center gap-3 py-2">
          <CheckCircle2 size={48} className="text-brand-primary" />
          <p className="text-xl font-bold text-brand-secondary">{t("billing.success_title")}</p>
          <p className="text-sm text-gray-600">
            {success.purpose === "renewal" && queued
              ? t("billing.success_renewal", {
                  plan: queued.plan_name,
                  start: formatDate(queued.current_period_start, i18n.language),
                })
              : t("billing.success_active", {
                  plan: planName,
                  end: formatDate(sub?.current_period_end, i18n.language),
                })}
          </p>
          <button
            type="button"
            onClick={checkout.dismissSuccess}
            className="mt-2 w-full rounded-lg bg-brand-primary hover:bg-brand-primaryDark text-white font-semibold text-sm py-2.5 transition-colors"
          >
            {t("billing.done")}
          </button>
        </div>
      </Modal>
    );
  }

  if (phase === "error" && error) {
    const noRetry = NO_RETRY_KINDS.includes(error.kind);
    const canRetry = !noRetry && error.kind !== "forbidden" && error.kind !== "downgrade";
    return (
      <Modal title={t("billing.error_title")} onClose={checkout.dismissError} labelledBy="checkout-error-title">
        <div className="flex flex-col items-center text-center gap-3 py-2">
          <AlertTriangle size={44} className="text-brand-warning" />
          <p className="text-lg font-bold text-brand-secondary">{t(`billing.err_${error.kind}_title`)}</p>
          <p className="text-sm text-gray-600">{t(`billing.err_${error.kind}`)}</p>
          {error.detail && <p className="text-xs text-gray-400">{error.detail}</p>}
          <div className="flex gap-3 w-full mt-2">
            {canRetry && (
              <button
                type="button"
                onClick={checkout.retry}
                className="flex-1 rounded-lg bg-brand-primary hover:bg-brand-primaryDark text-white font-semibold text-sm py-2.5 transition-colors"
              >
                {t("billing.try_again")}
              </button>
            )}
            {noRetry && (
              <button
                type="button"
                onClick={() => void checkout.checkStatus()}
                className="flex-1 rounded-lg bg-brand-primary hover:bg-brand-primaryDark text-white font-semibold text-sm py-2.5 transition-colors"
              >
                {t("billing.check_status")}
              </button>
            )}
            <button
              type="button"
              onClick={checkout.dismissError}
              className="flex-1 rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50 font-semibold text-sm py-2.5 transition-colors"
            >
              {t("billing.close")}
            </button>
          </div>
        </div>
      </Modal>
    );
  }

  return null;
}
