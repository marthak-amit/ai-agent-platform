import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  backfillInvoices, extendSubscription, getBillingPlans, getLegacyPlans, getPaymentEvent, getPaymentEvents, getRevenue,
  getSubscriptions, problemFrom, reprocessPaymentEvent, revokeSubscription, updateBillingPlan, updateLegacyPlan,
} from "../../api/admin";
import {
  Badge, Button, Card, Empty, ErrorNote, Field, fmtDate, fmtInr, fmtNum, fmtPaise, inputCls, Modal, PageHeader, Pagination,
  ReasonDialog, Spinner, statusTone, Table, Td,
} from "../../components/admin/ui";
import { useAsync } from "../../components/admin/useAsync";
import { useAdminAuth } from "../../context/AdminAuthContext";
import type { BillingPlan, LegacyPlan, PaymentEvent, Subscription } from "../../types/admin";

type Tab = "subscriptions" | "events" | "plans" | "revenue";
const TABS: { key: Tab; label: string }[] = [
  { key: "subscriptions", label: "Subscriptions" },
  { key: "events", label: "Payment events" },
  { key: "plans", label: "Plans" },
  { key: "revenue", label: "Revenue & invoices" },
];

export default function AdminBilling() {
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab") as Tab | null;
  const tab: Tab = TABS.some((t) => t.key === raw) ? (raw as Tab) : "subscriptions";

  return (
    <>
      <PageHeader title="Billing & plans" subtitle="Subscriptions, Razorpay events, sellable plans and revenue" />
      <div className="flex gap-1 mb-4 border-b border-gray-200 overflow-x-auto" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            onClick={() => setParams({ tab: t.key })}
            className={`px-4 py-2 text-sm font-medium whitespace-nowrap -mb-px border-b-2 ${
              tab === t.key ? "border-brand-primaryDark text-brand-primaryDark" : "border-transparent text-gray-500 hover:text-gray-800"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === "subscriptions" && <SubscriptionsTab />}
      {tab === "events" && <EventsTab />}
      {tab === "plans" && <PlansTab />}
      {tab === "revenue" && <RevenueTab />}
    </>
  );
}

// ── subscriptions ────────────────────────────────────────────────────────────

function SubscriptionsTab() {
  const { can } = useAdminAuth();
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const [clientId, setClientId] = useState("");
  const [action, setAction] = useState<{ kind: "extend" | "revoke"; sub: Subscription } | null>(null);
  const [days, setDays] = useState("30");
  const { data, loading, error, reload } = useAsync(
    () => getSubscriptions({ status: status || undefined, client_id: clientId ? Number(clientId) : undefined, page }),
    [status, clientId, page],
  );

  return (
    <Card>
      <div className="flex flex-wrap gap-3 mb-4">
        <select className={`${inputCls} w-auto`} value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }} aria-label="Status">
          <option value="">All statuses</option>
          {["active", "pending", "expired", "revoked", "cancelled", "superseded"].map((s) => <option key={s}>{s}</option>)}
        </select>
        <input className={`${inputCls} w-40`} placeholder="Client id" inputMode="numeric" value={clientId} onChange={(e) => { setClientId(e.target.value.replace(/\D/g, "")); setPage(1); }} aria-label="Client id" />
      </div>
      <ErrorNote message={error} onRetry={reload} />
      {loading && !data ? <Spinner /> : data && data.items.length === 0 ? <Empty>No subscriptions.</Empty> : data && (
        <>
          <Table head={["Client", "Plan", "Status", "Period", "Used", ""]}>
            {data.items.map((s) => (
              <tr key={s.id}>
                <Td><Link className="text-brand-primaryDark hover:underline font-medium" to={`/admin/clients/${s.client_id}`}>{s.business_name || s.client_email}</Link><div className="text-xs text-gray-500">#{s.client_id}</div></Td>
                <Td>{s.plan_name}{s.source_payment_id === null && <span className="ml-1"><Badge tone="blue">manual</Badge></span>}</Td>
                <Td><Badge tone={statusTone(s.status)}>{s.status}</Badge>{s.over_limit && <span className="ml-1"><Badge tone="red">over limit</Badge></span>}</Td>
                <Td className="whitespace-nowrap">{fmtDate(s.current_period_start, false)} → {fmtDate(s.current_period_end, false)}</Td>
                <Td className="tabular-nums">{fmtNum(s.conversations_used)} / {fmtNum(s.conversation_limit)}</Td>
                <Td className="whitespace-nowrap">
                  {can("billing.write") && (s.status === "active" || s.status === "pending") && (
                    <>
                      <Button variant="ghost" onClick={() => { setDays("30"); setAction({ kind: "extend", sub: s }); }}>Extend</Button>
                      <Button variant="ghost" className="text-red-600" onClick={() => setAction({ kind: "revoke", sub: s })}>Revoke</Button>
                    </>
                  )}
                </Td>
              </tr>
            ))}
          </Table>
          <Pagination page={page} pageSize={data.page_size} total={data.total} onPage={setPage} />
        </>
      )}
      {action?.kind === "extend" && (
        <ReasonDialog title="Extend subscription" confirmLabel="Extend" minReason={3} description={`${action.sub.business_name || action.sub.client_email} — queued periods after this one shift by the same amount.`}
          onSubmit={async (r) => { await extendSubscription(action.sub.id, Number(days), r); reload(); }} onClose={() => setAction(null)}>
          <Field label="Days to add"><input className={inputCls} type="number" min={1} max={3660} value={days} onChange={(e) => setDays(e.target.value)} required /></Field>
        </ReasonDialog>
      )}
      {action?.kind === "revoke" && (
        <ReasonDialog title="Revoke subscription" danger confirmLabel="Revoke" minReason={3} description="Cuts the period short now; the client enters the normal grace window."
          onSubmit={async (r) => { await revokeSubscription(action.sub.id, r); reload(); }} onClose={() => setAction(null)} />
      )}
    </Card>
  );
}

// ── payment events ───────────────────────────────────────────────────────────

function EventsTab() {
  const { can } = useAdminAuth();
  const [page, setPage] = useState(1);
  const [onlyErrors, setOnlyErrors] = useState(false);
  const [view, setView] = useState<PaymentEvent | null>(null);
  const [note, setNote] = useState("");
  const { data, loading, error, reload } = useAsync(() => getPaymentEvents({ has_error: onlyErrors || undefined, page }), [onlyErrors, page]);

  async function openEvent(id: number) {
    try {
      setView(await getPaymentEvent(id));
    } catch (err) {
      setNote(problemFrom(err).message);
    }
  }
  async function reprocess(id: number) {
    setNote("");
    try {
      const r = await reprocessPaymentEvent(id);
      setNote(`Event ${id}: ${r.outcome}${r.error ? ` — ${r.error}` : ""}`);
      reload();
    } catch (err) {
      setNote(problemFrom(err).message);
    }
  }

  return (
    <Card>
      <label className="flex items-center gap-2 text-sm mb-4">
        <input type="checkbox" checked={onlyErrors} onChange={(e) => { setOnlyErrors(e.target.checked); setPage(1); }} /> Only events with errors
      </label>
      {note && <p className="text-sm text-gray-700 mb-3">{note}</p>}
      <ErrorNote message={error} onRetry={reload} />
      {loading && !data ? <Spinner /> : data && data.items.length === 0 ? <Empty>No events.</Empty> : data && (
        <>
          <Table head={["Received", "Type", "Razorpay id", "State", ""]}>
            {data.items.map((ev) => (
              <tr key={ev.id}>
                <Td className="whitespace-nowrap">{fmtDate(ev.created_at)}</Td>
                <Td>{ev.event_type}</Td>
                <Td className="text-xs text-gray-500">{ev.razorpay_event_id}</Td>
                <Td>
                  {ev.error ? <Badge tone="red">error</Badge> : ev.processed ? <Badge tone="green">processed</Badge> : <Badge tone="amber">pending</Badge>}
                  {ev.error && <div className="text-xs text-red-600 max-w-xs break-words">{ev.error}</div>}
                </Td>
                <Td className="whitespace-nowrap">
                  <Button variant="ghost" onClick={() => openEvent(ev.id)}>Payload</Button>
                  {can("billing.write") && (ev.error || !ev.processed) && <Button variant="ghost" onClick={() => reprocess(ev.id)}>Reprocess</Button>}
                </Td>
              </tr>
            ))}
          </Table>
          <Pagination page={page} pageSize={data.page_size} total={data.total} onPage={setPage} />
        </>
      )}
      {view && (
        <Modal title={`Event ${view.razorpay_event_id}`} onClose={() => setView(null)} wide>
          <pre className="text-xs bg-gray-50 rounded-lg p-3 overflow-auto max-h-96">{JSON.stringify(view.payload, null, 2)}</pre>
        </Modal>
      )}
    </Card>
  );
}

// ── plans ────────────────────────────────────────────────────────────────────

function PlansTab() {
  const { can } = useAdminAuth();
  const billing = useAsync(getBillingPlans, []);
  const legacy = useAsync(getLegacyPlans, []);
  const [editing, setEditing] = useState<BillingPlan | null>(null);
  const [editingLegacy, setEditingLegacy] = useState<LegacyPlan | null>(null);
  const canEdit = can("plans.write");

  return (
    <div className="space-y-6">
      <Card title="Sellable plans (what clients buy)">
        <ErrorNote message={billing.error} onRetry={billing.reload} />
        {billing.loading && !billing.data ? <Spinner /> : (
          <Table head={["Plan", "Price (ex-GST)", "Conversations", "Period", "Features", "Subscribers", "Status", ""]}>
            {(billing.data ?? []).map((p) => (
              <tr key={p.id}>
                <Td><span className="font-medium">{p.name}</span><div className="text-xs text-gray-500">{p.code}</div></Td>
                <Td className="tabular-nums">{fmtPaise(p.price_paise)}</Td>
                <Td className="tabular-nums">{fmtNum(p.conversation_limit)}</Td>
                <Td>{p.billing_period_days} d</Td>
                <Td className="text-xs">{Object.entries(p.features).filter(([, v]) => v).map(([k]) => k).join(", ") || "—"}</Td>
                <Td className="tabular-nums">{p.active_subscribers}</Td>
                <Td><Badge tone={p.is_active ? "green" : "gray"}>{p.is_active ? "On sale" : "Hidden"}</Badge></Td>
                <Td>{canEdit && <Button variant="ghost" onClick={() => setEditing(p)}>Edit</Button>}</Td>
              </tr>
            ))}
          </Table>
        )}
        <p className="text-xs text-gray-400 mt-3">Edits apply to future purchases. Periods clients already paid for keep the limit they bought.</p>
      </Card>

      <Card title="Plan limits (channel gating, daily caps, campaigns)">
        <ErrorNote message={legacy.error} onRetry={legacy.reload} />
        {legacy.loading && !legacy.data ? <Spinner /> : (
          <Table head={["Plan", "Price", "Conversations", "Daily msgs", "Image quota", "Channels", "Status", ""]}>
            {(legacy.data ?? []).map((p) => (
              <tr key={p.plan_id}>
                <Td><span className="font-medium">{p.name}</span><div className="text-xs text-gray-500">{p.plan_id}</div></Td>
                <Td className="tabular-nums">{fmtInr(p.price_inr)}</Td>
                <Td className="tabular-nums">{fmtNum(p.conv_limit)}</Td>
                <Td className="tabular-nums">{fmtNum(p.daily_msg_limit)}</Td>
                <Td className="tabular-nums">{p.image_quota}</Td>
                <Td className="text-xs">{p.channels.join(", ")}</Td>
                <Td><Badge tone={p.is_active ? "green" : "gray"}>{p.is_active ? "Active" : "Inactive"}</Badge></Td>
                <Td>{canEdit && <Button variant="ghost" onClick={() => setEditingLegacy(p)}>Edit</Button>}</Td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      {editing && <BillingPlanDialog plan={editing} onDone={billing.reload} onClose={() => setEditing(null)} />}
      {editingLegacy && <LegacyPlanDialog plan={editingLegacy} onDone={legacy.reload} onClose={() => setEditingLegacy(null)} />}
    </div>
  );
}

function BillingPlanDialog({ plan, onDone, onClose }: { plan: BillingPlan; onDone: () => void; onClose: () => void }) {
  const [name, setName] = useState(plan.name);
  const [price, setPrice] = useState(String(plan.price_paise / 100));
  const [limit, setLimit] = useState(String(plan.conversation_limit));
  const [order, setOrder] = useState(String(plan.sort_order));
  const [active, setActive] = useState(plan.is_active);
  return (
    <ReasonDialog title={`Edit ${plan.name}`} confirmLabel="Save plan" onClose={onClose}
      onSubmit={async (reason) => {
        const body: Parameters<typeof updateBillingPlan>[1] = { reason };
        if (name !== plan.name) body.name = name;
        const paise = Math.round(Number(price) * 100);
        if (paise !== plan.price_paise) body.price_paise = paise;
        if (Number(limit) !== plan.conversation_limit) body.conversation_limit = Number(limit);
        if (Number(order) !== plan.sort_order) body.sort_order = Number(order);
        if (active !== plan.is_active) body.is_active = active;
        await updateBillingPlan(plan.id, body);
        onDone();
      }}>
      <div className="space-y-3">
        <Field label="Name"><input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} required /></Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Price (₹, excl. GST)"><input className={inputCls} type="number" min={0} step="0.01" value={price} onChange={(e) => setPrice(e.target.value)} required /></Field>
          <Field label="Conversations"><input className={inputCls} type="number" min={1} value={limit} onChange={(e) => setLimit(e.target.value)} required /></Field>
          <Field label="Sort order"><input className={inputCls} type="number" min={0} value={order} onChange={(e) => setOrder(e.target.value)} required /></Field>
        </div>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} /> On sale</label>
      </div>
    </ReasonDialog>
  );
}

function LegacyPlanDialog({ plan, onDone, onClose }: { plan: LegacyPlan; onDone: () => void; onClose: () => void }) {
  const [form, setForm] = useState({
    price_inr: String(plan.price_inr), conv_limit: String(plan.conv_limit), daily_msg_limit: String(plan.daily_msg_limit),
    image_quota: String(plan.image_quota), image_overage_price: String(plan.image_overage_price),
  });
  const [active, setActive] = useState(plan.is_active);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await updateLegacyPlan(plan.plan_id, {
        price_inr: Number(form.price_inr), conv_limit: Number(form.conv_limit), daily_msg_limit: Number(form.daily_msg_limit),
        image_quota: Number(form.image_quota), image_overage_price: Number(form.image_overage_price), is_active: active,
      });
      onDone();
      onClose();
    } catch (err) {
      setError(problemFrom(err).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={`Edit ${plan.name} limits`} onClose={onClose}>
      <form onSubmit={save} className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Price (₹)"><input className={inputCls} type="number" min={0} value={form.price_inr} onChange={set("price_inr")} required /></Field>
          <Field label="Conversations"><input className={inputCls} type="number" min={0} value={form.conv_limit} onChange={set("conv_limit")} required /></Field>
          <Field label="Daily messages"><input className={inputCls} type="number" min={0} value={form.daily_msg_limit} onChange={set("daily_msg_limit")} required /></Field>
          <Field label="Image quota"><input className={inputCls} type="number" min={0} value={form.image_quota} onChange={set("image_quota")} required /></Field>
          <Field label="Image overage (₹)"><input className={inputCls} type="number" min={0} value={form.image_overage_price} onChange={set("image_overage_price")} required /></Field>
        </div>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} /> Active</label>
        <ErrorNote message={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={busy}>Save</Button>
        </div>
      </form>
    </Modal>
  );
}

// ── revenue + invoices ───────────────────────────────────────────────────────

function RevenueTab() {
  const { can } = useAdminAuth();
  const { data, loading, error, reload } = useAsync(getRevenue, []);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  async function backfill() {
    setBusy(true);
    setMsg("");
    try {
      const r = await backfillInvoices();
      setMsg(r.created ? `Issued ${r.created} invoice(s): ${r.invoice_numbers.join(", ")}` : "Nothing to backfill — every paid order has an invoice.");
    } catch (err) {
      setMsg(problemFrom(err).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <Card title={data ? `Projected revenue — ${data.month}` : "Projected revenue"}>
        <ErrorNote message={error} onRetry={reload} />
        {loading && !data ? <Spinner /> : data && (
          <>
            <p className="text-3xl font-bold tabular-nums mb-3">{fmtInr(data.total_revenue_inr)}</p>
            <Table head={["Plan", "Active clients", "Revenue"]}>
              {data.breakdown.map((b) => (
                <tr key={b.plan}><Td>{b.plan_name}</Td><Td className="tabular-nums">{b.client_count}</Td><Td className="tabular-nums">{fmtInr(b.revenue_inr)}</Td></tr>
              ))}
            </Table>
          </>
        )}
      </Card>
      {can("billing.write") && (
        <Card title="Invoices">
          <p className="text-sm text-gray-600 mb-3">Issue tax invoices for paid orders that predate invoicing (oldest first, so numbers follow payment order).</p>
          <Button onClick={backfill} disabled={busy}>Backfill missing invoices</Button>
          {msg && <p className="text-sm text-gray-700 mt-3">{msg}</p>}
        </Card>
      )}
    </div>
  );
}
