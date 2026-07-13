import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useAuth } from "../context/AuthContext";
import Logo from "./Logo";
import type { CurrentUser, PermissionKey } from "../types";
import {
  LayoutDashboard,
  MessageSquare,
  Users,
  UserCheck,
  BarChart2,
  Megaphone,
  Package,
  ClipboardList,
  Link2,
  Settings,
  LogOut,
  Globe,
  Menu,
  ShoppingBag,
  ExternalLink,
  FlaskConical,
  Brain,
} from "lucide-react";

const APP_BASE_URL = (import.meta.env.VITE_APP_URL as string) || "http://localhost:5173";

// permission: undefined means visible to any authenticated user (e.g. Dashboard).
// ownerOnly: true means the item never shows for a non-owner, regardless of checklist.
const NAV_ITEMS: { path: string; key: string; icon: typeof LayoutDashboard; permission?: PermissionKey; ownerOnly?: boolean }[] = [
  { path: "/dashboard",     key: "nav.dashboard",     icon: LayoutDashboard },
  { path: "/conversations", key: "nav.conversations", icon: MessageSquare, permission: "manual_reply" },
  { path: "/orders",        key: "nav.orders",        icon: ClipboardList, permission: "order_view" },
  { path: "/leads",         key: "nav.leads",         icon: Users, ownerOnly: true },
  { path: "/customers",     key: "nav.customers",     icon: UserCheck, ownerOnly: true },
  { path: "/analytics",     key: "nav.analytics",     icon: BarChart2, permission: "analytics_view" },
  { path: "/campaigns",     key: "nav.campaigns",     icon: Megaphone, ownerOnly: true },
  { path: "/knowledge",     key: "nav.knowledge",     icon: Brain, ownerOnly: true },
  { path: "/catalogue",     key: "nav.catalogue",     icon: Package, permission: "catalog_edit" },
  { path: "/channels",      key: "nav.channels",      icon: Link2, ownerOnly: true },
  { path: "/sandbox",       key: "nav.sandbox",       icon: FlaskConical, ownerOnly: true },
  { path: "/settings",      key: "nav.settings",      icon: Settings, ownerOnly: true },
];

function isNavItemVisible(item: (typeof NAV_ITEMS)[number], currentUser: CurrentUser | undefined) {
  if (!item.permission && !item.ownerOnly) return true;
  if (!currentUser) return false;
  if (currentUser.is_owner) return true;
  if (item.ownerOnly) return false;
  return !!item.permission && currentUser.permissions.includes(item.permission);
}

const LANG_OPTIONS = [
  { code: "en", label: "EN" },
  { code: "hi", label: "HI" },
  { code: "gu", label: "GU" },
];

function AgentStatusDot({ client }: { client: { whatsapp_phone_number_id?: string | null; whatsapp_connected?: boolean | null } | null }) {
  if (!client) return null;
  const configured = !!(client.whatsapp_phone_number_id && client.whatsapp_connected);
  if (configured) {
    return (
      <span className="flex items-center gap-1 text-[10px] text-emerald-600 font-medium">
        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
        Active
      </span>
    );
  }
  return (
    <Link
      to="/channels"
      className="flex items-center gap-1 text-[10px] text-amber-500 font-medium hover:text-amber-700 transition-colors"
      title="Connect WhatsApp to go live"
    >
      <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />
      Not live · Connect WhatsApp →
    </Link>
  );
}

export default function Layout({ children }: { children: React.ReactNode }) {
  const { client, signOut, changeLanguage } = useAuth();
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const navItems = NAV_ITEMS.filter((item) => isNavItemVisible(item, client?.current_user));

  function handleSignOut() {
    signOut();
    navigate("/login");
  }

  const signedInEmail = client?.current_user.email ?? client?.email;
  const initials = (signedInEmail ?? "U").slice(0, 2).toUpperCase();
  const currentLang = client?.dashboard_language || "en";

  function SidebarContent() {
    return (
      <div className="flex flex-col h-full">
        {/* Logo + agent status */}
        <div className="px-4 py-5 border-b border-gray-100">
          <div className="flex flex-col items-start gap-1.5">
            <Logo className="h-10 w-auto shrink-0" />
            <div className="text-sm font-medium text-gray-700 truncate">
              {client?.business_name || "Your Business"}
            </div>
          </div>
          {/* Issue 10 — agent status under logo */}
          <div className="mt-2">
            <AgentStatusDot client={client} />
          </div>
        </div>

        {/* Navigation */}
        <nav className="flex-1 px-3 py-4 space-y-0.5 overflow-y-auto">
          {navItems.map((item) => {
            const Icon = item.icon;
            const active = location.pathname === item.path;
            return (
              <Link
                key={item.path}
                to={item.path}
                onClick={() => setSidebarOpen(false)}
                className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-150 ${
                  active
                    ? "bg-brand-primaryDark text-white shadow-[inset_3px_0_0_rgba(255,255,255,0.7),0_1px_2px_rgba(0,0,0,0.05)]"
                    : "text-gray-600 hover:bg-brand-primary/10 hover:text-brand-primaryDark"
                }`}
              >
                <Icon size={18} className={active ? "text-white" : "text-gray-400"} />
                {t(item.key)}
              </Link>
            );
          })}
        </nav>

        {/* Bottom section */}
        <div className="border-t border-gray-100 px-3 py-4 space-y-3">
          {/* My Catalogue quick link */}
          {client?.catalogue_slug && <div className="border-t border-gray-100 -mx-3 mb-3" />}
          {client?.catalogue_slug && (
            <a
              href={`${APP_BASE_URL}/shop/${client.catalogue_slug}`}
              target="_blank"
              rel="noreferrer"
              title={`/shop/${client.catalogue_slug}`}
              className="flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-xs font-medium text-gray-600 border border-gray-200 hover:bg-gray-50 hover:text-gray-900 transition-colors"
            >
              <ShoppingBag size={14} />
              My Catalogue
              <ExternalLink size={12} />
            </a>
          )}

          {/* Language switcher */}
          <div className="flex items-center gap-1 px-2">
            <Globe size={14} className="text-gray-400 mr-1" />
            {LANG_OPTIONS.map((opt) => (
              <button
                key={opt.code}
                onClick={() => changeLanguage(opt.code)}
                className={`text-xs font-medium px-2 py-1 rounded transition-colors ${
                  currentLang === opt.code
                    ? "bg-brand-primary/10 text-brand-primaryDark"
                    : "text-gray-400 hover:text-gray-600 hover:bg-gray-100"
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>

          {/* User row */}
          <div className="flex items-center gap-3 px-2">
            <div className="w-8 h-8 bg-brand-primaryDark rounded-full flex items-center justify-center text-white text-xs font-bold shrink-0">
              {initials}
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-xs text-gray-500 truncate">{signedInEmail}</div>
            </div>
            <button
              onClick={handleSignOut}
              title="Sign out"
              className="text-gray-400 hover:text-red-500 transition-colors"
            >
              <LogOut size={16} />
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen overflow-hidden bg-gray-50 font-sans">
      {/* Desktop sidebar */}
      <aside className="hidden lg:flex flex-col w-64 shrink-0 bg-white border-r border-gray-200 fixed h-full z-30">
        <SidebarContent />
      </aside>

      {/* Mobile sidebar overlay */}
      {sidebarOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div
            className="absolute inset-0 bg-black/40"
            onClick={() => setSidebarOpen(false)}
          />
          <aside className="absolute left-0 top-0 h-full w-64 bg-white shadow-xl z-10">
            <SidebarContent />
          </aside>
        </div>
      )}

      {/* Main content */}
      <div className="flex-1 lg:ml-64 flex flex-col overflow-hidden">
        {/* Mobile top bar */}
        <div className="lg:hidden bg-white border-b border-gray-200 px-4 py-3 flex items-center gap-3 shrink-0">
          <button
            onClick={() => setSidebarOpen(true)}
            className="text-gray-500 hover:text-gray-700"
          >
            <Menu size={22} />
          </button>
          <Logo className="h-8 w-auto" />
        </div>

        {/* Page content */}
        <main className="flex-1 overflow-y-auto">
          <div className="p-6 h-full">{children}</div>
        </main>
      </div>
    </div>
  );
}
