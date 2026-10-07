import { useTranslation } from "react-i18next";
import { Loader2 } from "lucide-react";
import ChannelIcon from "../chat/ChannelIcon";
import type { Plan } from "../../types/billing";
import { formatDate, formatPaise, perConversationPaise, type PlanAction } from "../../utils/billing";

/** The plan to highlight as "Most popular" (sold by the pricing page the same way). */
const POPULAR_PLAN_CODE = "growth_5000";

interface Props {
  plan: Plan;
  action: PlanAction;
  isCurrent: boolean;
  /** The plan the customer picked on the marketing site. */
  picked?: boolean;
  /** When the running period ends — used in the downgrade tooltip. */
  periodEnd?: string;
  /** Any checkout in flight: every button is disabled so a second order can't be created. */
  busy: boolean;
  /** This card's own checkout is the one in flight (shows the spinner). */
  loading: boolean;
  onSelect: (planCode: string) => void;
}

/** One sellable plan: price (+GST), per-conversation cost, features and the right call-to-action for the tenant. */
export default function PlanCard({ plan, action, isCurrent, picked = false, periodEnd, busy, loading, onSelect }: Props) {
  const { t, i18n } = useTranslation();
  const popular = plan.code === POPULAR_PLAN_CODE;
  const perConv = perConversationPaise(plan.base_paise, plan.conversation_limit);
  const blocked = action.kind === "downgrade_blocked";
  const disabled = busy || blocked;

  const label =
    action.kind === "subscribe"
      ? t("billing.btn_subscribe")
      : action.kind === "renew"
        ? t("billing.btn_renew")
        : action.kind === "upgrade"
          ? t("billing.btn_upgrade")
          : t("billing.btn_downgrade");

  return (
    <div
      className={`relative flex flex-col rounded-2xl bg-white p-6 border-2 ${
        isCurrent || picked ? "border-brand-primary" : popular ? "border-brand-primary/40" : "border-gray-200"
      } ${picked ? "ring-4 ring-brand-primary/20" : ""} flex-1`}
    >
      <div className="absolute -top-3 left-6 flex gap-2">
        {isCurrent && (
          <span className="rounded-full bg-brand-primaryDark text-white text-xs font-semibold px-3 py-1">
            {t("billing.current_plan")}
          </span>
        )}
        {picked && !isCurrent && (
          <span className="rounded-full bg-brand-primary text-white text-xs font-bold px-3 py-1">
            {t("billing.your_pick")}
          </span>
        )}
        {popular && !isCurrent && !picked && (
          <span className="rounded-full bg-brand-warning text-brand-secondary text-xs font-bold px-3 py-1">
            {t("billing.most_popular")}
          </span>
        )}
      </div>

      <h3 className="text-lg font-bold text-brand-secondary">{plan.name}</h3>
      <p className="text-sm text-gray-500 mt-0.5">
        {t("billing.conv_per_cycle", { limit: plan.conversation_limit.toLocaleString("en-IN"), days: plan.billing_period_days })}
      </p>

      <div className="mt-4">
        <div className="flex items-baseline gap-1.5">
          <span className="text-3xl font-bold text-brand-secondary">{formatPaise(plan.base_paise)}</span>
          <span className="text-sm text-gray-500">
            {plan.prices_include_gst ? t("billing.incl_gst") : t("billing.plus_gst", { pct: plan.gst_rate_bps / 100 })}
          </span>
        </div>
        {!plan.prices_include_gst && (
          <p className="text-xs text-gray-500 mt-1">
            {t("billing.total_with_gst", { amount: formatPaise(plan.total_paise) })}
          </p>
        )}
        <p className="text-sm font-medium text-brand-primaryDark mt-2">
          {t("billing.per_conversation", { price: formatPaise(perConv) })}
        </p>
      </div>

      <ul className="mt-4 flex flex-col gap-2 text-sm text-gray-700">
        <li className="flex items-center gap-2">
          <ChannelIcon channel="whatsapp" size={15} />
          {t("billing.feature_whatsapp")}
        </li>
        {plan.features.instagram ? (
          <li className="flex items-center gap-2">
            <ChannelIcon channel="instagram" size={15} />
            {t("billing.feature_instagram")}
            <span className="rounded-full bg-brand-accent/10 text-brand-accent text-[10px] font-bold px-1.5 py-0.5 uppercase tracking-wide">
              IG
            </span>
          </li>
        ) : (
          <li className="flex items-center gap-2 text-gray-400">
            <span className="opacity-40"><ChannelIcon channel="instagram" size={15} /></span>
            {t("billing.feature_no_instagram")}
          </li>
        )}
      </ul>

      <div className="mt-auto pt-5">
        {action.kind === "upgrade" && action.option && (
          <dl className="mb-3 rounded-lg bg-brand-bg border border-gray-100 px-3 py-2 text-xs flex flex-col gap-1">
            <div className="flex justify-between text-gray-500">
              <dt>{t("billing.upgrade_credit")}</dt>
              <dd className="font-medium text-brand-primaryDark">−{formatPaise(action.option.amounts.credit_paise)}</dd>
            </div>
            <div className="flex justify-between text-brand-secondary">
              <dt className="font-medium">{t("billing.upgrade_payable")}</dt>
              <dd className="font-bold">{formatPaise(action.option.amounts.total_paise)}</dd>
            </div>
          </dl>
        )}
        {action.kind === "renew" && <p className="mb-3 text-xs text-gray-500">{t("billing.renew_hint")}</p>}

        {/* The tooltip sits on the wrapper: browsers don't reliably show `title` on a disabled button. */}
        <div title={blocked ? t("billing.downgrade_tooltip", { date: formatDate(periodEnd, i18n.language) }) : undefined}>
          <button
            type="button"
            onClick={() => onSelect(plan.code)}
            disabled={disabled}
            className={`w-full inline-flex items-center justify-center gap-2 rounded-lg py-2.5 text-sm font-semibold transition-colors ${
              blocked
                ? "bg-gray-100 text-gray-400 cursor-not-allowed"
                : "bg-brand-primary hover:bg-brand-primaryDark text-white disabled:opacity-60 disabled:cursor-not-allowed"
            }`}
          >
            {loading && <Loader2 size={15} className="animate-spin" />}
            {label}
          </button>
        </div>
      </div>
    </div>
  );
}
