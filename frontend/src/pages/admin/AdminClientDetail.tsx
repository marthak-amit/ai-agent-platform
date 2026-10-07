import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Eye, Loader2, LogIn } from "lucide-react";
import {
  activateClient,
  getBillingPlans,
  getClientConversations,
  getClientDetail,
  getClientOrders,
  getConversationMessages,
  grantSubscription,
  impersonateClient,
  patchClientFlags,
  problemFrom,
  setBillingExempt,
  suspendClient,
} from "../../api/admin";
import {
  Badge, Button, Card, Empty, ErrorNote, Field, fmtDate, fmtInr, fmtNum, inputCls, Modal, PageHeader, Pagination, ReasonDialog,
  Spinner, statusTone, Table, Td,
} from "../../components/admin/ui";
import { useAsync } from "../../components/admin/useAsync";
import { useAdminAuth } from "../../context/AdminAuthContext";
import type { ClientDetail, ConversationRow } from "../../types/admin";

type Tab = "overview" | "conversations" | "orders";
type Dialog = null | "suspend" | "activate" | "impersonate" | "grant" | "exempt";

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4 py-1.5 text-sm border-b border-gray-50 last:border-0">
      <span className="text-gray-500">{k}</span>
      <span className="text-gray-900 text-right break-all">{children}</span>
    </div>
  );
}

const yn = (v: boolean) => <Badge tone={v ? "green" : "gray"}>{v ? "Yes" : "No"}</Badge>;

export default function AdminClientDetail() {
  const { id } = useParams();
  const clientId = Number(id);
  const { can } = useAdminAuth();
  const { data, loading, error, reload } = useAsync(() => getClientDetail(clientId), [clientId]);
  const [tab, setTab] = useState<Tab>("overview");
  const [dialog, setDialog] = useState<Dialog>(null);

  if (loading && !data) return <Spinner />;
  if (!data) return <ErrorNote message={error || "Client not found."} onRetry={reload} />;
  const { profile } = data;

  return (
    <>
      <Link to="/admin/clients" className="inline-flex items-center gap-1 text-sm text-gray-500 hover:text-gray-800 mb-3">
        <ArrowLeft size={14} /> All clients
      </Link>
      <PageHeader
        title={profile.business_name || `Client #${profile.id}`}
        subtitle={`#${profile.id} · ${profile.email}${profile.phone ? ` · ${profile.phone}` : ""} · joined ${fmtDate(profile.created_at, false)}`}
        actions={
          <>
            <Badge tone={profile.is_active ? "green" : "red"}>{profile.is_active ? "Active" : "Suspended"}</Badge>
            {can("clients.impersonate") && profile.is_active && (
              <Button variant="secondary" onClick={() => setDialog("impersonate")}>
                <LogIn size={14} /> Sign in as client
              </Button>
            )}
            {can("billing.write") && (
              <>
                <Button variant="secondary" onClick={() => setDialog("grant")}>Grant plan</Button>
                <Button variant="secondary" onClick={() => setDialog("exempt")}>
                  {data.flags.billing_exempt ? "Remove billing exemption" : "Exempt from billing"}
                </Button>
              </>
            )}
            {can("clients.write") &&
              (profile.is_active ? (
                <Button variant="danger" onClick={() => setDialog("suspend")}>Suspend</Button>
              ) : (
                <Button onClick={() => setDialog("activate")}>Activate</Button>
              ))}
          </>
        }
      />

      <div className="flex gap-1 mb-4 border-b border-gray-200" role="tablist">
        {(["overview", "conversations", "orders"] as Tab[])
          .filter((t) => t !== "conversations" || can("conversations.read"))
          .map((t) => (
            <button
              key={t}
              role="tab"
              aria-selected={tab === t}
              onClick={() => setTab(t)}
              className={`px-4 py-2 text-sm font-medium capitalize -mb-px border-b-2 ${
                tab === t ? "border-brand-primaryDark text-brand-primaryDark" : "border-transparent text-gray-500 hover:text-gray-800"
              }`}
            >
              {t}
            </button>
          ))}
      </div>

      {tab === "overview" && <OverviewTab data={data} clientId={clientId} onChanged={reload} canWrite={can("clients.write")} />}
      {tab === "conversations" && <ConversationsTab clientId={clientId} />}
      {tab === "orders" && <OrdersTab clientId={clientId} />}

      {dialog === "suspend" && (
        <ReasonDialog title="Suspend client" danger confirmLabel="Suspend" description="The client's bot stops answering customers and their dashboard login is refused until reactivated. Data is kept."
          onSubmit={async (r) => { await suspendClient(clientId, r); reload(); }} onClose={() => setDialog(null)} />
      )}
      {dialog === "activate" && (
        <ReasonDialog title="Activate client" confirmLabel="Activate" onSubmit={async (r) => { await activateClient(clientId, r); reload(); }} onClose={() => setDialog(null)} />
      )}
      {dialog === "exempt" && (
        <ReasonDialog title={data.flags.billing_exempt ? "Remove billing exemption" : "Exempt from billing"} confirmLabel="Confirm" minReason={3}
          description={data.flags.billing_exempt ? "The client will be subject to subscription enforcement again." : "The client keeps full service without a paid plan."}
          onSubmit={async (r) => { await setBillingExempt(clientId, !data.flags.billing_exempt, r); reload(); }} onClose={() => setDialog(null)} />
      )}
      {dialog === "impersonate" && <ImpersonateDialog clientId={clientId} onClose={() => setDialog(null)} />}
      {dialog === "grant" && <GrantDialog clientId={clientId} onDone={reload} onClose={() => setDialog(null)} />}
    </>
  );
}

function OverviewTab({ data, clientId, onChanged, canWrite }: { data: ClientDetail; clientId: number; onChanged: () => void; canWrite: boolean }) {
  const { flags, channels, billing, counts, usage, llm_30d: llm } = data;
  const sub = billing.active_subscription;
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card title="Subscription">
        {sub ? (
          <>
            <Row k="Plan">{sub.plan_name} <Badge tone={statusTone(sub.status)}>{sub.status}</Badge></Row>
            <Row k="Period">{fmtDate(sub.period_start, false)} → {fmtDate(sub.period_end, false)}</Row>
            <Row k="Conversations">{fmtNum(sub.conversations_used)} / {fmtNum(sub.conversation_limit)} {sub.over_limit && <Badge tone="red">over limit</Badge>}</Row>
            <Row k="Queued renewals">{billing.queued_periods}</Row>
          </>
        ) : (
          <Empty>No active subscription{flags.billing_exempt ? " (billing exempt)" : ""}.</Empty>
        )}
        <Row k="Billing exempt">{yn(flags.billing_exempt)}</Row>
        <Row k="Legacy plan slug">{flags.plan_slug}</Row>
      </Card>

      <Card title="Usage">
        <Row k="Messages today">{fmtNum(usage.messages_today)}</Row>
        <Row k="Messages this month">{fmtNum(usage.messages_this_month)}</Row>
        <Row k="AI cost (30 d)">{fmtInr(llm.cost_inr, 2)} · {fmtNum(llm.calls)} calls · {fmtNum(llm.failures)} failed</Row>
        <Row k="Conversations">{fmtNum(counts.conversations)} ({counts.paused_conversations} bot-paused)</Row>
        <Row k="Orders">{fmtNum(counts.orders)} ({counts.orders_awaiting_payment_review} awaiting payment review)</Row>
        <Row k="Products">{fmtNum(counts.products)}</Row>
      </Card>

      <FlagsCard data={data} clientId={clientId} onChanged={onChanged} canWrite={canWrite} />

      <Card title="Channels & payments (credentials are never shown)">
        <Row k="WhatsApp phone-number id">{channels.whatsapp_phone_number_id || "—"}</Row>
        <Row k="WhatsApp token set">{yn(channels.whatsapp_token_set)}</Row>
        <Row k="Instagram account id">{channels.instagram_account_id || "—"}</Row>
        <Row k="Instagram token set">{yn(channels.instagram_token_set)}</Row>
        <Row k="UPI configured">{yn(channels.upi_configured)}</Row>
        <Row k="Own Razorpay keys">{yn(channels.razorpay_configured)}</Row>
      </Card>

      <Card title="Team">
        <Table head={["Email", "Role", "Status"]}>
          {data.users.map((u) => (
            <tr key={u.id}>
              <Td>{u.email}</Td>
              <Td>{u.role}</Td>
              <Td>{u.invited ? <Badge tone="amber">Invited</Badge> : <Badge tone={u.is_active ? "green" : "red"}>{u.is_active ? "Active" : "Disabled"}</Badge>}</Td>
            </tr>
          ))}
        </Table>
      </Card>

      <Card title="Recent admin actions on this client">
        {data.recent_audit.length === 0 ? (
          <Empty>None yet.</Empty>
        ) : (
          <ul className="space-y-2 text-sm">
            {data.recent_audit.map((a) => (
              <li key={a.id}>
                <span className="font-medium">{a.action}</span> by {a.actor}
                <span className="text-gray-400"> · {fmtDate(a.created_at)}</span>
                {a.reason && <div className="text-xs text-gray-500">“{a.reason}”</div>}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function FlagsCard({ data, clientId, onChanged, canWrite }: { data: ClientDetail; clientId: number; onChanged: () => void; canWrite: boolean }) {
  const f = data.flags;
  const [router, setRouter] = useState<string>(f.router_v2_enabled === null ? "default" : String(f.router_v2_enabled));
  const [daily, setDaily] = useState(String(f.daily_message_limit));
  const [resume, setResume] = useState(String(f.bot_auto_resume_minutes));
  const [grand, setGrand] = useState(f.plan_grandfathered);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setSaved("");
    try {
      const res = await patchClientFlags(clientId, {
        router_v2_enabled: router === "default" ? null : router === "true",
        daily_message_limit: Number(daily),
        bot_auto_resume_minutes: Number(resume),
        plan_grandfathered: grand,
        reason: reason.trim(),
      });
      const n = Object.keys(res.changed).length;
      setSaved(n ? `Saved ${n} change${n > 1 ? "s" : ""}.` : "No changes.");
      setReason("");
      if (n) onChanged();
    } catch (err) {
      setError(problemFrom(err).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Operational flags">
      <form onSubmit={save} className="space-y-3">
        <Field label="LLM intent router" hint="“Platform default” follows ROUTER_V2_CLIENT_IDS">
          <select className={inputCls} value={router} onChange={(e) => setRouter(e.target.value)} disabled={!canWrite}>
            <option value="default">Platform default</option>
            <option value="true">Force on</option>
            <option value="false">Force off</option>
          </select>
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Daily message limit">
            <input className={inputCls} type="number" min={0} value={daily} onChange={(e) => setDaily(e.target.value)} disabled={!canWrite} />
          </Field>
          <Field label="Bot auto-resume (min)">
            <input className={inputCls} type="number" min={1} value={resume} onChange={(e) => setResume(e.target.value)} disabled={!canWrite} />
          </Field>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={grand} onChange={(e) => setGrand(e.target.checked)} disabled={!canWrite} /> Plan grandfathered
        </label>
        {canWrite && (
          <>
            <Field label="Reason (optional)">
              <input className={inputCls} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} />
            </Field>
            <ErrorNote message={error} />
            {saved && <p className="text-sm text-emerald-700">{saved}</p>}
            <Button type="submit" disabled={busy}>{busy && <Loader2 size={14} className="animate-spin" />} Save flags</Button>
          </>
        )}
      </form>
    </Card>
  );
}

function ImpersonateDialog({ clientId, onClose }: { clientId: number; onClose: () => void }) {
  return (
    <ReasonDialog
      title="Sign in as this client"
      confirmLabel="Sign in as client"
      description="Opens the client's dashboard in a new tab as their owner for 30 minutes. This replaces any client session already open in this browser. Every impersonation is logged with your reason."
      onSubmit={async (reason) => {
        const res = await impersonateClient(clientId, reason);
        try {
          localStorage.setItem("access_token", res.access_token);
        } catch {
          throw new Error("Browser storage is unavailable.");
        }
        window.open("/dashboard", "_blank", "noopener");
      }}
      onClose={onClose}
    />
  );
}

function GrantDialog({ clientId, onDone, onClose }: { clientId: number; onDone: () => void; onClose: () => void }) {
  const plans = useAsync(getBillingPlans, []);
  const [code, setCode] = useState("");
  const [days, setDays] = useState("");
  const [amount, setAmount] = useState("");
  const chosen = code || plans.data?.find((p) => p.is_active)?.code || "";

  return (
    <ReasonDialog
      title="Grant a plan"
      confirmLabel="Grant"
      minReason={3}
      description="Record an offline payment or goodwill grant. The period is stacked after whatever the client already has."
      onSubmit={async (reason) => {
        await grantSubscription(clientId, {
          plan_code: chosen,
          days: days ? Number(days) : undefined,
          amount_paise: amount ? Math.round(Number(amount) * 100) : undefined,
          reason,
        });
        onDone();
      }}
      onClose={onClose}
    >
      {plans.loading ? (
        <Spinner />
      ) : (
        <div className="space-y-3">
          <Field label="Plan">
            <select className={inputCls} value={chosen} onChange={(e) => setCode(e.target.value)}>
              {(plans.data ?? []).filter((p) => p.is_active).map((p) => (
                <option key={p.code} value={p.code}>{p.name} ({fmtNum(p.conversation_limit)} conversations)</option>
              ))}
            </select>
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Days" hint="Blank = plan's period">
              <input className={inputCls} type="number" min={1} max={3660} value={days} onChange={(e) => setDays(e.target.value)} />
            </Field>
            <Field label="Amount received (₹, incl. GST)" hint="Blank = ₹0 grant, no invoice">
              <input className={inputCls} type="number" min={1} step="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} />
            </Field>
          </div>
        </div>
      )}
    </ReasonDialog>
  );
}

function ConversationsTab({ clientId }: { clientId: number }) {
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<ConversationRow | null>(null);
  const { data, loading, error, reload } = useAsync(() => getClientConversations(clientId, page), [clientId, page]);

  return (
    <Card>
      <ErrorNote message={error} onRetry={reload} />
      {loading && !data ? <Spinner /> : data && data.items.length === 0 ? <Empty>No conversations yet.</Empty> : data && (
        <>
          <Table head={["Customer", "Channel", "Stage", "Bot", "Messages", "Last activity", ""]}>
            {data.items.map((c) => (
              <tr key={c.id} className="hover:bg-gray-50">
                <Td>{c.customer_name || "—"}<div className="text-xs text-gray-500">{c.phone_number}</div></Td>
                <Td>{c.channel}</Td>
                <Td>{c.current_stage}</Td>
                <Td>{c.ai_enabled ? <Badge tone="green">On</Badge> : <Badge tone="amber">Paused{c.bot_pause_source ? ` (${c.bot_pause_source})` : ""}</Badge>}</Td>
                <Td className="tabular-nums">{c.message_count}</Td>
                <Td className="whitespace-nowrap text-gray-500">{fmtDate(c.last_message_at ?? c.created_at)}</Td>
                <Td><Button variant="ghost" onClick={() => setOpen(c)}><Eye size={14} /> View</Button></Td>
              </tr>
            ))}
          </Table>
          <Pagination page={page} pageSize={data.page_size} total={data.total} onPage={setPage} />
        </>
      )}
      {open && <TranscriptModal clientId={clientId} conversation={open} onClose={() => setOpen(null)} />}
    </Card>
  );
}

function TranscriptModal({ clientId, conversation, onClose }: { clientId: number; conversation: ConversationRow; onClose: () => void }) {
  const { data, loading, error } = useAsync(() => getConversationMessages(clientId, conversation.id), [clientId, conversation.id]);
  return (
    <Modal title={`Conversation with ${conversation.customer_name || conversation.phone_number}`} onClose={onClose} wide>
      <p className="text-xs text-gray-500 mb-3">Viewing a customer's messages is recorded in the audit log. Newest 200 messages.</p>
      <ErrorNote message={error} />
      {loading ? <Spinner /> : (
        <div className="space-y-2">
          {(data ?? []).map((m) => {
            const mine = m.role === "user";
            return (
              <div key={m.id} className={`flex ${mine ? "justify-start" : "justify-end"}`}>
                <div className={`max-w-[80%] rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap ${mine ? "bg-gray-100" : "bg-emerald-50"}`}>
                  {m.content || (m.media_type ? `[${m.media_type}]` : "")}
                  <div className="text-[10px] text-gray-400 mt-1">{m.sender_type ?? m.role} · {fmtDate(m.created_at)}</div>
                </div>
              </div>
            );
          })}
          {data && data.length === 0 && <Empty>No messages.</Empty>}
        </div>
      )}
    </Modal>
  );
}

function OrdersTab({ clientId }: { clientId: number }) {
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const { data, loading, error, reload } = useAsync(() => getClientOrders(clientId, status, page), [clientId, status, page]);
  return (
    <Card>
      <div className="mb-3">
        <select className={`${inputCls} w-auto`} value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }} aria-label="Order status">
          <option value="">All statuses</option>
          {["pending_payment", "payment_submitted", "paid", "cancelled", "new", "dispatched", "delivered"].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>
      <ErrorNote message={error} onRetry={reload} />
      {loading && !data ? <Spinner /> : data && data.items.length === 0 ? <Empty>No orders.</Empty> : data && (
        <>
          <Table head={["Order", "Customer", "Item", "Total", "Payment", "Status", "Created"]}>
            {data.items.map((o) => (
              <tr key={o.id}>
                <Td className="font-medium">{o.order_number}</Td>
                <Td>{o.customer_name}<div className="text-xs text-gray-500">{o.customer_phone}</div></Td>
                <Td>{o.product_name} × {o.quantity}</Td>
                <Td className="tabular-nums">{fmtInr(o.total_amount, 2)}</Td>
                <Td>{o.payment_method} <Badge tone={statusTone(o.payment_status)}>{o.payment_status}</Badge></Td>
                <Td><Badge tone={statusTone(o.status)}>{o.status}</Badge></Td>
                <Td className="whitespace-nowrap text-gray-500">{fmtDate(o.created_at)}</Td>
              </tr>
            ))}
          </Table>
          <Pagination page={page} pageSize={data.page_size} total={data.total} onPage={setPage} />
        </>
      )}
    </Card>
  );
}
