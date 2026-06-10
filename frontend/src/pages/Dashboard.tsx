import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";
import {
  getLeads,
  getUsageStats,
  getOrders,
  getOrderStats,
} from "../api/client";
import Layout from "../components/Layout";
import StatCard from "../components/StatCard";
import type {
  Lead,
  UsageStat,
  Order,
  OrderStats,
} from "../types";
import {
  MessageSquare,
  Flame,
  Eye,
  Share2,
  Zap,
  ChevronRight,
  ShoppingCart,
  IndianRupee,
  Send,
  Package,
  FlaskConical,
  Clock,
  X,
  Copy,
} from "lucide-react";
import { useAuth } from "../context/AuthContext";

const APP_BASE_URL = (import.meta.env.VITE_APP_URL as string) || "http://localhost:5173";

// ── helpers ───────────────────────────────────────────────────────────────────

function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function firstWord(str: string): string {
  return str.split(/[\s,]/)[0] ?? str;
}

// ── Catalogue banner ──────────────────────────────────────────────────────────

function CatalogueBanner({ slug, businessName }: { slug: string; businessName: string }) {
  const [copied, setCopied] = useState(false);
  const catalogueUrl = `${APP_BASE_URL}/shop/${slug}`;

  function copyLink() {
    navigator.clipboard.writeText(catalogueUrl).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  async function share() {
    if (navigator.share) {
      try { await navigator.share({ title: businessName, text: "Browse our catalogue", url: catalogueUrl }); }
      catch { copyLink(); }
    } else {
      copyLink();
    }
  }

  return (
    <div className="flex items-center gap-3 bg-[#EEF2FF] border border-indigo-100 rounded-xl px-4 py-2.5">
      <div className="w-8 h-8 bg-indigo-100 rounded-full flex items-center justify-center shrink-0 text-base">
        🛍️
      </div>
      <code className="flex-1 min-w-0 text-xs text-indigo-700 font-mono truncate">
        /shop/{slug}
      </code>
      <div className="flex items-center gap-1.5 shrink-0">
        <button
          onClick={copyLink}
          title={copied ? "Copied!" : "Copy link"}
          className="w-8 h-8 flex items-center justify-center bg-white rounded-full shadow-sm border border-gray-100 hover:bg-indigo-50 hover:border-indigo-200 transition-colors"
        >
          <Copy size={13} className={copied ? "text-indigo-600" : "text-gray-400"} />
        </button>
        <button
          onClick={() => window.open(catalogueUrl, "_blank")}
          title="Preview"
          className="w-8 h-8 flex items-center justify-center bg-white rounded-full shadow-sm border border-gray-100 hover:bg-indigo-50 hover:border-indigo-200 transition-colors"
        >
          <Eye size={13} className="text-gray-400" />
        </button>
        <button
          onClick={share}
          title="Share"
          className="w-8 h-8 flex items-center justify-center bg-white rounded-full shadow-sm border border-gray-100 hover:bg-indigo-50 hover:border-indigo-200 transition-colors"
        >
          <Share2 size={13} className="text-gray-400" />
        </button>
      </div>
    </div>
  );
}

// ── WhatsApp connect banner ───────────────────────────────────────────────────

function ConnectWhatsAppBanner({ onDismiss }: { onDismiss: () => void }) {
  const navigate = useNavigate();
  return (
    <div className="bg-gradient-to-r from-orange-500 to-amber-500 rounded-2xl px-6 py-4 text-white shadow-md">
      <div className="flex items-center gap-4">
        <div className="w-10 h-10 bg-white/20 rounded-full flex items-center justify-center shrink-0">
          <Zap size={20} className="text-white" />
        </div>
        <div className="flex-1 min-w-0">
          <p className="font-bold text-white text-[15px] leading-tight">
            Connect WhatsApp to go live
          </p>
          <p className="text-white/80 text-[13px] mt-0.5">
            Your agent is ready — just needs a number
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={() => navigate("/onboarding")}
            className="bg-white text-orange-600 font-semibold text-[13px] px-4 py-2 rounded-lg hover:bg-orange-50 transition-colors"
          >
            Connect Now →
          </button>
          <button
            onClick={onDismiss}
            title="Dismiss"
            className="w-8 h-8 flex items-center justify-center rounded-full bg-white/20 hover:bg-white/30 text-white transition-colors"
          >
            <X size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Order status styles ───────────────────────────────────────────────────────

const STATUS_STYLES: Record<string, string> = {
  new:             "bg-blue-100 text-blue-700",
  confirmed:       "bg-indigo-100 text-indigo-700",
  paid:            "bg-green-100 text-green-700",
  payment_pending: "bg-amber-100 text-amber-700",
  processing:      "bg-orange-100 text-orange-700",
  dispatched:      "bg-purple-100 text-purple-700",
  delivered:       "bg-emerald-100 text-emerald-700",
  cancelled:       "bg-red-100 text-red-700",
};

// ── Daily usage bar ───────────────────────────────────────────────────────────

function UsageBar({ usage }: { usage: UsageStat }) {
  const pct = usage.percentage_used ?? 0;
  const barColor =
    pct >= 80 ? "bg-red-500" :
    pct >= 60 ? "bg-amber-400" :
    "bg-emerald-500";

  return (
    <div
      className="bg-white rounded-2xl px-6 py-5"
      style={{ boxShadow: "0 1px 3px rgba(0,0,0,0.08), 0 8px 24px rgba(0,0,0,0.04)" }}
    >
      <div className="flex items-center justify-between mb-3">
        <span className="text-[14px] font-bold text-gray-900">Daily Usage</span>
        <span className="text-[14px] font-bold text-indigo-600 tabular-nums">
          {usage.today_count} / {usage.limit}
        </span>
      </div>

      <div className="w-full bg-gray-100 rounded-full overflow-hidden" style={{ height: 8 }}>
        {usage.today_count === 0 ? (
          <div className="h-full rounded-full bg-gray-300" style={{ width: 4 }} />
        ) : (
          <div
            className={`h-full rounded-full transition-all duration-700 ease-out ${barColor}`}
            style={{ width: `${Math.min(pct, 100)}%` }}
          />
        )}
      </div>

      <div className="flex items-center justify-between mt-2">
        <span className="text-[12px] text-gray-500">
          {usage.today_count === 0 ? "No messages yet today" : `${pct.toFixed(1)}% used today`}
        </span>
        <span className="text-[12px] text-gray-500">
          {usage.monthly_count} messages this month
        </span>
      </div>

      {pct >= 80 && (
        <div className="mt-3">
          <Link
            to="/settings"
            className="text-[13px] font-semibold text-amber-600 hover:text-amber-700 transition-colors"
          >
            Running low — Upgrade plan →
          </Link>
        </div>
      )}
    </div>
  );
}

// ── Main ──────────────────────────────────────────────────────────────────────

export default function Dashboard() {
  const { t } = useTranslation();
  const { client } = useAuth();
  const navigate = useNavigate();

  const [leads, setLeads] = useState<Lead[]>([]);
  const [usage, setUsage] = useState<UsageStat | null>(null);
  const [recentOrders, setRecentOrders] = useState<Order[]>([]);
  const [orderStats, setOrderStats] = useState<OrderStats | null>(null);
  const [loading, setLoading] = useState(true);

  const [waBannerDismissed, setWaBannerDismissed] = useState(
    () => localStorage.getItem("connect_wa_dismissed") === "1",
  );

  function dismissWaBanner() {
    localStorage.setItem("connect_wa_dismissed", "1");
    setWaBannerDismissed(true);
  }

  useEffect(() => {
    async function load() {
      const [ls, us, orders, ostats] = await Promise.all([
        getLeads(),
        getUsageStats(),
        getOrders({ limit: 5 }),
        getOrderStats(),
      ]);
      setLeads(ls);
      setUsage(us);
      setRecentOrders(orders);
      setOrderStats(ostats);
      setLoading(false);
    }
    load();
  }, []);

  const hot = leads.filter((l) => l.status === "hot").length;
  const warm = leads.filter((l) => l.status === "warm").length;

  const hour = new Date().getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  const firstName = firstWord(client?.business_name || "there");

  const waConnected = !!client?.whatsapp_phone_number_id;
  const showWaBanner = !waConnected && !waBannerDismissed;

  const pendingOrders = orderStats?.pending_dispatch ?? 0;

  const quickActions = [
    {
      label: "New Broadcast",
      icon: Send,
      iconColor: "text-indigo-600",
      to: "/campaigns",
      badge: null,
    },
    {
      label: "Process Orders",
      icon: Package,
      iconColor: "text-amber-600",
      to: "/orders?status=confirmed",
      badge: pendingOrders > 0 ? String(pendingOrders) : null,
    },
    {
      label: "Hot Leads",
      icon: Flame,
      iconColor: "text-red-500",
      to: "/leads?status=hot",
      badge: hot > 0 ? String(hot) : null,
    },
    {
      label: "Test Agent",
      icon: FlaskConical,
      iconColor: "text-purple-600",
      to: "/sandbox",
      badge: null,
    },
  ];

  return (
    <Layout>
      <div className="max-w-[1200px] mx-auto space-y-4">

        {/* 1 — Welcome banner */}
        <div
          className="bg-gradient-to-r from-indigo-600 to-indigo-700 rounded-2xl px-8 py-6 text-white shadow-md"
          style={{ minHeight: 100 }}
        >
          <div className="flex items-center justify-between gap-4">
            <div>
              <h1 className="text-[22px] font-bold text-white leading-tight">
                {greeting}, {firstName}! 👋
              </h1>
              <p className="text-[14px] text-white/80 mt-1">
                {hot > 0
                  ? `${hot} hot lead${hot !== 1 ? "s" : ""} need${hot === 1 ? "s" : ""} attention today.`
                  : warm > 0
                  ? `${warm} warm lead${warm !== 1 ? "s" : ""} in your pipeline.`
                  : "Here's what's happening today."}
              </p>
            </div>

            {/* Mini stats — desktop only */}
            {!loading && usage && (
              <div className="hidden sm:flex items-center gap-2 shrink-0">
                {[
                  { emoji: "💬", label: `${usage.today_count} messages` },
                  { emoji: "🔥", label: `${hot} hot` },
                  { emoji: "📦", label: `${orderStats?.today_orders ?? 0} orders` },
                ].map((stat) => (
                  <div
                    key={stat.label}
                    className="flex items-center gap-1.5 bg-white/20 rounded-full px-3 py-1.5 text-[12px] text-white font-medium"
                  >
                    <span>{stat.emoji}</span>
                    <span>{stat.label}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* 2 — Connect WhatsApp banner */}
        {showWaBanner && <ConnectWhatsAppBanner onDismiss={dismissWaBanner} />}

        {/* 3 — Catalogue bar */}
        {client?.catalogue_slug && (
          <CatalogueBanner slug={client.catalogue_slug} businessName={client.business_name} />
        )}

        {loading ? (
          <div className="space-y-4">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
              {[...Array(4)].map((_, i) => (
                <div key={i} className="bg-white rounded-2xl h-32 animate-pulse border border-gray-100" />
              ))}
            </div>
            <div className="bg-white rounded-2xl h-12 animate-pulse border border-gray-100" />
            <div className="bg-white rounded-2xl h-16 animate-pulse border border-gray-100" />
            <div className="bg-white rounded-2xl h-40 animate-pulse border border-gray-100" />
          </div>
        ) : (
          <>
            {/* 4 — Stat cards */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
              <StatCard
                label={t("dashboard.messages_today")}
                value={usage?.today_count ?? 0}
                color="blue"
                icon={MessageSquare}
                tooltip="Messages processed by the AI agent today"
              />
              <StatCard
                label={t("dashboard.hot_leads")}
                value={hot}
                color="red"
                icon={Flame}
                sub={hot > 0 ? "need follow-up" : undefined}
                tooltip="Leads tagged as hot — likely ready to buy"
              />
              <StatCard
                label="Orders Today"
                value={orderStats?.today_orders ?? 0}
                color="green"
                icon={ShoppingCart}
                sub={`₹${(orderStats?.today_revenue ?? 0).toLocaleString("en-IN")} revenue`}
                tooltip="Orders created today across all channels"
              />
              <StatCard
                label="Revenue Today"
                value={`₹${(orderStats?.today_revenue ?? 0).toLocaleString("en-IN")}`}
                color="indigo"
                icon={IndianRupee}
                sub={`${orderStats?.total_orders ?? 0} orders this month`}
                tooltip="Sum of paid orders received today"
              />
            </div>

            {/* 5 — Quick actions */}
            <div className="flex flex-wrap gap-2">
              {quickActions.map((action) => {
                const Icon = action.icon;
                return (
                  <button
                    key={action.label}
                    onClick={() => navigate(action.to)}
                    className="flex items-center gap-2 bg-white border border-gray-200 rounded-[10px] px-5 py-2.5 text-[13px] font-medium text-gray-700 hover:border-indigo-400 hover:bg-indigo-50/50 hover:shadow-sm transition-all duration-150 active:scale-[0.98] relative"
                  >
                    <Icon size={16} className={action.iconColor} />
                    {action.label}
                    {action.badge && (
                      <span className="ml-0.5 bg-orange-500 text-white text-[10px] font-bold px-1.5 py-0.5 rounded-full leading-none">
                        {action.badge}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>

            {/* 6 — Daily usage bar */}
            {usage && <UsageBar usage={usage} />}

            {/* 7 — Recent orders */}
            {(recentOrders.length > 0 || (orderStats && orderStats.total_orders > 0)) && (
              <div
                className="bg-white rounded-2xl overflow-hidden"
                style={{ boxShadow: "0 1px 3px rgba(0,0,0,0.08), 0 8px 24px rgba(0,0,0,0.04)" }}
              >
                <div className="flex items-center justify-between px-6 py-4 border-b border-gray-50">
                  <h2 className="text-[16px] font-bold text-gray-900">Recent Orders</h2>
                  <Link
                    to="/orders"
                    className="text-[13px] font-medium text-indigo-600 hover:text-indigo-700 transition-colors"
                  >
                    View all orders →
                  </Link>
                </div>

                {recentOrders.length === 0 ? (
                  <div className="px-6 py-10 text-center text-sm text-gray-400">No orders yet</div>
                ) : (
                  <div className="p-3 space-y-2">
                    {recentOrders.slice(0, 5).map((order) => (
                      <Link
                        key={order.id}
                        to="/orders"
                        className="flex items-center gap-4 bg-white border border-gray-100 rounded-xl px-4 py-3.5 hover:border-gray-200 hover:bg-gray-50/60 transition-all duration-150 group"
                      >
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-[13px] font-bold text-indigo-700">
                              {order.order_number}
                            </span>
                            <span
                              className={`text-[11px] font-medium px-2 py-0.5 rounded-full capitalize ${STATUS_STYLES[order.status] ?? "bg-gray-100 text-gray-600"}`}
                            >
                              {order.status.replace(/_/g, " ")}
                            </span>
                            {order.payment_status === "pending" && (
                              <span className="inline-flex items-center gap-1 text-[11px] font-medium px-2 py-0.5 rounded-full bg-amber-100 text-amber-700">
                                <Clock size={10} />
                                Payment Pending
                              </span>
                            )}
                          </div>
                          <div className="text-[13px] text-gray-500 truncate mt-0.5">
                            {order.product_name} — {order.customer_name}
                            {" · "}
                            <span className="text-[11px] text-gray-400">{timeAgo(order.created_at)}</span>
                          </div>
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
                          <span className="text-[15px] font-bold text-emerald-700">
                            ₹{order.total_amount.toLocaleString("en-IN")}
                          </span>
                          <span className="w-8 h-8 flex items-center justify-center rounded-full bg-gray-100 group-hover:bg-indigo-100 group-hover:text-indigo-600 text-gray-400 transition-all duration-150">
                            <ChevronRight size={15} />
                          </span>
                        </div>
                      </Link>
                    ))}
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </Layout>
  );
}
