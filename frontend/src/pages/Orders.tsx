import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  getOrders,
  getOrderStats,
  updateOrderStatus,
  notifyCustomer,
  exportOrdersCSV,
} from "../api/client";
import Layout from "../components/Layout";
import type { Order, OrderStats } from "../types";
import {
  Package,
  Search,
  Download,
  X,
  ChevronDown,
  Truck,
  MessageSquare,
  IndianRupee,
  ShoppingCart,
  Clock,
  CheckCircle2,
} from "lucide-react";

// ── Helpers ───────────────────────────────────────────────────────────────────

const STATUS_STYLES: Record<string, string> = {
  new: "bg-blue-100 text-blue-700",
  confirmed: "bg-indigo-100 text-indigo-700",
  paid: "bg-green-100 text-green-700",
  processing: "bg-orange-100 text-orange-700",
  dispatched: "bg-purple-100 text-purple-700",
  delivered: "bg-emerald-100 text-emerald-700",
  cancelled: "bg-red-100 text-red-700",
};

const PAYMENT_STYLES: Record<string, string> = {
  COD: "bg-amber-100 text-amber-700",
  UPI: "bg-blue-100 text-blue-700",
};

const STATUS_ORDER = ["all", "new", "confirmed", "paid", "processing", "dispatched", "delivered", "cancelled"];
const COURIERS = ["Delhivery", "BlueDart", "DTDC", "India Post", "Ekart", "Other"];

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`text-xs font-semibold px-2 py-0.5 rounded-full capitalize ${STATUS_STYLES[status] ?? "bg-gray-100 text-gray-600"}`}>
      {status}
    </span>
  );
}

function PaymentBadge({ method }: { method: string }) {
  return (
    <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${PAYMENT_STYLES[method] ?? "bg-gray-100 text-gray-600"}`}>
      {method}
    </span>
  );
}

// ── Dispatch Modal ─────────────────────────────────────────────────────────────

function DispatchModal({
  order,
  onClose,
  onConfirm,
}: {
  order: Order;
  onClose: () => void;
  onConfirm: (tracking: string, courier: string) => Promise<void>;
}) {
  const [tracking, setTracking] = useState("");
  const [courier, setCourier] = useState(COURIERS[0]);
  const [saving, setSaving] = useState(false);

  async function handleConfirm() {
    setSaving(true);
    try {
      await onConfirm(tracking, courier);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-semibold text-gray-900">Mark as Dispatched</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><X size={18} /></button>
        </div>
        <p className="text-sm text-gray-500 mb-4">Order <span className="font-medium text-gray-800">{order.order_number}</span> — customer will be notified via WhatsApp.</p>

        <div className="space-y-3">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Courier</label>
            <select
              value={courier}
              onChange={(e) => setCourier(e.target.value)}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400"
            >
              {COURIERS.map((c) => <option key={c}>{c}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Tracking Number</label>
            <input
              value={tracking}
              onChange={(e) => setTracking(e.target.value)}
              placeholder="e.g. DL123456789"
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400"
            />
          </div>
        </div>

        <div className="flex gap-3 mt-5">
          <button onClick={onClose} className="flex-1 py-2 rounded-lg border border-gray-200 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
          <button
            onClick={handleConfirm}
            disabled={saving}
            className="flex-1 py-2 rounded-lg bg-indigo-600 text-white text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50"
          >
            {saving ? "Saving…" : "Confirm & Notify"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Order Detail Modal ─────────────────────────────────────────────────────────

function OrderDetailModal({
  order,
  onClose,
  onStatusChange,
}: {
  order: Order;
  onClose: () => void;
  onStatusChange: (id: number, status: string, tracking?: string, courier?: string) => Promise<void>;
}) {
  const [customMsg, setCustomMsg] = useState("");
  const [sending, setSending] = useState(false);
  const [msgSent, setMsgSent] = useState(false);
  const [showDispatch, setShowDispatch] = useState(false);

  async function handleSendMsg() {
    if (!customMsg.trim()) return;
    setSending(true);
    try {
      await notifyCustomer(order.id, customMsg);
      setMsgSent(true);
      setCustomMsg("");
      setTimeout(() => setMsgSent(false), 3000);
    } finally {
      setSending(false);
    }
  }

  const timeline = [
    { label: "Order placed", time: order.created_at },
    { label: "Confirmed", time: order.confirmed_at },
    { label: "Paid", time: order.paid_at },
    { label: "Dispatched", time: order.dispatched_at },
    { label: "Delivered", time: order.delivered_at },
  ].filter((t) => t.time);

  return (
    <>
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
        <div className="bg-white rounded-2xl shadow-xl w-full max-w-xl max-h-[90vh] overflow-y-auto">
          <div className="sticky top-0 bg-white border-b border-gray-100 px-6 py-4 flex items-center justify-between">
            <div>
              <h3 className="font-semibold text-gray-900">{order.order_number}</h3>
              <p className="text-xs text-gray-400">{relativeTime(order.created_at)}</p>
            </div>
            <div className="flex items-center gap-3">
              <StatusBadge status={order.status} />
              <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><X size={18} /></button>
            </div>
          </div>

          <div className="px-6 py-5 space-y-5">
            {/* Customer + product */}
            <div className="grid grid-cols-2 gap-4">
              <div>
                <p className="text-xs text-gray-400 font-medium uppercase tracking-wide mb-1">Customer</p>
                <p className="text-sm font-semibold text-gray-800">{order.customer_name}</p>
                <p className="text-xs text-gray-500">{order.customer_phone}</p>
              </div>
              <div>
                <p className="text-xs text-gray-400 font-medium uppercase tracking-wide mb-1">Payment</p>
                <PaymentBadge method={order.payment_method} />
                <p className="text-xs text-gray-500 mt-1 capitalize">{order.payment_status}</p>
              </div>
            </div>

            <div>
              <p className="text-xs text-gray-400 font-medium uppercase tracking-wide mb-1">Product</p>
              <p className="text-sm font-semibold text-gray-800">{order.product_name}</p>
              <p className="text-xs text-gray-500">
                {[order.product_sku, order.variant_color, order.variant_size].filter(Boolean).join(" · ")}
                {" — "}Qty {order.quantity} × ₹{order.unit_price.toLocaleString("en-IN")}
              </p>
              <p className="text-base font-bold text-indigo-600 mt-1">₹{order.total_amount.toLocaleString("en-IN")}</p>
            </div>

            <div>
              <p className="text-xs text-gray-400 font-medium uppercase tracking-wide mb-1">Delivery Address</p>
              <p className="text-sm text-gray-700">{order.delivery_address}</p>
            </div>

            {(order.tracking_number || order.courier_name) && (
              <div>
                <p className="text-xs text-gray-400 font-medium uppercase tracking-wide mb-1">Tracking</p>
                <p className="text-sm text-gray-700">
                  {order.courier_name && <span className="font-medium">{order.courier_name} — </span>}
                  {order.tracking_number}
                </p>
              </div>
            )}

            {order.notes && (
              <div>
                <p className="text-xs text-gray-400 font-medium uppercase tracking-wide mb-1">Notes</p>
                <p className="text-sm text-gray-700">{order.notes}</p>
              </div>
            )}

            {/* Timeline */}
            {timeline.length > 0 && (
              <div>
                <p className="text-xs text-gray-400 font-medium uppercase tracking-wide mb-2">Timeline</p>
                <div className="space-y-1">
                  {timeline.map((t) => (
                    <div key={t.label} className="flex items-center gap-2 text-xs text-gray-600">
                      <CheckCircle2 size={12} className="text-indigo-400 shrink-0" />
                      <span className="font-medium">{t.label}</span>
                      <span className="text-gray-400">{relativeTime(t.time)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Actions */}
            <div>
              <p className="text-xs text-gray-400 font-medium uppercase tracking-wide mb-2">Actions</p>
              <div className="flex flex-wrap gap-2">
                {order.status !== "paid" && order.status !== "cancelled" && (
                  <button
                    onClick={() => onStatusChange(order.id, "paid")}
                    className="text-xs px-3 py-1.5 rounded-lg bg-green-50 text-green-700 border border-green-200 hover:bg-green-100 font-medium"
                  >
                    Mark Paid
                  </button>
                )}
                {order.status === "confirmed" && (
                  <button
                    onClick={() => { setShowDispatch(true); }}
                    className="text-xs px-3 py-1.5 rounded-lg bg-purple-50 text-purple-700 border border-purple-200 hover:bg-purple-100 font-medium"
                  >
                    <Truck size={12} className="inline mr-1" />Dispatch
                  </button>
                )}
                {order.status === "dispatched" && (
                  <button
                    onClick={() => onStatusChange(order.id, "delivered")}
                    className="text-xs px-3 py-1.5 rounded-lg bg-emerald-50 text-emerald-700 border border-emerald-200 hover:bg-emerald-100 font-medium"
                  >
                    Mark Delivered
                  </button>
                )}
                {order.conversation_id && (
                  <Link
                    to="/conversations"
                    className="text-xs px-3 py-1.5 rounded-lg bg-indigo-50 text-indigo-700 border border-indigo-200 hover:bg-indigo-100 font-medium"
                  >
                    View Conversation
                  </Link>
                )}
              </div>
            </div>

            {/* Custom WhatsApp */}
            <div>
              <p className="text-xs text-gray-400 font-medium uppercase tracking-wide mb-2">Send WhatsApp</p>
              <div className="flex gap-2">
                <input
                  value={customMsg}
                  onChange={(e) => setCustomMsg(e.target.value)}
                  placeholder="Custom message to customer…"
                  className="flex-1 border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400"
                />
                <button
                  onClick={handleSendMsg}
                  disabled={sending || !customMsg.trim()}
                  className="px-4 py-2 rounded-lg bg-indigo-600 text-white text-xs font-semibold hover:bg-indigo-700 disabled:opacity-50"
                >
                  {msgSent ? "Sent!" : sending ? "…" : <MessageSquare size={14} />}
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      {showDispatch && (
        <DispatchModal
          order={order}
          onClose={() => setShowDispatch(false)}
          onConfirm={async (tracking, courier) => {
            await onStatusChange(order.id, "dispatched", tracking, courier);
            setShowDispatch(false);
            onClose();
          }}
        />
      )}
    </>
  );
}

// ── Action Dropdown ────────────────────────────────────────────────────────────

function ActionDropdown({
  order,
  onStatusChange,
  onViewDetail,
}: {
  order: Order;
  onStatusChange: (id: number, status: string, tracking?: string, courier?: string) => Promise<void>;
  onViewDetail: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [showDispatch, setShowDispatch] = useState(false);

  return (
    <>
      <div className="relative">
        <button
          onClick={() => setOpen((p) => !p)}
          className="flex items-center gap-1 text-xs px-2 py-1 rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50"
        >
          Actions <ChevronDown size={12} />
        </button>
        {open && (
          <div className="absolute right-0 mt-1 w-44 bg-white rounded-xl shadow-lg border border-gray-100 py-1 z-20">
            <button onClick={() => { onViewDetail(); setOpen(false); }} className="w-full text-left px-4 py-2 text-xs text-gray-700 hover:bg-gray-50">View Detail</button>
            {order.status !== "paid" && <button onClick={() => { onStatusChange(order.id, "paid"); setOpen(false); }} className="w-full text-left px-4 py-2 text-xs text-gray-700 hover:bg-gray-50">Mark as Paid</button>}
            {order.status !== "processing" && <button onClick={() => { onStatusChange(order.id, "processing"); setOpen(false); }} className="w-full text-left px-4 py-2 text-xs text-gray-700 hover:bg-gray-50">Mark as Processing</button>}
            {order.status !== "dispatched" && (
              <button onClick={() => { setShowDispatch(true); setOpen(false); }} className="w-full text-left px-4 py-2 text-xs text-gray-700 hover:bg-gray-50">Mark as Dispatched</button>
            )}
            {order.status !== "delivered" && <button onClick={() => { onStatusChange(order.id, "delivered"); setOpen(false); }} className="w-full text-left px-4 py-2 text-xs text-gray-700 hover:bg-gray-50">Mark as Delivered</button>}
            {order.conversation_id && (
              <Link to="/conversations" className="block px-4 py-2 text-xs text-gray-700 hover:bg-gray-50">View Conversation</Link>
            )}
          </div>
        )}
      </div>

      {showDispatch && (
        <DispatchModal
          order={order}
          onClose={() => setShowDispatch(false)}
          onConfirm={async (tracking, courier) => {
            await onStatusChange(order.id, "dispatched", tracking, courier);
            setShowDispatch(false);
          }}
        />
      )}
    </>
  );
}

// ── Main Page ──────────────────────────────────────────────────────────────────

export default function Orders() {
  const [orders, setOrders] = useState<Order[]>([]);
  const [stats, setStats] = useState<OrderStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [selectedOrder, setSelectedOrder] = useState<Order | null>(null);
  const [exporting, setExporting] = useState(false);

  async function load(status?: string, searchQ?: string) {
    setLoading(true);
    try {
      const [os, st] = await Promise.all([
        getOrders({ status: status === "all" ? undefined : status, search: searchQ || undefined }),
        getOrderStats(),
      ]);
      setOrders(os);
      setStats(st);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  function handleStatusTab(s: string) {
    setStatusFilter(s);
    load(s, search);
  }

  function handleSearch(e: React.ChangeEvent<HTMLInputElement>) {
    setSearch(e.target.value);
    load(statusFilter, e.target.value);
  }

  async function handleStatusChange(id: number, status: string, tracking?: string, courier?: string) {
    const updated = await updateOrderStatus(id, status, tracking, courier);
    setOrders((prev) => prev.map((o) => (o.id === id ? updated : o)));
    if (selectedOrder?.id === id) setSelectedOrder(updated);
  }

  async function handleExport() {
    setExporting(true);
    try {
      await exportOrdersCSV();
    } finally {
      setExporting(false);
    }
  }

  return (
    <Layout>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-gray-900">Orders</h1>
          <p className="text-sm text-gray-400 mt-0.5">Manage customer orders and dispatch</p>
        </div>
        <button
          onClick={handleExport}
          disabled={exporting}
          className="flex items-center gap-2 px-4 py-2 rounded-lg border border-gray-200 text-sm text-gray-600 hover:bg-gray-50 disabled:opacity-50"
        >
          <Download size={15} />
          {exporting ? "Exporting…" : "Export CSV"}
        </button>
      </div>

      {/* Stat cards */}
      {stats && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-6">
          {[
            { label: "Today's Orders", value: stats.today_orders, icon: ShoppingCart, color: "indigo" },
            { label: "Today's Revenue", value: `₹${stats.today_revenue.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`, icon: IndianRupee, color: "green" },
            { label: "Pending Dispatch", value: stats.pending_dispatch, icon: Clock, color: "amber" },
            { label: "COD Pending", value: stats.cod_pending, icon: Package, color: "purple" },
          ].map(({ label, value, icon: Icon, color }) => (
            <div key={label} className="bg-white rounded-xl border border-gray-100 shadow-sm px-5 py-4">
              <div className={`w-9 h-9 rounded-xl flex items-center justify-center mb-3 bg-${color}-50`}>
                <Icon size={18} className={`text-${color}-600`} />
              </div>
              <div className="text-2xl font-bold text-gray-900">{value}</div>
              <div className="text-xs text-gray-400 mt-0.5">{label}</div>
            </div>
          ))}
        </div>
      )}

      {/* Filters */}
      <div className="bg-white rounded-xl border border-gray-100 shadow-sm mb-4">
        {/* Status tabs */}
        <div className="flex gap-1 px-4 pt-4 overflow-x-auto">
          {STATUS_ORDER.map((s) => (
            <button
              key={s}
              onClick={() => handleStatusTab(s)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap transition-colors ${
                statusFilter === s
                  ? "bg-indigo-600 text-white"
                  : "text-gray-500 hover:bg-gray-100"
              }`}
            >
              {s.charAt(0).toUpperCase() + s.slice(1)}
            </button>
          ))}
        </div>

        {/* Search */}
        <div className="px-4 py-3">
          <div className="relative">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              value={search}
              onChange={handleSearch}
              placeholder="Search order number, customer name or phone…"
              className="w-full pl-9 pr-4 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400"
            />
          </div>
        </div>
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-gray-100 shadow-sm overflow-hidden">
        {loading ? (
          <div className="divide-y divide-gray-50">
            {[...Array(5)].map((_, i) => (
              <div key={i} className="h-14 animate-pulse bg-gray-50" />
            ))}
          </div>
        ) : orders.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-center">
            <Package size={40} className="text-gray-200 mb-3" />
            <p className="text-sm text-gray-400">No orders yet.</p>
            <p className="text-xs text-gray-300 mt-1">Orders are auto-created from completed conversations.</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gray-50 border-b border-gray-100">
                  <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wide">Order</th>
                  <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wide">Customer</th>
                  <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wide">Product</th>
                  <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wide">Qty</th>
                  <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wide">Amount</th>
                  <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wide">Payment</th>
                  <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wide">Status</th>
                  <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wide">Date</th>
                  <th className="px-4 py-3"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {orders.map((order) => (
                  <tr
                    key={order.id}
                    className="hover:bg-gray-50 transition-colors cursor-pointer"
                    onClick={() => setSelectedOrder(order)}
                  >
                    <td className="px-4 py-3 font-medium text-indigo-700 text-xs whitespace-nowrap">
                      {order.order_number}
                    </td>
                    <td className="px-4 py-3">
                      <div className="font-medium text-gray-800 text-xs">{order.customer_name}</div>
                      <div className="text-gray-400 text-xs">{order.customer_phone}</div>
                    </td>
                    <td className="px-4 py-3 max-w-[160px]">
                      <div className="font-medium text-gray-800 text-xs truncate">{order.product_name}</div>
                      <div className="text-gray-400 text-xs truncate">
                        {[order.product_sku, order.variant_color, order.variant_size].filter(Boolean).join(" · ")}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-600">{order.quantity}</td>
                    <td className="px-4 py-3 text-xs font-bold text-emerald-700 whitespace-nowrap">
                      ₹{order.total_amount.toLocaleString("en-IN")}
                    </td>
                    <td className="px-4 py-3">
                      <PaymentBadge method={order.payment_method} />
                    </td>
                    <td className="px-4 py-3">
                      <StatusBadge status={order.status} />
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-400 whitespace-nowrap">
                      {relativeTime(order.created_at)}
                    </td>
                    <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}>
                      <ActionDropdown
                        order={order}
                        onStatusChange={handleStatusChange}
                        onViewDetail={() => setSelectedOrder(order)}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Detail modal */}
      {selectedOrder && (
        <OrderDetailModal
          order={selectedOrder}
          onClose={() => setSelectedOrder(null)}
          onStatusChange={handleStatusChange}
        />
      )}
    </Layout>
  );
}
