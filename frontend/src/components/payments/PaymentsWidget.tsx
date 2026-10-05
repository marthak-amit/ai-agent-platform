import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { CheckCircle2, ChevronRight, Wallet } from "lucide-react";
import { useAuth } from "../../context/AuthContext";
import { useRealtime } from "../../context/RealtimeContext";

/** Dashboard-home card: "X payments waiting verification" → /payments. Hidden for users who can't verify. */
export default function PaymentsWidget() {
  const { t } = useTranslation();
  const { client } = useAuth();
  const { pendingCount } = useRealtime();
  const user = client?.current_user;
  if (!user || !(user.is_owner || user.permissions.includes("payment_verify"))) return null;

  if (pendingCount === 0) {
    return (
      <div className="flex items-center gap-3 bg-white border border-gray-100 rounded-2xl px-5 py-3 shadow-sm">
        <CheckCircle2 size={20} className="text-brand-primary" />
        <span className="text-sm text-gray-600">{t("payments.widget_none")}</span>
      </div>
    );
  }
  return (
    <Link
      to="/payments"
      className="flex items-center gap-4 bg-brand-warning/15 border border-brand-warning/50 rounded-2xl px-5 py-4 shadow-sm hover:bg-brand-warning/25 transition-colors"
    >
      <span className="w-10 h-10 rounded-full bg-brand-warning text-brand-secondary flex items-center justify-center shrink-0">
        <Wallet size={20} />
      </span>
      <span className="flex-1 min-w-0">
        <span className="block font-semibold text-brand-secondary">
          {pendingCount === 1 ? t("payments.widget_waiting_one") : t("payments.widget_waiting_other", { n: pendingCount })}
        </span>
      </span>
      <span className="flex items-center gap-1 text-sm font-semibold text-brand-primaryDark whitespace-nowrap">
        {t("payments.widget_cta")} <ChevronRight size={16} />
      </span>
    </Link>
  );
}
