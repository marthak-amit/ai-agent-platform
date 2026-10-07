import { useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { AlertTriangle, XCircle, X } from "lucide-react";
import { useAuth } from "../../context/AuthContext";
import { useBilling } from "../../context/BillingContext";
import { deriveBanners, formatDate, type Banner } from "../../utils/billing";

const STORAGE_KEY = "billing_banners_dismissed";

function readDismissed(): string[] {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((k): k is string => typeof k === "string") : [];
  } catch {
    return [];
  }
}

function writeDismissed(keys: string[]) {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(keys));
  } catch {
    // storage unavailable (private mode) — the banner just comes back on the next page load
  }
}

/**
 * Global billing banners for the app layout: usage ≥ 80 %, over limit, expiring ≤ 3 days, grace period and expired.
 * Amber ones can be dismissed for the session; red ones (over limit, grace, expired) stay until resolved.
 * Renders nothing while loading, on error, or for billing-exempt accounts.
 */
export default function BillingBanners() {
  const { t, i18n } = useTranslation();
  const { client } = useAuth();
  const { state } = useBilling();
  const { pathname } = useLocation();
  const [dismissed, setDismissed] = useState<string[]>(readDismissed);

  const banners = useMemo(
    () => deriveBanners(state).filter((b) => b.severity === "critical" || !dismissed.includes(b.key)),
    [state, dismissed],
  );
  if (banners.length === 0) return null;

  const isOwner = !!client?.current_user.is_owner;
  const onBillingPage = pathname === "/billing";

  function dismiss(key: string) {
    const next = [...dismissed, key];
    setDismissed(next);
    writeDismissed(next);
  }

  function message(b: Banner): string {
    const lang = i18n.language;
    switch (b.kind) {
      case "usage_high":
        return t("billing.banner_usage_high", b.params);
      case "over_limit":
        return t("billing.banner_over_limit", b.params);
      case "expiring":
        return Number(b.params.days) === 0
          ? t("billing.banner_expiring_today", b.params)
          : t("billing.banner_expiring", { ...b.params, count: Number(b.params.days), end: formatDate(String(b.params.end), lang) });
      case "grace":
        return t("billing.banner_grace", { date: formatDate(String(b.params.until), lang) });
      case "expired":
        return t(b.params.restricted ? "billing.banner_expired_restricted" : "billing.banner_expired");
    }
  }

  function cta(b: Banner): string {
    return t(b.kind === "expiring" ? "billing.cta_renew" : b.kind === "grace" || b.kind === "expired" ? "billing.cta_choose_plan" : "billing.cta_upgrade");
  }

  return (
    <div className="shrink-0" role="region" aria-label={t("billing.title")}>
      {banners.map((b) => {
        const critical = b.severity === "critical";
        // Over-limit uses the coral accent (the assistant still works); grace/expired are the hard red.
        const hardRed = b.kind === "grace" || b.kind === "expired";
        const tone = hardRed
          ? "bg-red-600 text-white"
          : critical
            ? "bg-brand-accent/15 border-b border-brand-accent/40 text-gray-800"
            : "bg-brand-warning/20 border-b border-brand-warning/50 text-amber-900";
        const Icon = hardRed ? XCircle : AlertTriangle;
        return (
          <div key={b.key} role={critical ? "alert" : "status"} className={`px-4 py-2 flex items-center gap-2 text-sm ${tone}`}>
            <Icon size={16} className="shrink-0" />
            <span className="flex-1">{message(b)}</span>
            {isOwner && !onBillingPage && (
              <Link to="/billing" className="font-semibold underline whitespace-nowrap">
                {cta(b)}
              </Link>
            )}
            {!isOwner && <span className="text-xs opacity-80 whitespace-nowrap">{t("billing.ask_owner")}</span>}
            {!critical && (
              <button type="button" onClick={() => dismiss(b.key)} aria-label={t("billing.dismiss")} className="opacity-70 hover:opacity-100">
                <X size={14} />
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
