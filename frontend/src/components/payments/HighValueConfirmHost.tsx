import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { registerHighValueHost, type HighValueRequest } from "../../utils/highValueConfirm";

/**
 * The dialog behind a 409 `high_value_confirmation_required`: "Verify ₹X in your bank before approving".
 * Mounted once; `approvePayment()` awaits the seller's choice and re-posts with `confirm_high_value: true` only on "verified".
 */
export default function HighValueConfirmHost() {
  const { t } = useTranslation();
  const [req, setReq] = useState<HighValueRequest | null>(null);
  const resolver = useRef<((ok: boolean) => void) | null>(null);

  useEffect(() => {
    registerHighValueHost((request, resolve) => {
      resolver.current?.(false); // a second request while one is open cancels the first
      resolver.current = resolve;
      setReq(request);
    });
    return () => registerHighValueHost(null);
  }, []);

  function close(ok: boolean) {
    resolver.current?.(ok);
    resolver.current = null;
    setReq(null);
  }

  useEffect(() => {
    if (!req) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [req]);

  if (!req) return null;
  const amount = req.total != null ? `₹${Math.round(req.total).toLocaleString("en-IN")}` : "";
  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center p-4" role="dialog" aria-modal="true">
      <div className="absolute inset-0 bg-black/50" onClick={() => close(false)} />
      <div className="relative bg-white rounded-2xl shadow-xl w-full max-w-sm p-5 z-10">
        <h2 className="text-base font-semibold text-brand-secondary">{t("payments.high_value_title", { amount })}</h2>
        <p className="text-sm text-gray-600 mt-2">{t("payments.high_value_body", { order: req.orderNumber, amount })}</p>
        <div className="flex gap-2 mt-4">
          <button
            type="button"
            onClick={() => close(true)}
            className="flex-1 bg-brand-primary text-white py-2.5 rounded-xl text-sm font-semibold hover:opacity-90 transition-opacity"
          >
            {t("payments.high_value_confirm")}
          </button>
          <button
            type="button"
            onClick={() => close(false)}
            className="px-4 py-2 border border-gray-200 rounded-xl text-sm text-gray-600 hover:bg-gray-50"
          >
            {t("payments.cancel")}
          </button>
        </div>
      </div>
    </div>
  );
}
