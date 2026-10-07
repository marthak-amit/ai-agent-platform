import { useTranslation } from "react-i18next";
import { CalendarClock, Info } from "lucide-react";
import type { SubscriptionState } from "../../types/billing";
import { formatDate } from "../../utils/billing";
import UsageBar from "./UsageBar";

type Tone = "green" | "amber" | "red" | "gray";
const BADGE: Record<Tone, string> = {
  green: "bg-brand-primary/15 text-brand-primaryDark",
  amber: "bg-brand-warning/20 text-amber-800",
  red: "bg-brand-accent/15 text-brand-accent",
  gray: "bg-gray-100 text-gray-600",
};

function Badge({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  return <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ${BADGE[tone]}`}>{children}</span>;
}

/** The "where am I now" card: plan, status badge, period, days left, usage bar and the over-limit reassurance. */
export default function CurrentPlanCard({ state }: { state: SubscriptionState }) {
  const { t, i18n } = useTranslation();
  const sub = state.subscription;
  const ent = state.entitlement;

  if (ent?.state === "exempt") {
    return (
      <section className="rounded-2xl bg-white border border-gray-200 p-6">
        <div className="flex items-center gap-3">
          <h2 className="text-lg font-bold text-brand-secondary">{t("billing.exempt_title")}</h2>
          <Badge tone="green">{t("billing.status_exempt")}</Badge>
        </div>
        <p className="text-sm text-gray-500 mt-1">{t("billing.exempt_desc")}</p>
      </section>
    );
  }

  if (!sub) {
    const lapsed = ent?.state === "grace" || ent?.state === "expired";
    return (
      <section className="rounded-2xl bg-white border border-gray-200 p-6">
        <div className="flex items-center gap-3">
          <h2 className="text-lg font-bold text-brand-secondary">{t("billing.no_plan_title")}</h2>
          <Badge tone={ent?.state === "expired" ? "red" : ent?.state === "grace" ? "amber" : "gray"}>
            {t(`billing.status_${ent?.state === "grace" || ent?.state === "expired" ? ent.state : "none"}`)}
          </Badge>
        </div>
        <p className="text-sm text-gray-500 mt-1">{lapsed ? t("billing.no_plan_lapsed") : t("billing.no_plan_desc")}</p>
        {ent?.state === "grace" && ent.grace_ends_at && (
          <p className="text-sm text-amber-800 mt-2">
            {t("billing.grace_until", { date: formatDate(ent.grace_ends_at, i18n.language) })}
          </p>
        )}
      </section>
    );
  }

  const overLimit = state.over_limit || sub.percent_used > 100;
  const expiringSoon = sub.days_left <= 3;
  const badge: { tone: Tone; label: string } =
    sub.status === "active"
      ? overLimit
        ? { tone: "red", label: t("billing.status_over_limit") }
        : expiringSoon
          ? { tone: "amber", label: t("billing.status_expiring") }
          : { tone: "green", label: t("billing.status_active") }
      : { tone: "gray", label: sub.status };

  return (
    <section className="rounded-2xl bg-white border border-gray-200 p-6 flex flex-col gap-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-3">
            <h2 className="text-xl font-bold text-brand-secondary">{sub.plan.name}</h2>
            <Badge tone={badge.tone}>{badge.label}</Badge>
          </div>
          <p className="flex items-center gap-1.5 text-sm text-gray-500 mt-1.5">
            <CalendarClock size={14} className="shrink-0" />
            {formatDate(sub.current_period_start, i18n.language)} – {formatDate(sub.current_period_end, i18n.language)}
          </p>
        </div>
        <div className="text-right">
          <div className="text-3xl font-bold text-brand-secondary leading-none">{sub.days_left}</div>
          <div className="text-xs text-gray-500 mt-1">{t("billing.days_left", { count: sub.days_left })}</div>
        </div>
      </div>

      <UsageBar used={sub.conversations_used} limit={sub.conversation_limit} percent={sub.percent_used} />

      {overLimit && (
        <div className="flex items-start gap-2 rounded-lg bg-brand-accent/10 border border-brand-accent/30 px-3 py-2.5 text-sm text-gray-700">
          <Info size={16} className="mt-0.5 shrink-0 text-brand-accent" />
          <span>{t("billing.over_limit_note")}</span>
        </div>
      )}

      {state.queued.length > 0 && (
        <ul className="text-sm text-gray-600 flex flex-col gap-1 border-t border-gray-100 pt-4">
          {state.queued.map((q) => (
            <li key={q.id}>
              {t("billing.queued_line", {
                plan: q.plan_name,
                start: formatDate(q.current_period_start, i18n.language),
                end: formatDate(q.current_period_end, i18n.language),
              })}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
