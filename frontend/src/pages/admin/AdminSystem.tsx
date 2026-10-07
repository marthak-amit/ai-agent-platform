import { CheckCircle2, RefreshCw, XCircle } from "lucide-react";
import { getSystemHealth } from "../../api/admin";
import { Badge, Button, Card, ErrorNote, fmtDate, PageHeader, Spinner, statusTone, Table, Td } from "../../components/admin/ui";
import { useAsync } from "../../components/admin/useAsync";

const isOk = (v: string) => v === "ok" || v.startsWith("ok");

export default function AdminSystem() {
  const { data, loading, error, reload } = useAsync(getSystemHealth, []);

  return (
    <>
      <PageHeader
        title="System health"
        subtitle={data ? `As of ${fmtDate(data.timestamp)}` : undefined}
        actions={<Button variant="secondary" onClick={reload} disabled={loading}><RefreshCw size={14} className={loading ? "animate-spin" : ""} /> Refresh</Button>}
      />
      <ErrorNote message={error} onRetry={reload} />
      {loading && !data ? <Spinner /> : data && (
        <div className="grid gap-6 lg:grid-cols-2">
          <Card title="Checks" actions={<Badge tone={statusTone(data.status)}>{data.status}</Badge>}>
            <ul className="space-y-2 text-sm">
              {Object.entries(data.checks).map(([k, v]) => (
                <li key={k} className="flex items-start gap-2">
                  {isOk(v) ? <CheckCircle2 size={16} className="text-emerald-600 mt-0.5 shrink-0" /> : <XCircle size={16} className="text-red-600 mt-0.5 shrink-0" />}
                  <span><span className="font-medium">{k}</span> <span className="text-gray-500">{v}</span></span>
                </li>
              ))}
            </ul>
          </Card>

          <Card title="LLM">
            <div className="space-y-1.5 text-sm">
              <div>Circuit breaker: {data.llm.available ? <Badge tone="green">closed</Badge> : <Badge tone="red">open — {data.llm.reason}</Badge>}</div>
              <div>Failures in the last hour: <strong>{data.llm.failures_last_hour}</strong></div>
              <div>Vision: {data.llm.vision_enabled ? <Badge tone="green">enabled</Badge> : <Badge tone="amber">disabled — {data.llm.vision_disabled_reason}</Badge>}</div>
              {Object.entries(data.llm.missing_models).map(([m, why]) => <div key={m} className="text-red-600">Missing model {m}: {why}</div>)}
              <div className="pt-2 text-xs text-gray-500">
                {Object.entries(data.config.llm_models).map(([k, v]) => <div key={k}>{k}: <span className="font-mono">{v}</span></div>)}
              </div>
            </div>
          </Card>

          <Card title="Instagram">
            <div className="text-sm space-y-1.5">
              <div>Kill switch: {data.instagram.disabled ? <Badge tone="red">sends disabled</Badge> : <Badge tone="green">off</Badge>}</div>
              <div>Token verdicts: {Object.keys(data.instagram.client_verdicts).length ? Object.entries(data.instagram.client_verdicts).map(([k, v]) => <Badge key={k} tone={k === "invalid" ? "red" : "green"}>{`${k}: ${v}`}</Badge>) : <span className="text-gray-400">none recorded</span>}</div>
              {data.instagram.invalid_clients.length > 0 && <div className="text-red-600">Invalid for client(s): {data.instagram.invalid_clients.join(", ")}</div>}
            </div>
          </Card>

          <Card title="Billing pipeline">
            {data.billing.error ? <p className="text-sm text-red-600">{data.billing.error}</p> : (
              <div className="text-sm space-y-1.5">
                <div>Last Razorpay webhook: {fmtDate(data.billing.last_webhook_at)}</div>
                <div>Webhook errors (24 h): <strong className={data.billing.webhook_errors_24h ? "text-red-600" : ""}>{data.billing.webhook_errors_24h ?? 0}</strong></div>
                {(data.billing.jobs ?? []).map((j) => <div key={j.name}>Job <span className="font-mono">{j.name}</span>: <Badge tone={statusTone(j.status)}>{j.status}</Badge> <span className="text-gray-400">{fmtDate(j.last_finished_at)}</span></div>)}
              </div>
            )}
          </Card>

          <Card title="Scheduled jobs" className="lg:col-span-2">
            <Table head={["Job", "Trigger", "Next run"]}>
              {data.scheduler.map((j) => <tr key={j.id}><Td className="font-mono text-xs">{j.id}</Td><Td className="text-xs text-gray-500">{j.trigger}</Td><Td>{fmtDate(j.next_run_time)}</Td></tr>)}
            </Table>
          </Card>

          <Card title="Configuration (no secrets)" className="lg:col-span-2">
            <div className="text-sm grid sm:grid-cols-2 gap-x-8 gap-y-1.5">
              <div>Environment: <strong>{data.config.environment}</strong></div>
              <div>Router v2 client ids: <span className="font-mono">{data.config.router_v2_client_ids}</span></div>
              <div>Shared admin key: {data.config.admin_api_key_enabled ? <Badge tone="amber">enabled</Badge> : <Badge tone="green">disabled</Badge>}</div>
              {Object.entries(data.config.secrets_configured).map(([k, v]) => <div key={k}>{k}: {v ? <Badge tone="green">configured</Badge> : <Badge tone="red">missing</Badge>}</div>)}
            </div>
          </Card>
        </div>
      )}
    </>
  );
}
