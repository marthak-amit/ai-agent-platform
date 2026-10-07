import { useTranslation } from "react-i18next";
import { usageTone, type UsageTone } from "../../utils/billing";

const BAR: Record<UsageTone, string> = {
  ok: "bg-brand-primary",
  warn: "bg-brand-warning",
  over: "bg-brand-accent",
};
const TEXT: Record<UsageTone, string> = {
  ok: "text-brand-primaryDark",
  warn: "text-amber-700",
  over: "text-brand-accent",
};

/** Conversations used / limit. Green < 80 %, amber 80–100 %, coral > 100 % (the bar itself caps at full width). */
export default function UsageBar({ used, limit, percent }: { used: number; limit: number; percent: number }) {
  const { t } = useTranslation();
  const tone = usageTone(percent);
  const width = Math.min(100, Math.max(0, percent));
  return (
    <div>
      <div className="flex items-baseline justify-between mb-1.5">
        <span className="text-sm text-gray-600">
          <span className="font-semibold text-brand-secondary">{used.toLocaleString("en-IN")}</span>
          {" / "}
          {limit.toLocaleString("en-IN")} {t("billing.conversations")}
        </span>
        <span className={`text-sm font-semibold ${TEXT[tone]}`}>{Math.round(percent)}%</span>
      </div>
      <div
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.min(100, Math.round(percent))}
        aria-label={t("billing.usage")}
        className="h-2.5 w-full rounded-full bg-gray-100 overflow-hidden"
      >
        <div className={`h-full rounded-full transition-all ${BAR[tone]}`} style={{ width: `${width}%` }} />
      </div>
    </div>
  );
}
