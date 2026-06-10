import type { LucideIcon } from "lucide-react";

interface StatCardProps {
  label: string;
  value: number | string;
  color?: "indigo" | "green" | "yellow" | "red" | "blue";
  icon?: LucideIcon;
  trend?: string;
  sub?: string;
  tooltip?: string;
}

const COLOR_MAP = {
  indigo: { bg: "bg-indigo-100", text: "text-indigo-600" },
  green:  { bg: "bg-green-100",  text: "text-green-600"  },
  yellow: { bg: "bg-amber-100",  text: "text-amber-600"  },
  red:    { bg: "bg-red-100",    text: "text-red-600"    },
  blue:   { bg: "bg-blue-100",   text: "text-blue-600"   },
};

export default function StatCard({ label, value, color = "indigo", icon: Icon, trend, sub, tooltip }: StatCardProps) {
  const colors = COLOR_MAP[color];

  const trendColor =
    trend?.startsWith("+") ? "text-emerald-600" :
    trend?.startsWith("-") ? "text-red-500" :
    "text-gray-400";

  return (
    <div
      title={tooltip}
      className="bg-white rounded-2xl border border-gray-100 p-5 cursor-default transition-shadow duration-200 hover:shadow-md"
      style={{ boxShadow: "0 1px 3px rgba(0,0,0,0.08), 0 8px 24px rgba(0,0,0,0.04)" }}
    >
      {/* Row 1 — label + trend */}
      <div className="flex items-center justify-between mb-2">
        <span className="text-[11px] font-medium uppercase tracking-[0.05em] text-gray-500">
          {label}
        </span>
        {trend && (
          <span className={`text-[11px] font-semibold ${trendColor}`}>{trend}</span>
        )}
      </div>

      {/* Row 2 — big number */}
      <p className="text-[36px] font-bold text-gray-900 leading-none tabular-nums mb-3">
        {value}
      </p>

      {/* Row 3 — icon circle + sub text */}
      <div className="flex items-center gap-3">
        {Icon && (
          <div className={`w-10 h-10 rounded-full flex items-center justify-center shrink-0 ${colors.bg}`}>
            <Icon size={20} className={colors.text} />
          </div>
        )}
        {sub && <span className="text-xs text-gray-400">{sub}</span>}
      </div>
    </div>
  );
}
