import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { AlertTriangle, Brain, IndianRupee, MessageSquare, Users } from "lucide-react";
import { getOverview } from "../../api/admin";
import StatCard from "../../components/StatCard";
import { Card, ErrorNote, fmtInr, fmtNum, PageHeader, Spinner } from "../../components/admin/ui";
import { useAsync } from "../../components/admin/useAsync";
import { useAdminAuth } from "../../context/AdminAuthContext";

export default function AdminOverview() {
  const { can } = useAdminAuth();
  const { data, loading, error, reload } = useAsync(getOverview, []);

  if (loading && !data) return <Spinner />;
  if (!data) return <ErrorNote message={error || "No data."} onRetry={reload} />;

  const a = data.attention;
  const attention = [
    { n: a.payments_awaiting_review, label: "payment screenshots awaiting a seller's review", to: "/admin/clients" },
    { n: a.bots_paused, label: "conversations with the bot paused (human takeover)", to: "/admin/clients" },
    { n: a.billing_webhook_errors, label: "unprocessed billing webhook errors", to: "/admin/billing?tab=events", perm: "billing.read" as const },
    { n: a.over_limit_clients, label: "clients over their conversation limit", to: "/admin/billing", perm: "billing.read" as const },
  ].filter((x) => x.n > 0 && (!x.perm || can(x.perm)));

  return (
    <>
      <PageHeader title="Overview" subtitle="Platform-wide snapshot" />
      <ErrorNote message={error} onRetry={reload} />
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4 mb-6">
        <StatCard label="Active clients" value={fmtNum(data.clients.active)} icon={Users} color="green" sub={`${data.clients.suspended} suspended · ${data.clients.total} total`} trend={`+${data.clients.new_7d} this week`} />
        <StatCard label="Collected (30 d)" value={fmtInr(data.revenue.collected_30d_inr)} icon={IndianRupee} color="blue" sub={`${data.revenue.paid_orders_30d} live payments · ${data.revenue.active_subscriptions} active subs · ${data.revenue.expiring_7d} expiring in 7d`} tooltip="Paid live-mode Razorpay orders, GST-inclusive. Test/mock payments are excluded." />
        <StatCard label="Messages today" value={fmtNum(data.messages.today)} icon={MessageSquare} color="indigo" sub={`${fmtNum(data.messages.this_month)} this month`} />
        <StatCard label="AI cost (30 d)" value={fmtInr(data.llm_30d.cost_inr, 2)} icon={Brain} color="yellow" sub={`${fmtNum(data.llm_30d.calls)} calls · ${fmtNum(data.llm_30d.failures)} failed`} />
      </div>

      <Card title="Needs attention" className="mb-6">
        {attention.length === 0 ? (
          <p className="text-sm text-emerald-700">Nothing needs a human right now.</p>
        ) : (
          <ul className="space-y-2">
            {attention.map((x) => (
              <li key={x.label} className="flex items-center gap-3 text-sm">
                <AlertTriangle size={16} className="text-amber-500 shrink-0" />
                <span>
                  <strong className="tabular-nums">{fmtNum(x.n)}</strong> {x.label}
                </span>
                <Link to={x.to} className="ml-auto text-brand-primaryDark font-medium hover:underline shrink-0">
                  Review →
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="New clients — last 14 days">
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data.signups_14d}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="date" tickFormatter={(d: string) => d.slice(5)} fontSize={11} />
                <YAxis allowDecimals={false} fontSize={11} width={28} />
                <Tooltip />
                <Bar dataKey="count" name="Sign-ups" fill="#0F8B4C" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>
        <Card title="AI cost per day (₹) — last 14 days">
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={data.llm_daily_14d}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="date" tickFormatter={(d: string) => d.slice(5)} fontSize={11} />
                <YAxis fontSize={11} width={40} />
                <Tooltip formatter={(v) => fmtInr(Number(v), 2)} />
                <Line type="monotone" dataKey="cost_inr" name="Cost" stroke="#1A2E44" strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Card>
      </div>
    </>
  );
}
