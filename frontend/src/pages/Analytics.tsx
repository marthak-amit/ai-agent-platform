import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  Legend,
} from "recharts";
import Layout from "../components/Layout";
import { getAnalyticsOverview, getLeadsFunnel, getTopQuestions } from "../api/client";
import { MessageSquare, TrendingUp, Zap, Clock } from "lucide-react";

interface Overview {
  messages_last_7_days: { date: string; count: number }[];
  leads_breakdown: { hot: number; warm: number; cold: number; total: number };
  conversion_rate: number;
  top_channels: { whatsapp: number; instagram: number; website: number };
  peak_hours: { hour: number; count: number }[];
  avg_response_time_seconds: number;
  total_conversations: number;
  new_conversations_today: number;
  messages_this_month: number;
}

interface Funnel {
  total_inquiries: number;
  qualified_leads: number;
  hot_leads: number;
  orders_placed: number;
  conversion_rate: number;
}

interface Topic {
  topic: string;
  count: number;
}

const CHANNEL_COLORS = ["#22c55e", "#a855f7", "#3b82f6"];
const DAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function dayLabel(dateStr: string) {
  const d = new Date(dateStr + "T00:00:00");
  return DAY_LABELS[d.getDay()];
}

function hourLabel(h: number) {
  if (h === 0) return "12am";
  if (h === 12) return "12pm";
  return h < 12 ? `${h}am` : `${h - 12}pm`;
}

const PERIOD_OPTIONS = ["7D", "30D", "90D"] as const;

export default function Analytics() {
  const { t } = useTranslation();
  const [overview, setOverview] = useState<Overview | null>(null);
  const [funnel, setFunnel] = useState<Funnel | null>(null);
  const [topics, setTopics] = useState<Topic[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [period, setPeriod] = useState<"7D" | "30D" | "90D">("7D");

  useEffect(() => {
    Promise.all([getAnalyticsOverview(), getLeadsFunnel(), getTopQuestions()])
      .then(([ov, fn, tq]) => {
        setOverview(ov);
        setFunnel(fn);
        setTopics(tq);
      })
      .catch(() => setError("Failed to load analytics data."))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <Layout>
        <div className="space-y-4">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {[...Array(4)].map((_, i) => (
              <div key={i} className="h-28 bg-white rounded-xl animate-pulse border border-gray-100" />
            ))}
          </div>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {[...Array(4)].map((_, i) => (
              <div key={i} className="h-64 bg-white rounded-xl animate-pulse border border-gray-100" />
            ))}
          </div>
        </div>
      </Layout>
    );
  }

  if (error || !overview || !funnel) {
    return (
      <Layout>
        <div className="flex items-center justify-center h-64 text-red-500">
          {error ?? "No data available yet."}
        </div>
      </Layout>
    );
  }

  const channelData = [
    { name: "WhatsApp", value: overview.top_channels.whatsapp },
    { name: "Instagram", value: overview.top_channels.instagram },
    { name: "Website", value: overview.top_channels.website },
  ].filter((d) => d.value > 0);

  const peakMax = Math.max(...overview.peak_hours.map((h) => h.count), 1);
  const peakPeakHour = overview.peak_hours.reduce(
    (best, h) => (h.count > best.count ? h : best),
    overview.peak_hours[0]
  );

  const funnelStages = [
    { label: t("analytics.total_inquiries"), value: funnel.total_inquiries, color: "bg-gray-300" },
    { label: t("analytics.qualified_leads"), value: funnel.qualified_leads, color: "bg-indigo-300" },
    { label: t("analytics.hot_leads"), value: funnel.hot_leads, color: "bg-indigo-500" },
    { label: t("analytics.orders_placed"), value: funnel.orders_placed, color: "bg-indigo-700" },
  ];
  const funnelMax = funnelStages[0].value || 1;

  const hourlyData = overview.peak_hours.map((h) => ({
    hour: hourLabel(h.hour),
    count: h.count,
    isPeak: h.hour === peakPeakHour.hour,
  }));

  const gradientCards = [
    {
      label: t("analytics.conversations_month"),
      value: overview.total_conversations,
      icon: MessageSquare,
      from: "from-indigo-500",
      to: "to-indigo-700",
    },
    {
      label: t("analytics.messages_today"),
      value: (overview.messages_last_7_days[overview.messages_last_7_days.length - 1])?.count ?? 0,
      icon: Zap,
      from: "from-green-500",
      to: "to-green-700",
    },
    {
      label: t("analytics.conversion_rate"),
      value: `${overview.conversion_rate}%`,
      icon: TrendingUp,
      from: "from-amber-500",
      to: "to-amber-600",
    },
    {
      label: t("analytics.avg_response_time"),
      value: `${overview.avg_response_time_seconds}s`,
      icon: Clock,
      from: "from-blue-500",
      to: "to-blue-700",
    },
  ];

  return (
    <Layout>
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold text-gray-900">{t("analytics.title")}</h1>
          <div className="flex gap-1 bg-gray-100 rounded-lg p-1">
            {PERIOD_OPTIONS.map((p) => (
              <button
                key={p}
                onClick={() => setPeriod(p)}
                className={`px-3 py-1 text-sm font-medium rounded-md transition-all duration-150 ${
                  period === p ? "bg-white text-gray-900 shadow-sm" : "text-gray-500 hover:text-gray-700"
                }`}
              >
                {p}
              </button>
            ))}
          </div>
        </div>

        {/* Gradient stat cards */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {gradientCards.map((card) => {
            const Icon = card.icon;
            return (
              <div
                key={card.label}
                className={`bg-gradient-to-br ${card.from} ${card.to} rounded-xl p-5 text-white shadow-sm`}
              >
                <div className="flex items-center justify-between mb-3">
                  <span className="text-xs font-medium uppercase tracking-wide text-white/70">{card.label}</span>
                  <div className="w-8 h-8 bg-white/20 rounded-lg flex items-center justify-center">
                    <Icon size={16} className="text-white" />
                  </div>
                </div>
                <p className="text-3xl font-bold text-white">{card.value}</p>
              </div>
            );
          })}
        </div>

        {/* Row 2: 7-day messages + lead funnel */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-5">
            <h2 className="font-semibold text-gray-900 mb-4">{t("analytics.messages_7_days")}</h2>
            {overview.messages_last_7_days.every((d) => d.count === 0) ? (
              <p className="text-gray-400 text-sm py-10 text-center">No messages yet.</p>
            ) : (
              <ResponsiveContainer width="100%" height={220}>
                <BarChart data={overview.messages_last_7_days}>
                  <XAxis dataKey="date" tickFormatter={dayLabel} tick={{ fontSize: 12 }} axisLine={false} tickLine={false} />
                  <YAxis allowDecimals={false} tick={{ fontSize: 12 }} axisLine={false} tickLine={false} />
                  <Tooltip formatter={(v) => [v, "messages"]} labelFormatter={(l) => dayLabel(String(l))} />
                  <Bar dataKey="count" fill="#6366f1" radius={[6, 6, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>

          <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-5">
            <h2 className="font-semibold text-gray-900 mb-4">{t("analytics.lead_funnel")}</h2>
            {funnel.total_inquiries === 0 ? (
              <p className="text-gray-400 text-sm py-10 text-center">No data yet.</p>
            ) : (
              <div className="space-y-5">
                {funnelStages.map((s) => {
                  const pct = funnelMax ? Math.round((s.value / funnelMax) * 100) : 0;
                  return (
                    <div key={s.label}>
                      <div className="flex justify-between text-sm mb-1.5">
                        <span className="text-gray-600">{s.label}</span>
                        <span className="font-semibold text-gray-900">
                          {s.value} <span className="text-gray-400 font-normal">({pct}%)</span>
                        </span>
                      </div>
                      <div className="h-2.5 bg-gray-100 rounded-full overflow-hidden">
                        <div className={`h-full ${s.color} rounded-full transition-all duration-700`} style={{ width: `${pct}%` }} />
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>

        {/* Row 3: Channel breakdown + peak hours */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-5">
            <h2 className="font-semibold text-gray-900 mb-4">{t("analytics.channel_breakdown")}</h2>
            {channelData.length === 0 ? (
              <p className="text-gray-400 text-sm py-10 text-center">No messages yet.</p>
            ) : (
              <ResponsiveContainer width="100%" height={220}>
                <PieChart>
                  <Pie
                    data={channelData}
                    dataKey="value"
                    nameKey="name"
                    innerRadius={55}
                    outerRadius={85}
                    paddingAngle={3}
                    label={({ name, percent }: { name?: string; percent?: number }) =>
                      `${name ?? ""} ${((percent ?? 0) * 100).toFixed(0)}%`
                    }
                    labelLine={false}
                  >
                    {channelData.map((_, i) => (
                      <Cell key={i} fill={CHANNEL_COLORS[i % CHANNEL_COLORS.length]} />
                    ))}
                  </Pie>
                  <Legend />
                  <Tooltip formatter={(v) => [v, "messages"]} />
                </PieChart>
              </ResponsiveContainer>
            )}
          </div>

          <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-5">
            <h2 className="font-semibold text-gray-900 mb-1">{t("analytics.peak_hours")}</h2>
            {peakMax === 1 && overview.peak_hours.every((h) => h.count === 0) ? (
              <p className="text-gray-400 text-sm py-10 text-center">No data yet.</p>
            ) : (
              <>
                <p className="text-xs text-gray-400 mb-3">
                  Busiest: {hourLabel(peakPeakHour.hour)} ({peakPeakHour.count} msgs)
                </p>
                <ResponsiveContainer width="100%" height={195}>
                  <BarChart data={hourlyData} barSize={8}>
                    <XAxis dataKey="hour" tick={{ fontSize: 10 }} interval={3} axisLine={false} tickLine={false} />
                    <YAxis allowDecimals={false} tick={{ fontSize: 11 }} axisLine={false} tickLine={false} />
                    <Tooltip formatter={(v) => [v, "messages"]} />
                    <Bar dataKey="count" radius={[3, 3, 0, 0]} fill="#6366f1">
                      {hourlyData.map((entry, i) => (
                        <Cell key={i} fill={entry.isPeak ? "#f59e0b" : "#6366f1"} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </>
            )}
          </div>
        </div>

        {/* Top questions */}
        <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-5">
          <h2 className="font-semibold text-gray-900 mb-4">{t("analytics.top_questions")}</h2>
          {topics.length === 0 ? (
            <p className="text-gray-400 text-sm py-6 text-center">No messages analysed yet.</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100">
                  <th className="pb-3 font-medium text-xs uppercase tracking-wide text-gray-400 text-left">Topic</th>
                  <th className="pb-3 font-medium text-xs uppercase tracking-wide text-gray-400 text-right">Count</th>
                </tr>
              </thead>
              <tbody>
                {topics.map((tp, i) => (
                  <tr key={tp.topic} className="border-b border-gray-50 last:border-0 hover:bg-gray-50 transition-colors">
                    <td className="py-3 text-gray-700 capitalize">
                      <span className="mr-2 text-gray-300">{i + 1}.</span>
                      {tp.topic}
                    </td>
                    <td className="py-3 text-right font-semibold text-gray-900">{tp.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </Layout>
  );
}
