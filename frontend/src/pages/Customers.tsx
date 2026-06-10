import { useEffect, useState } from "react";
import {
  getCustomers,
  getCustomerStats,
  updateCustomer,
  toggleCustomerVip,
  toggleCustomerBlock,
  sendCustomerMessage,
  getCustomerOrders,
  getCustomerConversations,
  type CustomerOrderRecord,
  type CustomerConversationRecord,
} from "../api/client";
import Layout from "../components/Layout";
import {
  Users,
  Search,
  Star,
  Ban,
  MessageSquare,
  ChevronDown,
  X,
  IndianRupee,
  ShoppingCart,
  Clock,
  Crown,
} from "lucide-react";

// ── Types ──────────────────────────────────────────────────────────────────────

interface Customer {
  id: number;
  client_id: number;
  phone: string;
  name: string | null;
  email: string | null;
  address: string | null;
  total_orders: number;
  total_spent: number;
  last_order_at: string | null;
  first_message_at: string | null;
  last_message_at: string | null;
  preferred_language: string;
  preferred_payment: string | null;
  is_vip: boolean;
  is_blocked: boolean;
  tags: string | null;
  notes: string | null;
  created_at: string | null;
}

interface CustomerStats {
  total_customers: number;
  active_this_month: number;
  vip_customers: number;
  avg_order_value: number;
}

type CustomerOrder = CustomerOrderRecord;
type CustomerConversation = CustomerConversationRecord;

// ── Helpers ────────────────────────────────────────────────────────────────────

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30);
  return `${months}mo ago`;
}

function avatarColor(phone: string): string {
  const colors = [
    "bg-indigo-500", "bg-purple-500", "bg-pink-500", "bg-rose-500",
    "bg-orange-500", "bg-amber-500", "bg-emerald-500", "bg-teal-500",
    "bg-cyan-500", "bg-blue-500",
  ];
  const idx = phone.split("").reduce((sum, c) => sum + c.charCodeAt(0), 0) % colors.length;
  return colors[idx];
}

function initials(customer: Customer): string {
  if (customer.name) {
    return customer.name.split(" ").map((w) => w[0]).slice(0, 2).join("").toUpperCase();
  }
  return customer.phone.slice(-2);
}

const STATUS_STYLES: Record<string, string> = {
  new: "bg-blue-100 text-blue-700",
  confirmed: "bg-indigo-100 text-indigo-700",
  paid: "bg-green-100 text-green-700",
  dispatched: "bg-purple-100 text-purple-700",
  delivered: "bg-emerald-100 text-emerald-700",
  cancelled: "bg-red-100 text-red-700",
};

// ── Stat Card ─────────────────────────────────────────────────────────────────

function StatCard({
  label,
  value,
  icon: Icon,
  color,
}: {
  label: string;
  value: string | number;
  icon: React.ElementType;
  color: string;
}) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-gray-500">{label}</p>
          <p className="text-2xl font-bold text-gray-900 mt-1">{value}</p>
        </div>
        <div className={`w-12 h-12 rounded-xl flex items-center justify-center ${color}`}>
          <Icon size={22} className="text-white" />
        </div>
      </div>
    </div>
  );
}

// ── Send Message Modal ─────────────────────────────────────────────────────────

function SendMessageModal({
  customer,
  onClose,
}: {
  customer: Customer;
  onClose: () => void;
}) {
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);

  async function handleSend() {
    if (!message.trim()) return;
    setSending(true);
    try {
      await sendCustomerMessage(customer.id, message.trim());
      setSent(true);
      setTimeout(onClose, 1200);
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-semibold text-gray-900">
            Send Message to {customer.name || customer.phone}
          </h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
            <X size={20} />
          </button>
        </div>
        <textarea
          className="w-full border border-gray-200 rounded-lg p-3 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-indigo-500"
          rows={4}
          placeholder="Type your WhatsApp message..."
          value={message}
          onChange={(e) => setMessage(e.target.value)}
        />
        <div className="flex gap-3 mt-4">
          <button
            onClick={onClose}
            className="flex-1 px-4 py-2 rounded-lg border border-gray-200 text-sm text-gray-600 hover:bg-gray-50"
          >
            Cancel
          </button>
          <button
            onClick={handleSend}
            disabled={sending || sent || !message.trim()}
            className="flex-1 px-4 py-2 rounded-lg bg-indigo-600 text-white text-sm font-medium hover:bg-indigo-700 disabled:opacity-50"
          >
            {sent ? "Sent ✓" : sending ? "Sending..." : "Send WhatsApp"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Customer Detail Modal ──────────────────────────────────────────────────────

function CustomerDetailModal({
  customer: initial,
  onClose,
  onUpdate,
}: {
  customer: Customer;
  onClose: () => void;
  onUpdate: (c: Customer) => void;
}) {
  const [customer, setCustomer] = useState(initial);
  const [tab, setTab] = useState<"orders" | "conversations">("orders");
  const [orders, setOrders] = useState<CustomerOrder[]>([]);
  const [convs, setConvs] = useState<CustomerConversation[]>([]);
  const [loading, setLoading] = useState(true);
  const [editNotes, setEditNotes] = useState(customer.notes || "");
  const [editName, setEditName] = useState(customer.name || "");
  const [savingNotes, setSavingNotes] = useState(false);
  const [showMessage, setShowMessage] = useState(false);

  useEffect(() => {
    Promise.all([
      getCustomerOrders(customer.id),
      getCustomerConversations(customer.id),
    ]).then(([o, c]) => {
      setOrders(o);
      setConvs(c);
      setLoading(false);
    });
  }, [customer.id]);

  async function handleSaveNotes() {
    setSavingNotes(true);
    const updated = await updateCustomer(customer.id, { notes: editNotes, name: editName || undefined });
    setCustomer(updated);
    onUpdate(updated);
    setSavingNotes(false);
  }

  async function handleToggleVip() {
    const updated = await toggleCustomerVip(customer.id);
    setCustomer(updated);
    onUpdate(updated);
  }

  const avgOrderVal = customer.total_orders > 0
    ? (customer.total_spent / customer.total_orders).toFixed(0)
    : "—";

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-3xl max-h-[90vh] overflow-hidden flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <div className="flex items-center gap-3">
            <div className={`w-12 h-12 rounded-full flex items-center justify-center text-white font-bold text-lg ${avatarColor(customer.phone)}`}>
              {initials(customer)}
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="font-semibold text-gray-900 text-lg">
                  {customer.name || "Unknown"}
                </span>
                {customer.is_vip && (
                  <Crown size={16} className="text-amber-500" />
                )}
              </div>
              <p className="text-sm text-gray-500">{customer.phone}</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setShowMessage(true)}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-indigo-600 text-white text-sm font-medium hover:bg-indigo-700"
            >
              <MessageSquare size={14} />
              Message
            </button>
            <button onClick={onClose} className="text-gray-400 hover:text-gray-600 ml-2">
              <X size={20} />
            </button>
          </div>
        </div>

        <div className="flex flex-1 overflow-hidden">
          {/* Left panel */}
          <div className="w-72 shrink-0 border-r border-gray-100 overflow-y-auto p-5 space-y-5">
            {/* Stats */}
            <div className="grid grid-cols-2 gap-3">
              {[
                { label: "Orders", value: customer.total_orders },
                { label: "Total Spent", value: `₹${customer.total_spent.toLocaleString("en-IN")}` },
                { label: "Avg Order", value: avgOrderVal === "—" ? "—" : `₹${avgOrderVal}` },
                { label: "Last Order", value: relativeTime(customer.last_order_at) },
              ].map(({ label, value }) => (
                <div key={label} className="bg-gray-50 rounded-lg p-3">
                  <p className="text-xs text-gray-500">{label}</p>
                  <p className="text-sm font-bold text-gray-900 mt-0.5">{value}</p>
                </div>
              ))}
            </div>

            {/* VIP toggle */}
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-gray-700 flex items-center gap-1.5">
                <Crown size={14} className="text-amber-500" /> VIP Customer
              </span>
              <button
                onClick={handleToggleVip}
                className={`relative w-11 h-6 rounded-full transition-colors ${
                  customer.is_vip ? "bg-amber-400" : "bg-gray-200"
                }`}
              >
                <span
                  className={`absolute top-1 w-4 h-4 rounded-full bg-white shadow transition-transform ${
                    customer.is_vip ? "translate-x-6" : "translate-x-1"
                  }`}
                />
              </button>
            </div>

            {/* Tags */}
            {customer.tags && (
              <div>
                <p className="text-xs text-gray-500 mb-1.5">Tags</p>
                <div className="flex flex-wrap gap-1">
                  {customer.tags.split(",").filter(Boolean).map((tag) => (
                    <span
                      key={tag}
                      className="px-2 py-0.5 rounded-full bg-indigo-100 text-indigo-700 text-xs font-medium"
                    >
                      {tag.trim()}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* Preferences */}
            <div className="space-y-1.5 text-sm">
              <div className="flex justify-between">
                <span className="text-gray-500">Language</span>
                <span className="font-medium capitalize">{customer.preferred_language}</span>
              </div>
              {customer.preferred_payment && (
                <div className="flex justify-between">
                  <span className="text-gray-500">Prefers</span>
                  <span className="font-medium">{customer.preferred_payment}</span>
                </div>
              )}
              <div className="flex justify-between">
                <span className="text-gray-500">First seen</span>
                <span className="font-medium">{relativeTime(customer.first_message_at)}</span>
              </div>
            </div>

            {/* Name edit */}
            <div>
              <p className="text-xs text-gray-500 mb-1">Name</p>
              <input
                className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
                placeholder="Customer name"
              />
            </div>

            {/* Notes */}
            <div>
              <p className="text-xs text-gray-500 mb-1">Notes</p>
              <textarea
                className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-indigo-500"
                rows={3}
                value={editNotes}
                onChange={(e) => setEditNotes(e.target.value)}
                placeholder="Add notes..."
              />
              <button
                onClick={handleSaveNotes}
                disabled={savingNotes}
                className="mt-1.5 w-full py-1.5 rounded-lg bg-indigo-600 text-white text-sm font-medium hover:bg-indigo-700 disabled:opacity-50"
              >
                {savingNotes ? "Saving..." : "Save"}
              </button>
            </div>
          </div>

          {/* Right panel */}
          <div className="flex-1 flex flex-col overflow-hidden">
            {/* Tabs */}
            <div className="flex border-b border-gray-100 px-5">
              {(["orders", "conversations"] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={`px-4 py-3 text-sm font-medium border-b-2 transition-colors capitalize ${
                    tab === t
                      ? "border-indigo-600 text-indigo-600"
                      : "border-transparent text-gray-500 hover:text-gray-700"
                  }`}
                >
                  {t}
                </button>
              ))}
            </div>

            <div className="flex-1 overflow-y-auto p-5">
              {loading ? (
                <div className="flex items-center justify-center h-32">
                  <div className="w-6 h-6 border-2 border-indigo-600 border-t-transparent rounded-full animate-spin" />
                </div>
              ) : tab === "orders" ? (
                orders.length === 0 ? (
                  <p className="text-sm text-gray-400 text-center py-10">No orders yet</p>
                ) : (
                  <div className="space-y-3">
                    {orders.map((o) => (
                      <div key={o.id} className="border border-gray-100 rounded-xl p-4">
                        <div className="flex items-center justify-between mb-2">
                          <span className="text-sm font-semibold text-gray-800">{o.order_number}</span>
                          <span className={`text-xs font-semibold px-2 py-0.5 rounded-full capitalize ${STATUS_STYLES[o.status] ?? "bg-gray-100 text-gray-600"}`}>
                            {o.status}
                          </span>
                        </div>
                        <p className="text-sm text-gray-600">{o.product_name} × {o.quantity}</p>
                        <div className="flex items-center justify-between mt-2">
                          <span className="text-sm font-bold text-emerald-600">₹{o.total_amount.toLocaleString("en-IN")}</span>
                          <span className="text-xs text-gray-400">{relativeTime(o.created_at)}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                )
              ) : convs.length === 0 ? (
                <p className="text-sm text-gray-400 text-center py-10">No conversations yet</p>
              ) : (
                <div className="space-y-3">
                  {convs.map((c) => (
                    <div key={c.id} className="border border-gray-100 rounded-xl p-4">
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-medium capitalize text-gray-800">{c.channel}</span>
                        <span className="text-xs px-2 py-0.5 rounded-full bg-gray-100 text-gray-600 capitalize">
                          {c.current_stage}
                        </span>
                      </div>
                      <p className="text-xs text-gray-400 mt-1">{relativeTime(c.updated_at || c.created_at)}</p>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {showMessage && (
        <SendMessageModal customer={customer} onClose={() => setShowMessage(false)} />
      )}
    </div>
  );
}

// ── Main Page ──────────────────────────────────────────────────────────────────

const FILTERS = ["all", "vip", "new", "inactive"] as const;
type FilterType = (typeof FILTERS)[number];

const SORTS = [
  { value: "latest", label: "Latest active" },
  { value: "most_orders", label: "Most orders" },
  { value: "highest_spent", label: "Highest spent" },
] as const;

export default function Customers() {
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [stats, setStats] = useState<CustomerStats | null>(null);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<FilterType>("all");
  const [sort, setSort] = useState<string>("latest");
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Customer | null>(null);
  const [showMessage, setShowMessage] = useState<Customer | null>(null);

  async function load() {
    setLoading(true);
    const [list, s] = await Promise.all([
      getCustomers({ search: search || undefined, filter: filter === "all" ? undefined : filter, sort }),
      getCustomerStats(),
    ]);
    setCustomers(list);
    setStats(s);
    setLoading(false);
  }

  useEffect(() => { load(); }, [search, filter, sort]);

  function handleUpdate(updated: Customer) {
    setCustomers((prev) => prev.map((c) => (c.id === updated.id ? updated : c)));
    if (selected?.id === updated.id) setSelected(updated);
  }

  async function handleToggleVip(e: React.MouseEvent, customer: Customer) {
    e.stopPropagation();
    const updated = await toggleCustomerVip(customer.id);
    handleUpdate(updated);
  }

  async function handleBlock(e: React.MouseEvent, customer: Customer) {
    e.stopPropagation();
    if (!confirm(`${customer.is_blocked ? "Unblock" : "Block"} ${customer.name || customer.phone}?`)) return;
    const updated = await toggleCustomerBlock(customer.id);
    setCustomers((prev) => prev.filter((c) => c.id !== updated.id || !updated.is_blocked));
  }

  return (
    <Layout>
      <div className="max-w-7xl mx-auto space-y-6">
        {/* Header */}
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Customers</h1>
          <p className="text-sm text-gray-500 mt-1">All contacts who have messaged you</p>
        </div>

        {/* Stats row */}
        {stats && (
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <StatCard label="Total Customers" value={stats.total_customers} icon={Users} color="bg-indigo-500" />
            <StatCard label="Active This Month" value={stats.active_this_month} icon={Clock} color="bg-emerald-500" />
            <StatCard label="VIP Customers" value={stats.vip_customers} icon={Crown} color="bg-amber-500" />
            <StatCard
              label="Avg Order Value"
              value={stats.avg_order_value > 0 ? `₹${stats.avg_order_value.toLocaleString("en-IN")}` : "—"}
              icon={IndianRupee}
              color="bg-purple-500"
            />
          </div>
        )}

        {/* Filter bar */}
        <div className="flex flex-wrap items-center gap-3">
          {/* Search */}
          <div className="relative flex-1 min-w-[200px] max-w-xs">
            <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              className="w-full pl-9 pr-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="Search name or phone..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>

          {/* Filter pills */}
          <div className="flex gap-1.5">
            {FILTERS.map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={`px-3 py-1.5 rounded-lg text-sm font-medium capitalize transition-colors ${
                  filter === f
                    ? "bg-indigo-600 text-white"
                    : "bg-white border border-gray-200 text-gray-600 hover:bg-gray-50"
                }`}
              >
                {f}
              </button>
            ))}
          </div>

          {/* Sort */}
          <div className="relative ml-auto">
            <select
              className="appearance-none pl-3 pr-8 py-2 border border-gray-200 rounded-lg text-sm text-gray-700 bg-white focus:outline-none focus:ring-2 focus:ring-indigo-500"
              value={sort}
              onChange={(e) => setSort(e.target.value)}
            >
              {SORTS.map((s) => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
            <ChevronDown size={14} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
          </div>
        </div>

        {/* Table */}
        <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
          {loading ? (
            <div className="flex items-center justify-center h-48">
              <div className="w-8 h-8 border-2 border-indigo-600 border-t-transparent rounded-full animate-spin" />
            </div>
          ) : customers.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-48 text-gray-400">
              <Users size={40} className="mb-3 opacity-30" />
              <p className="text-sm">No customers found</p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-100 text-xs font-semibold text-gray-500 uppercase tracking-wider">
                    <th className="text-left px-5 py-3">Customer</th>
                    <th className="text-left px-4 py-3 hidden md:table-cell">Phone</th>
                    <th className="text-center px-4 py-3">Orders</th>
                    <th className="text-right px-4 py-3">Total Spent</th>
                    <th className="text-left px-4 py-3 hidden lg:table-cell">Last Active</th>
                    <th className="text-left px-4 py-3 hidden lg:table-cell">Tags</th>
                    <th className="text-right px-4 py-3">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {customers.map((c) => (
                    <tr
                      key={c.id}
                      onClick={() => setSelected(c)}
                      className="border-b border-gray-50 last:border-0 hover:bg-indigo-50/30 cursor-pointer transition-colors"
                    >
                      {/* Customer */}
                      <td className="px-5 py-3.5">
                        <div className="flex items-center gap-3">
                          <div className={`w-9 h-9 rounded-full flex items-center justify-center text-white text-xs font-bold shrink-0 ${avatarColor(c.phone)}`}>
                            {initials(c)}
                          </div>
                          <div>
                            <div className="flex items-center gap-1.5">
                              <span className="font-medium text-gray-900">
                                {c.name || <span className="text-gray-400 italic">Unknown</span>}
                              </span>
                              {c.is_vip && <Crown size={12} className="text-amber-500" />}
                            </div>
                            <p className="text-xs text-gray-400 md:hidden">{c.phone}</p>
                          </div>
                        </div>
                      </td>

                      {/* Phone */}
                      <td className="px-4 py-3.5 text-gray-600 hidden md:table-cell">{c.phone}</td>

                      {/* Orders */}
                      <td className="px-4 py-3.5 text-center">
                        <span className={`inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold ${
                          c.total_orders > 0 ? "bg-indigo-100 text-indigo-700" : "bg-gray-100 text-gray-500"
                        }`}>
                          <ShoppingCart size={11} />
                          {c.total_orders}
                        </span>
                      </td>

                      {/* Total spent */}
                      <td className="px-4 py-3.5 text-right font-bold text-emerald-600">
                        {c.total_spent > 0 ? `₹${c.total_spent.toLocaleString("en-IN")}` : "—"}
                      </td>

                      {/* Last active */}
                      <td className="px-4 py-3.5 text-gray-500 hidden lg:table-cell">
                        {relativeTime(c.last_message_at)}
                      </td>

                      {/* Tags */}
                      <td className="px-4 py-3.5 hidden lg:table-cell">
                        <div className="flex flex-wrap gap-1">
                          {c.is_vip && (
                            <span className="px-2 py-0.5 rounded-full bg-amber-100 text-amber-700 text-xs font-medium">VIP</span>
                          )}
                          {c.tags?.split(",").filter(Boolean).slice(0, 2).map((tag) => (
                            <span key={tag} className="px-2 py-0.5 rounded-full bg-gray-100 text-gray-600 text-xs">
                              {tag.trim()}
                            </span>
                          ))}
                        </div>
                      </td>

                      {/* Actions */}
                      <td className="px-4 py-3.5">
                        <div className="flex items-center justify-end gap-1.5" onClick={(e) => e.stopPropagation()}>
                          <button
                            onClick={(e) => { e.stopPropagation(); setShowMessage(c); }}
                            title="Send message"
                            className="p-1.5 rounded-lg text-gray-400 hover:text-indigo-600 hover:bg-indigo-50 transition-colors"
                          >
                            <MessageSquare size={15} />
                          </button>
                          <button
                            onClick={(e) => handleToggleVip(e, c)}
                            title={c.is_vip ? "Remove VIP" : "Mark VIP"}
                            className={`p-1.5 rounded-lg transition-colors ${
                              c.is_vip
                                ? "text-amber-500 hover:bg-amber-50"
                                : "text-gray-400 hover:text-amber-500 hover:bg-amber-50"
                            }`}
                          >
                            <Star size={15} />
                          </button>
                          <button
                            onClick={(e) => handleBlock(e, c)}
                            title={c.is_blocked ? "Unblock" : "Block"}
                            className="p-1.5 rounded-lg text-gray-400 hover:text-red-500 hover:bg-red-50 transition-colors"
                          >
                            <Ban size={15} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {selected && (
        <CustomerDetailModal
          customer={selected}
          onClose={() => setSelected(null)}
          onUpdate={handleUpdate}
        />
      )}

      {showMessage && (
        <SendMessageModal customer={showMessage} onClose={() => setShowMessage(null)} />
      )}
    </Layout>
  );
}
