import { useState } from "react";
import { getAuditLog } from "../../api/admin";
import { Badge, Button, Card, Empty, ErrorNote, fmtDate, inputCls, Modal, PageHeader, Pagination, Spinner, Table, Td } from "../../components/admin/ui";
import { useAsync } from "../../components/admin/useAsync";
import type { AuditEntry } from "../../types/admin";

export default function AdminAudit() {
  const [actor, setActor] = useState("");
  const [action, setAction] = useState("");
  const [outcome, setOutcome] = useState("");
  const [clientId, setClientId] = useState("");
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<AuditEntry | null>(null);
  const { data, loading, error, reload } = useAsync(
    () => getAuditLog({
      actor: actor.trim() || undefined, action: action.trim() || undefined,
      success: outcome === "" ? undefined : outcome === "ok", client_id: clientId ? Number(clientId) : undefined, page,
    }),
    [actor, action, outcome, clientId, page],
  );
  const reset = <T,>(set: (v: T) => void) => (v: T) => { set(v); setPage(1); };

  return (
    <>
      <PageHeader title="Audit log" subtitle="Everything operators did — and every sign-in attempt" />
      <Card>
        <div className="flex flex-wrap gap-3 mb-4">
          <input className={`${inputCls} w-52`} placeholder="Actor (email)" value={actor} onChange={(e) => reset(setActor)(e.target.value)} aria-label="Actor" />
          <input className={`${inputCls} w-52`} placeholder="Action (e.g. client.)" value={action} onChange={(e) => reset(setAction)(e.target.value)} aria-label="Action" />
          <input className={`${inputCls} w-32`} placeholder="Client id" inputMode="numeric" value={clientId} onChange={(e) => reset(setClientId)(e.target.value.replace(/\D/g, ""))} aria-label="Client id" />
          <select className={`${inputCls} w-auto`} value={outcome} onChange={(e) => reset(setOutcome)(e.target.value)} aria-label="Outcome">
            <option value="">All outcomes</option>
            <option value="ok">Succeeded</option>
            <option value="fail">Failed / denied</option>
          </select>
        </div>
        <ErrorNote message={error} onRetry={reload} />
        {loading && !data ? <Spinner /> : data && data.items.length === 0 ? <Empty>No entries.</Empty> : data && (
          <>
            <Table head={["When", "Actor", "Action", "Target", "Reason", "IP", ""]}>
              {data.items.map((e) => (
                <tr key={e.id} className={e.success ? "" : "bg-red-50/40"}>
                  <Td className="whitespace-nowrap text-gray-500">{fmtDate(e.created_at)}</Td>
                  <Td>{e.actor}</Td>
                  <Td><span className="font-mono text-xs">{e.action}</span> {!e.success && <Badge tone="red">failed</Badge>}</Td>
                  <Td className="text-xs text-gray-500">{[e.target_type, e.target_id].filter(Boolean).join(" #")}{e.client_id ? ` · client ${e.client_id}` : ""}</Td>
                  <Td className="max-w-xs break-words text-gray-600">{e.reason}</Td>
                  <Td className="text-xs text-gray-400">{e.ip}</Td>
                  <Td><Button variant="ghost" onClick={() => setOpen(e)}>Detail</Button></Td>
                </tr>
              ))}
            </Table>
            <Pagination page={page} pageSize={data.page_size} total={data.total} onPage={setPage} />
          </>
        )}
      </Card>
      {open && (
        <Modal title={`${open.action} — #${open.id}`} onClose={() => setOpen(null)} wide>
          <pre className="text-xs bg-gray-50 rounded-lg p-3 overflow-auto max-h-96">{JSON.stringify(open.detail, null, 2)}</pre>
        </Modal>
      )}
    </>
  );
}
