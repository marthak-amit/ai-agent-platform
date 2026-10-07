import { useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { getLLMUsage } from "../../api/admin";
import StatCard from "../../components/StatCard";
import { Card, ErrorNote, Field, fmtInr, fmtNum, inputCls, PageHeader, Spinner, Table, Td } from "../../components/admin/ui";
import { useAsync } from "../../components/admin/useAsync";
import type { UsageMetrics } from "../../types/admin";

const isoDay = (d: Date) => d.toISOString().slice(0, 10);

function MetricsRow({ label, m }: { label: React.ReactNode; m: UsageMetrics }) {
  return (
    <tr>
      <Td>{label}</Td>
      <Td className="tabular-nums">{fmtNum(m.calls)}</Td>
      <Td className="tabular-nums">{fmtNum(m.prompt_tokens + m.completion_tokens)}</Td>
      <Td className="tabular-nums">{fmtInr(m.cost_inr, 2)}</Td>
      <Td className="tabular-nums">{m.failures ? <span className="text-red-600">{m.failures}</span> : 0}</Td>
      <Td className="tabular-nums">{Math.round(m.avg_latency_ms)} ms</Td>
    </tr>
  );
}
const HEAD = ["", "Calls", "Tokens", "Cost", "Failed", "Avg latency"];

export default function AdminUsage() {
  const today = new Date();
  const [from, setFrom] = useState(isoDay(new Date(today.getTime() - 29 * 86_400_000)));
  const [to, setTo] = useState(isoDay(today));
  const [clientId, setClientId] = useState("");
  const { data, loading, error, reload } = useAsync(
    () => getLLMUsage({ from, to, client_id: clientId ? Number(clientId) : undefined }),
    [from, to, clientId],
  );

  return (
    <>
      <PageHeader title="AI usage & cost" subtitle="Every provider call, priced from real token counts" />
      <Card className="mb-6">
        <div className="flex flex-wrap gap-3">
          <Field label="From"><input className={inputCls} type="date" value={from} max={to} onChange={(e) => setFrom(e.target.value)} /></Field>
          <Field label="To"><input className={inputCls} type="date" value={to} min={from} onChange={(e) => setTo(e.target.value)} /></Field>
          <Field label="Client id (blank = all)"><input className={inputCls} inputMode="numeric" value={clientId} onChange={(e) => setClientId(e.target.value.replace(/\D/g, ""))} /></Field>
        </div>
      </Card>
      <ErrorNote message={error} onRetry={reload} />
      {loading && !data ? <Spinner /> : data && (
        <div className="space-y-6">
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <StatCard label="Total cost" value={fmtInr(data.totals.cost_inr, 2)} color="yellow" sub={`${data.from_date} → ${data.to_date}`} />
            <StatCard label="Calls" value={fmtNum(data.totals.calls)} color="blue" sub={`${fmtNum(data.totals.prompt_tokens + data.totals.completion_tokens)} tokens`} />
            <StatCard label="Failures" value={fmtNum(data.totals.failures)} color={data.totals.failures ? "red" : "green"} sub={data.totals.calls ? `${((data.totals.failures / data.totals.calls) * 100).toFixed(1)}% of calls` : ""} />
            <StatCard label="Avg latency" value={`${Math.round(data.totals.avg_latency_ms)} ms`} color="indigo" />
          </div>
          <Card title="Cost per day (₹)">
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={data.per_day}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="day" tickFormatter={(d: string) => d.slice(5)} fontSize={11} />
                  <YAxis fontSize={11} width={44} />
                  <Tooltip formatter={(v) => fmtInr(Number(v), 2)} />
                  <Line type="monotone" dataKey="cost_inr" name="Cost" stroke="#0F8B4C" strokeWidth={2} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </Card>
          <div className="grid gap-6 lg:grid-cols-2">
            <Card title="By purpose"><Table head={["Purpose", ...HEAD.slice(1)]}>{data.per_purpose.map((r) => <MetricsRow key={r.purpose} label={r.purpose} m={r} />)}</Table></Card>
            <Card title="By model"><Table head={["Model", ...HEAD.slice(1)]}>{data.per_model.map((r) => <MetricsRow key={r.model} label={<span className="text-xs">{r.model}</span>} m={r} />)}</Table></Card>
            <Card title="Most expensive conversations"><Table head={["Conversation", ...HEAD.slice(1)]}>{data.per_conversation.slice(0, 15).map((r) => <MetricsRow key={r.conversation_id} label={`#${r.conversation_id}`} m={r} />)}</Table></Card>
            <Card title="Most expensive orders"><Table head={["Order", ...HEAD.slice(1)]}>{data.per_order.slice(0, 15).map((r) => <MetricsRow key={r.order_id} label={r.order_number ?? `#${r.order_id}`} m={r} />)}</Table></Card>
          </div>
        </div>
      )}
    </>
  );
}
