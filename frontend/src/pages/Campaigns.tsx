import { useEffect, useRef, useState } from "react";
import {
  CampaignDetail,
  CampaignSummary,
  addRecipients,
  createCampaign,
  getCampaignDetail,
  getCampaigns,
  scheduleCampaign,
  sendCampaign,
} from "../api/client";
import Layout from "../components/Layout";
import { useAuth } from "../context/AuthContext";
import { Plus, X, Megaphone } from "lucide-react";

// ── Helpers ───────────────────────────────────────────────────────────────────

const STATUS_BADGE: Record<string, string> = {
  draft:     "bg-gray-100 text-gray-600",
  scheduled: "bg-blue-100 text-blue-700",
  running:   "bg-amber-100 text-amber-700",
  completed: "bg-green-100 text-green-700",
  failed:    "bg-red-100 text-red-700",
};

const RECIPIENT_BADGE: Record<string, string> = {
  pending:   "bg-gray-100 text-gray-500",
  sent:      "bg-green-100 text-green-700",
  failed:    "bg-red-100 text-red-600",
  delivered: "bg-indigo-100 text-indigo-700",
};

function fmt(d: string | null) {
  if (!d) return "—";
  return new Date(d).toLocaleString("en-IN", {
    day: "2-digit", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function Badge({ status, map }: { status: string; map: Record<string, string> }) {
  const cls = map[status] ?? "bg-gray-100 text-gray-500";
  return <span className={`inline-block px-2.5 py-1 rounded-full text-xs font-medium capitalize ${cls}`}>{status}</span>;
}

function ProgressBar({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0;
  return (
    <div className="w-full bg-gray-100 rounded-full h-2">
      <div className="bg-indigo-500 h-2 rounded-full transition-all" style={{ width: `${pct}%` }} />
    </div>
  );
}

// ── New-campaign modal ────────────────────────────────────────────────────────

type Step = "compose" | "recipients" | "schedule";

function NewCampaignModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [step, setStep] = useState<Step>("compose");
  const [name, setName] = useState("");
  const [template, setTemplate] = useState("");
  const [recipientMode, setRecipientMode] = useState<"import" | "manual" | "csv">("import");
  const [manualRows, setManualRows] = useState([{ phone: "", name: "" }]);
  const [scheduleMode, setScheduleMode] = useState<"now" | "later">("now");
  const [scheduledAt, setScheduledAt] = useState("");
  const [campaignId, setCampaignId] = useState<number | null>(null);
  const [totalRecipients, setTotalRecipients] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const charCount = template.length;
  const previewText = template.replace("{name}", manualRows[0]?.name || "Customer");

  async function handleCompose() {
    if (!name.trim() || !template.trim()) { setError("Campaign name and message are required."); return; }
    setBusy(true); setError("");
    try {
      const c = await createCampaign({ name, message_template: template });
      setCampaignId(c.id);
      setStep("recipients");
    } catch (e: unknown) {
      setError((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to create campaign.");
    } finally { setBusy(false); }
  }

  async function handleRecipients() {
    if (!campaignId) return;
    setBusy(true); setError("");
    try {
      let result;
      if (recipientMode === "import") {
        result = await addRecipients(campaignId, { import_from_conversations: true });
      } else {
        const valid = manualRows.filter((r) => r.phone.trim());
        if (!valid.length) { setError("Add at least one phone number."); setBusy(false); return; }
        result = await addRecipients(campaignId, { recipients: valid.map((r) => ({ phone: r.phone.trim(), name: r.name.trim() || undefined })) });
      }
      setTotalRecipients(result.total);
      setStep("schedule");
    } catch (e: unknown) {
      setError((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to add recipients.");
    } finally { setBusy(false); }
  }

  function handleCsv(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      const text = ev.target?.result as string;
      const rows = text.split("\n").map((l) => l.trim()).filter(Boolean).map((line) => {
        const [phone = "", name = ""] = line.split(",");
        return { phone: phone.trim(), name: name.trim() };
      }).filter((r) => r.phone);
      setManualRows(rows);
      setRecipientMode("csv");
    };
    reader.readAsText(file);
  }

  async function handleSend() {
    if (!campaignId) return;
    setBusy(true); setError("");
    try {
      if (scheduleMode === "now") {
        await sendCampaign(campaignId);
      } else {
        if (!scheduledAt) { setError("Choose a date and time to schedule."); setBusy(false); return; }
        await scheduleCampaign(campaignId, new Date(scheduledAt).toISOString());
      }
      onCreated();
    } catch (e: unknown) {
      setError((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to launch campaign.");
    } finally { setBusy(false); }
  }

  const COST_PER_MSG = 0.78;
  const estimatedCost = (totalRecipients * COST_PER_MSG).toFixed(2);
  const STEPS: Step[] = ["compose", "recipients", "schedule"];

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/50 backdrop-blur-sm">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-2xl mx-4 flex flex-col max-h-[90vh] overflow-hidden">
        <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
          <h2 className="text-lg font-bold text-gray-900">New Campaign</h2>
          <button onClick={onClose} className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-gray-100 text-gray-400 transition-colors">
            <X size={18} />
          </button>
        </div>

        {/* Step indicator */}
        <div className="flex border-b border-gray-100">
          {STEPS.map((s, i) => (
            <div key={s} className={`flex-1 py-3 text-xs font-medium text-center capitalize ${
              step === s ? "border-b-2 border-indigo-600 text-indigo-600"
                : i < STEPS.indexOf(step) ? "text-green-600"
                : "text-gray-400"
            }`}>
              {i + 1}. {s}
            </div>
          ))}
        </div>

        <div className="overflow-y-auto flex-1 px-6 py-5 flex flex-col gap-4">
          {error && <div className="bg-red-50 text-red-700 text-sm rounded-lg px-4 py-2.5 border border-red-100">{error}</div>}

          {step === "compose" && (
            <>
              <div>
                <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Campaign name</label>
                <input type="text" value={name} onChange={(e) => setName(e.target.value)}
                  className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  placeholder="Diwali Sale Announcement" />
              </div>
              <div>
                <div className="flex items-center justify-between mb-1.5">
                  <label className="text-xs font-medium uppercase tracking-wide text-gray-400">Message template</label>
                  <span className={`text-xs ${charCount > 1024 ? "text-red-500" : "text-gray-400"}`}>{charCount}/1024</span>
                </div>
                <p className="text-xs text-gray-400 mb-2">Use <code className="bg-gray-100 px-1 rounded">{"{name}"}</code> to personalise.</p>
                <textarea value={template} onChange={(e) => setTemplate(e.target.value)} rows={5} maxLength={1024}
                  className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 resize-none"
                  placeholder={"Namaste {name}! 🎉 Diwali special offer: 20% off on all sarees. Shop now!"} />
              </div>
              {template && (
                <div className="bg-gray-50 border border-gray-200 rounded-xl p-4">
                  <p className="text-xs font-medium text-gray-400 mb-1.5 uppercase tracking-wide">Preview</p>
                  <p className="text-sm text-gray-800 whitespace-pre-wrap">{previewText}</p>
                </div>
              )}
            </>
          )}

          {step === "recipients" && (
            <>
              <div className="flex gap-2">
                {(["import", "manual", "csv"] as const).map((m) => (
                  <button key={m} type="button" onClick={() => setRecipientMode(m)}
                    className={`flex-1 py-2.5 text-sm rounded-lg border font-medium transition-all ${
                      recipientMode === m ? "border-indigo-500 bg-indigo-50 text-indigo-700" : "border-gray-200 text-gray-600 hover:bg-gray-50"
                    }`}>
                    {m === "import" ? "Auto-import" : m === "manual" ? "Add manually" : "Upload CSV"}
                  </button>
                ))}
              </div>
              {recipientMode === "import" && (
                <div className="bg-blue-50 border border-blue-100 rounded-xl p-4 text-sm text-blue-700">
                  Automatically imports all WhatsApp numbers that have ever messaged your agent.
                </div>
              )}
              {recipientMode === "manual" && (
                <div className="flex flex-col gap-2">
                  {manualRows.map((row, i) => (
                    <div key={i} className="flex gap-2">
                      <input type="tel" placeholder="Phone (e.g. 919876543210)" value={row.phone}
                        onChange={(e) => setManualRows((r) => r.map((x, j) => j === i ? { ...x, phone: e.target.value } : x))}
                        className="flex-1 border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500" />
                      <input type="text" placeholder="Name (optional)" value={row.name}
                        onChange={(e) => setManualRows((r) => r.map((x, j) => j === i ? { ...x, name: e.target.value } : x))}
                        className="w-36 border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500" />
                      {manualRows.length > 1 && (
                        <button type="button" onClick={() => setManualRows((r) => r.filter((_, j) => j !== i))}
                          className="text-gray-400 hover:text-red-500 px-1">×</button>
                      )}
                    </div>
                  ))}
                  <button type="button" onClick={() => setManualRows((r) => [...r, { phone: "", name: "" }])}
                    className="self-start text-xs text-indigo-600 hover:text-indigo-800 font-medium">
                    + Add another
                  </button>
                </div>
              )}
              {recipientMode === "csv" && (
                <div className="flex flex-col gap-2">
                  <p className="text-xs text-gray-500">Format: <code className="bg-gray-100 px-1 rounded">phone,name</code> — one per line.</p>
                  <input ref={fileRef} type="file" accept=".csv,text/csv" onChange={handleCsv} className="text-sm text-gray-600" />
                  {manualRows.length > 0 && manualRows[0].phone && (
                    <p className="text-xs text-green-700 font-medium">{manualRows.length} recipient(s) parsed.</p>
                  )}
                </div>
              )}
            </>
          )}

          {step === "schedule" && (
            <>
              <div className="grid grid-cols-2 gap-3">
                {(["now", "later"] as const).map((m) => (
                  <button key={m} type="button" onClick={() => setScheduleMode(m)}
                    className={`py-3 rounded-xl border text-sm font-medium transition-all ${
                      scheduleMode === m ? "border-indigo-500 bg-indigo-50 text-indigo-700" : "border-gray-200 text-gray-600 hover:bg-gray-50"
                    }`}>
                    {m === "now" ? "🚀 Send now" : "📅 Schedule for later"}
                  </button>
                ))}
              </div>
              {scheduleMode === "later" && (
                <input type="datetime-local" value={scheduledAt} onChange={(e) => setScheduledAt(e.target.value)}
                  className="border border-gray-200 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500" />
              )}
              <div className="bg-amber-50 border border-amber-100 rounded-xl p-4">
                <p className="text-sm font-semibold text-amber-800">Estimated cost</p>
                <p className="text-3xl font-bold text-amber-700 mt-1">₹{estimatedCost}</p>
                <p className="text-xs text-amber-600 mt-1">{totalRecipients} recipients × ₹{COST_PER_MSG}/message</p>
              </div>
            </>
          )}
        </div>

        <div className="px-6 py-4 border-t border-gray-100 flex justify-between items-center">
          <button type="button"
            onClick={step === "compose" ? onClose : () => setStep(step === "schedule" ? "recipients" : "compose")}
            className="text-sm text-gray-500 hover:text-gray-700 font-medium">
            {step === "compose" ? "Cancel" : "← Back"}
          </button>
          <button type="button" disabled={busy || charCount > 1024}
            onClick={step === "compose" ? handleCompose : step === "recipients" ? handleRecipients : handleSend}
            className="bg-indigo-600 text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors">
            {busy ? "…" : step === "compose" ? "Next →" : step === "recipients" ? "Next →" : scheduleMode === "now" ? "Send campaign" : "Schedule"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Campaign detail panel ─────────────────────────────────────────────────────

function CampaignDetailPanel({ campaignId, onClose }: { campaignId: number; onClose: () => void }) {
  const [detail, setDetail] = useState<CampaignDetail | null>(null);
  const [filter, setFilter] = useState("all");

  useEffect(() => {
    getCampaignDetail(campaignId).then(setDetail);
    const id = setInterval(async () => {
      const d = await getCampaignDetail(campaignId);
      setDetail(d);
      if (d.status !== "running") clearInterval(id);
    }, 30_000);
    return () => clearInterval(id);
  }, [campaignId]);

  if (!detail) return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/50 backdrop-blur-sm">
      <div className="bg-white rounded-2xl p-8 text-gray-400 text-sm">Loading…</div>
    </div>
  );

  const pct = detail.total_recipients > 0 ? Math.round((detail.sent_count / detail.total_recipients) * 100) : 0;
  const filtered = filter === "all" ? detail.recipients : detail.recipients.filter((r) => r.status === filter);

  return (
    <div className="fixed inset-0 z-40 flex items-end sm:items-center justify-center bg-black/50 backdrop-blur-sm">
      <div className="bg-white rounded-t-2xl sm:rounded-2xl shadow-xl w-full max-w-3xl mx-0 sm:mx-4 flex flex-col max-h-[90vh] overflow-hidden">
        <div className="px-6 py-4 border-b border-gray-100 flex items-start justify-between">
          <div>
            <h2 className="text-lg font-bold text-gray-900">{detail.name}</h2>
            <div className="flex items-center gap-2 mt-1.5">
              <Badge status={detail.status} map={STATUS_BADGE} />
              {detail.status === "running" && <span className="text-xs text-amber-600 animate-pulse font-medium">● Live</span>}
            </div>
          </div>
          <button onClick={onClose} className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-gray-100 text-gray-400 transition-colors mt-1">
            <X size={18} />
          </button>
        </div>

        <div className="overflow-y-auto flex-1 px-6 py-5 flex flex-col gap-5">
          <div className="flex flex-col gap-2">
            <div className="flex justify-between text-sm text-gray-600">
              <span className="font-medium">Progress</span>
              <span className="font-semibold text-gray-900">{detail.sent_count}/{detail.total_recipients} ({pct}%)</span>
            </div>
            <ProgressBar value={detail.sent_count} max={detail.total_recipients} />
          </div>

          <div className="grid grid-cols-4 gap-3">
            {[
              { label: "Total", value: detail.total_recipients, cls: "text-gray-800" },
              { label: "Sent", value: detail.sent_count, cls: "text-green-600" },
              { label: "Failed", value: detail.failed_count, cls: "text-red-500" },
              { label: "Rate", value: `${pct}%`, cls: "text-indigo-600" },
            ].map((s) => (
              <div key={s.label} className="bg-gray-50 rounded-xl p-3.5 text-center">
                <p className={`text-2xl font-bold ${s.cls}`}>{s.value}</p>
                <p className="text-xs text-gray-400 mt-0.5 uppercase tracking-wide">{s.label}</p>
              </div>
            ))}
          </div>

          <div className="bg-gray-50 border border-gray-100 rounded-xl p-4">
            <p className="text-xs font-medium uppercase tracking-wide text-gray-400 mb-2">Message template</p>
            <p className="text-sm text-gray-800 whitespace-pre-wrap">{detail.message_template}</p>
          </div>

          <div>
            <div className="flex items-center gap-2 mb-3">
              <p className="text-sm font-semibold text-gray-900">Recipients</p>
              <div className="flex gap-1 ml-auto">
                {["all", "sent", "failed", "pending"].map((f) => (
                  <button key={f} onClick={() => setFilter(f)}
                    className={`px-2.5 py-1 rounded-lg text-xs font-medium capitalize transition-colors ${
                      filter === f ? "bg-indigo-100 text-indigo-700" : "text-gray-500 hover:bg-gray-100"
                    }`}>
                    {f}
                  </button>
                ))}
              </div>
            </div>
            <div className="border border-gray-100 rounded-xl overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-gray-50">
                  <tr>
                    {["Phone", "Name", "Status", "Sent at", "Error"].map((h) => (
                      <th key={h} className="text-left px-4 py-2.5 text-xs font-medium uppercase tracking-wide text-gray-400">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-50">
                  {filtered.length === 0 && (
                    <tr><td colSpan={5} className="text-center py-6 text-gray-400 text-xs">No recipients match the filter.</td></tr>
                  )}
                  {filtered.map((r) => (
                    <tr key={r.id} className="hover:bg-gray-50 transition-colors">
                      <td className="px-4 py-2.5 font-mono text-xs text-gray-600">{r.phone_number}</td>
                      <td className="px-4 py-2.5 text-gray-600 text-xs">{r.customer_name || "—"}</td>
                      <td className="px-4 py-2.5"><Badge status={r.status} map={RECIPIENT_BADGE} /></td>
                      <td className="px-4 py-2.5 text-xs text-gray-400">{fmt(r.sent_at)}</td>
                      <td className="px-4 py-2.5 text-xs text-red-500 max-w-[180px] truncate" title={r.error_message ?? ""}>{r.error_message || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function Campaigns() {
  const { client } = useAuth();
  const [campaigns, setCampaigns] = useState<CampaignSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [showNew, setShowNew] = useState(false);
  const [detailId, setDetailId] = useState<number | null>(null);

  const plan = client?.plan_slug ?? "starter";
  const canUseCampaigns = plan === "growth" || plan === "pro";

  async function load() {
    setLoading(true);
    try {
      const data = await getCampaigns();
      setCampaigns(data);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  const totalSent = campaigns.reduce((s, c) => s + c.sent_count, 0);
  const totalFailed = campaigns.reduce((s, c) => s + c.failed_count, 0);
  const totalDelivered = campaigns.reduce((s, c) => s + c.delivered_count, 0);
  const totalRecipients = campaigns.reduce((s, c) => s + c.total_recipients, 0);
  const overallRate = totalRecipients > 0 ? Math.round(totalSent / totalRecipients * 100) : 0;

  return (
    <Layout>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Campaigns</h1>
        {canUseCampaigns && (
          <button onClick={() => setShowNew(true)}
            className="flex items-center gap-2 bg-indigo-600 text-white rounded-lg px-4 py-2.5 text-sm font-semibold hover:bg-indigo-700 active:scale-95 transition-all shadow-sm">
            <Plus size={16} /> New Campaign
          </button>
        )}
      </div>

      {!canUseCampaigns && (
        <div className="bg-amber-50 border border-amber-200 rounded-xl p-6 text-center mb-6">
          <p className="text-amber-800 font-semibold text-sm">Broadcast campaigns require the <strong>Growth</strong> or <strong>Pro</strong> plan.</p>
          <p className="text-amber-600 text-xs mt-1">Upgrade to send campaigns to all your customers at once.</p>
        </div>
      )}

      {campaigns.length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-6">
          {[
            { label: "Total sent", value: totalSent.toLocaleString(), color: "text-gray-900" },
            { label: "Delivered", value: totalDelivered.toLocaleString(), color: "text-green-600" },
            { label: "Failed", value: totalFailed.toLocaleString(), color: "text-red-500" },
            { label: "Delivery rate", value: `${overallRate}%`, color: "text-indigo-600" },
          ].map((s) => (
            <div key={s.label} className="bg-white border border-gray-100 shadow-sm rounded-xl p-5">
              <p className={`text-2xl font-bold ${s.color}`}>{s.value}</p>
              <p className="text-xs text-gray-400 mt-0.5 uppercase tracking-wide">{s.label}</p>
            </div>
          ))}
        </div>
      )}

      {/* Campaign cards */}
      {loading ? (
        <div className="space-y-3">
          {[...Array(3)].map((_, i) => <div key={i} className="h-32 bg-white rounded-xl animate-pulse border border-gray-100" />)}
        </div>
      ) : campaigns.length === 0 ? (
        <div className="bg-white rounded-xl border border-gray-100 shadow-sm px-5 py-16 text-center">
          <Megaphone size={48} className="text-gray-200 mx-auto mb-3" />
          <p className="font-semibold text-gray-600">No campaigns yet</p>
          <p className="text-sm text-gray-400 mt-1">
            {canUseCampaigns ? 'Click "New Campaign" to send your first broadcast.' : "Upgrade to Growth or Pro to create campaigns."}
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {campaigns.map((c) => (
            <div key={c.id} className="bg-white rounded-xl border border-gray-100 shadow-sm p-5 hover:shadow-md transition-shadow">
              <div className="flex items-start justify-between gap-4 mb-4">
                <div>
                  <h3 className="font-semibold text-gray-900">{c.name}</h3>
                  <p className="text-xs text-gray-400 mt-0.5">{fmt(c.created_at)}</p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <Badge status={c.status} map={STATUS_BADGE} />
                  {c.status === "running" && <span className="text-xs text-amber-600 animate-pulse font-medium">● Live</span>}
                </div>
              </div>

              {c.total_recipients > 0 && (
                <div className="mb-4">
                  <div className="flex justify-between text-xs text-gray-500 mb-1.5">
                    <span>Progress</span>
                    <span className="font-medium">{c.sent_count}/{c.total_recipients}</span>
                  </div>
                  <ProgressBar value={c.sent_count} max={c.total_recipients} />
                </div>
              )}

              <div className="flex items-center gap-6 text-sm">
                <div><span className="font-semibold text-gray-900">{c.total_recipients}</span> <span className="text-gray-400 text-xs">recipients</span></div>
                <div><span className="font-semibold text-green-600">{c.sent_count}</span> <span className="text-gray-400 text-xs">sent</span></div>
                <div><span className="font-semibold text-indigo-600">{c.delivered_count}</span> <span className="text-gray-400 text-xs">delivered</span></div>
                <div><span className="font-semibold text-red-500">{c.failed_count}</span> <span className="text-gray-400 text-xs">failed</span></div>
                <button onClick={() => setDetailId(c.id)} className="ml-auto text-xs text-indigo-600 font-semibold hover:underline">
                  View details →
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {showNew && <NewCampaignModal onClose={() => setShowNew(false)} onCreated={() => { setShowNew(false); load(); }} />}
      {detailId !== null && <CampaignDetailPanel campaignId={detailId} onClose={() => setDetailId(null)} />}
    </Layout>
  );
}
