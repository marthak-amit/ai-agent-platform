import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Search } from "lucide-react";
import { searchClients } from "../../api/admin";
import { Badge, Card, Empty, ErrorNote, fmtDate, inputCls, PageHeader, Pagination, Spinner, statusTone, Table, Td } from "../../components/admin/ui";
import { useAsync } from "../../components/admin/useAsync";

const PAGE_SIZE = 25;

export default function AdminClients() {
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);

  useEffect(() => {
    const id = window.setTimeout(() => {
      setDebounced(q.trim());
      setPage(1);
    }, 300);
    return () => window.clearTimeout(id);
  }, [q]);

  const { data, loading, error, reload } = useAsync(
    () => searchClients({ q: debounced, status, page, page_size: PAGE_SIZE }),
    [debounced, status, page],
  );

  return (
    <>
      <PageHeader title="Clients" subtitle="Every tenant on the platform" />
      <Card>
        <div className="flex flex-wrap gap-3 mb-4">
          <div className="relative flex-1 min-w-[220px]">
            <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
            <input className={`${inputCls} pl-9`} placeholder="Search id, email, business or phone" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search clients" />
          </div>
          <select className={`${inputCls} w-auto`} value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }} aria-label="Status filter">
            <option value="">All statuses</option>
            <option value="active">Active</option>
            <option value="suspended">Suspended</option>
          </select>
        </div>
        <ErrorNote message={error} onRetry={reload} />
        {loading && !data ? (
          <Spinner />
        ) : data && data.items.length === 0 ? (
          <Empty>No clients match.</Empty>
        ) : (
          data && (
            <>
              <Table head={["Client", "Status", "Plan", "Conversations", "Channels", "Joined"]}>
                {data.items.map((c) => (
                  <tr key={c.id} className="hover:bg-gray-50">
                    <Td>
                      <Link to={`/admin/clients/${c.id}`} className="font-medium text-brand-primaryDark hover:underline">
                        {c.business_name || "(no name)"}
                      </Link>
                      <div className="text-xs text-gray-500">#{c.id} · {c.email}</div>
                    </Td>
                    <Td>
                      <Badge tone={c.is_active ? "green" : "red"}>{c.is_active ? "Active" : "Suspended"}</Badge>
                      {c.billing_exempt && <span className="ml-1"><Badge tone="blue">Exempt</Badge></span>}
                    </Td>
                    <Td>
                      {c.sub_plan ? (
                        <>
                          {c.sub_plan} <Badge tone={statusTone(c.sub_status)}>{c.sub_status}</Badge>
                          <div className="text-xs text-gray-500">until {fmtDate(c.sub_period_end, false)}</div>
                        </>
                      ) : (
                        <span className="text-gray-400">No active plan</span>
                      )}
                    </Td>
                    <Td className="tabular-nums">
                      {c.conversation_limit != null ? `${c.conversations_used ?? 0} / ${c.conversation_limit}` : "—"}
                    </Td>
                    <Td>
                      <Badge tone={c.whatsapp_connected ? "green" : "gray"}>WhatsApp</Badge>{" "}
                      <Badge tone={c.instagram_connected ? "green" : "gray"}>Instagram</Badge>
                    </Td>
                    <Td className="whitespace-nowrap text-gray-500">{fmtDate(c.created_at, false)}</Td>
                  </tr>
                ))}
              </Table>
              <Pagination page={page} pageSize={PAGE_SIZE} total={data.total} onPage={setPage} />
            </>
          )
        )}
      </Card>
    </>
  );
}
