import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Loader2, Rocket, X } from "lucide-react";
import { goLive } from "../../api/client";
import { useAuth } from "../../context/AuthContext";
import { useBilling } from "../../context/BillingContext";
import { useSetup } from "../../context/SetupContext";
import { useToast } from "../../context/ToastContext";
import type { Checklist } from "../../types/setup";

interface Props {
  checklist: Checklist;
  onClose: () => void;
}

/**
 * Confirms "Go live". Says plainly what happens: the free trial STARTS NOW (setup and testing were never counted),
 * or the running plan takes over, or — when this WhatsApp number already had its trial — a plan must be chosen.
 * Remaining sample products are removed in the same step (customers must never be sold samples).
 */
export default function GoLiveModal({ checklist, onClose }: Props) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const { refreshProfile } = useAuth();
  const { refresh: refreshBilling } = useBilling();
  const { refresh: refreshSetup } = useSetup();
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const mode = checklist.go_live_mode;

  async function confirm() {
    setWorking(true);
    setError("");
    try {
      await goLive(checklist.demo_products > 0);
      await Promise.all([refreshProfile(), refreshSetup(), refreshBilling()]);
      toast.show(t("setup.golive.success"), { kind: "success" });
      onClose();
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: { code?: string } } } })?.response?.data?.detail;
      setError(
        detail?.code === "TRIAL_ALREADY_USED"
          ? t("setup.golive.err_trial_used")
          : detail?.code === "SETUP_INCOMPLETE"
            ? t("setup.golive.err_incomplete")
            : t("setup.golive.error"),
      );
      void refreshSetup();
    } finally {
      setWorking(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4" role="dialog" aria-modal="true" aria-label={t("setup.golive.title")}>
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md p-6 relative">
        <button type="button" onClick={onClose} aria-label={t("setup.golive.cancel")} className="absolute top-4 right-4 text-gray-400 hover:text-gray-600">
          <X size={18} />
        </button>
        <div className="flex items-center gap-3 mb-3">
          <span className="w-10 h-10 rounded-full bg-brand-primary/10 flex items-center justify-center">
            <Rocket size={20} className="text-brand-primaryDark" />
          </span>
          <h2 className="text-lg font-bold text-gray-900">{t("setup.golive.title")}</h2>
        </div>

        <p className="text-sm text-gray-600 leading-relaxed">
          {mode === "trial" && t("setup.golive.trial_text", { days: checklist.trial_days, limit: checklist.trial_conversation_limit })}
          {mode === "plan" && t("setup.golive.plan_text")}
          {mode === "trial_used" && t("setup.golive.trial_used_text")}
        </p>
        {checklist.demo_products > 0 && mode !== "trial_used" && (
          <p className="text-sm text-gray-600 mt-2">{t("setup.golive.samples_text", { count: checklist.demo_products })}</p>
        )}
        {error && <p role="alert" className="mt-3 text-sm text-red-600">{error}</p>}

        <div className="flex gap-2 mt-5">
          {mode === "trial_used" ? (
            <button
              type="button"
              onClick={() => navigate("/billing")}
              className="flex-1 bg-brand-primaryDark text-white font-semibold text-sm px-4 py-2.5 rounded-lg hover:opacity-90"
            >
              {t("setup.golive.choose_plan")}
            </button>
          ) : (
            <button
              type="button"
              onClick={confirm}
              disabled={working}
              className="flex-1 inline-flex items-center justify-center gap-2 bg-brand-primaryDark text-white font-semibold text-sm px-4 py-2.5 rounded-lg hover:opacity-90 disabled:opacity-60"
            >
              {working && <Loader2 size={15} className="animate-spin" />}
              {working ? t("setup.golive.working") : t("setup.golive.confirm")}
            </button>
          )}
          <button type="button" onClick={onClose} className="px-4 py-2.5 text-sm font-medium text-gray-600 rounded-lg hover:bg-gray-100">
            {t("setup.golive.cancel")}
          </button>
        </div>
      </div>
    </div>
  );
}
