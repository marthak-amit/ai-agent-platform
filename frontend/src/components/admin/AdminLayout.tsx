import { useState } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  Activity,
  BarChart3,
  Brain,
  CreditCard,
  LayoutDashboard,
  LogOut,
  Menu,
  ScrollText,
  ShieldCheck,
  UserCog,
  Users,
} from "lucide-react";
import { useAdminAuth } from "../../context/AdminAuthContext";
import type { AdminPermission } from "../../types/admin";
import { Badge } from "./ui";

const NAV: { path: string; label: string; icon: typeof Users; perm: AdminPermission }[] = [
  { path: "/admin", label: "Overview", icon: LayoutDashboard, perm: "overview.read" },
  { path: "/admin/clients", label: "Clients", icon: Users, perm: "clients.read" },
  { path: "/admin/billing", label: "Billing & plans", icon: CreditCard, perm: "billing.read" },
  { path: "/admin/usage", label: "AI usage & cost", icon: Brain, perm: "usage.read" },
  { path: "/admin/system", label: "System health", icon: Activity, perm: "system.read" },
  { path: "/admin/audit", label: "Audit log", icon: ScrollText, perm: "audit.read" },
  { path: "/admin/operators", label: "Operators", icon: UserCog, perm: "admins.manage" },
];

export default function AdminLayout() {
  const { admin, signOut, can } = useAdminAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);

  const items = NAV.filter((i) => can(i.perm));
  const isActive = (path: string) => (path === "/admin" ? location.pathname === "/admin" : location.pathname.startsWith(path));

  async function handleSignOut() {
    await signOut();
    navigate("/admin/login");
  }

  const sidebar = (
    <div className="flex flex-col h-full bg-brand-secondary text-gray-200">
      <div className="px-5 py-5 flex items-center gap-2 border-b border-white/10">
        <ShieldCheck size={20} className="text-brand-primary" />
        <div>
          <div className="text-sm font-semibold text-white leading-tight">SellerTalk24</div>
          <div className="text-[11px] text-gray-400 leading-tight">Admin console</div>
        </div>
      </div>
      <nav className="flex-1 px-3 py-4 space-y-0.5 overflow-y-auto" aria-label="Admin">
        {items.map((item) => {
          const Icon = item.icon;
          return (
            <Link
              key={item.path}
              to={item.path}
              onClick={() => setOpen(false)}
              className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                isActive(item.path) ? "bg-white/10 text-white" : "text-gray-300 hover:bg-white/5 hover:text-white"
              }`}
            >
              <Icon size={17} /> {item.label}
            </Link>
          );
        })}
      </nav>
      <div className="px-4 py-4 border-t border-white/10 text-xs space-y-2">
        <Link to="/admin/account" onClick={() => setOpen(false)} className="block hover:text-white">
          <div className="truncate text-gray-200 font-medium">{admin?.name || admin?.email}</div>
          <div className="truncate text-gray-400">{admin?.email}</div>
        </Link>
        <div className="flex items-center justify-between">
          <Badge tone="blue">{admin?.role}</Badge>
          <button onClick={handleSignOut} className="flex items-center gap-1 text-gray-300 hover:text-white">
            <LogOut size={14} /> Sign out
          </button>
        </div>
      </div>
    </div>
  );

  return (
    <div className="min-h-screen flex bg-brand-bg font-sans">
      <aside className="hidden lg:block w-60 shrink-0 sticky top-0 h-screen">{sidebar}</aside>
      {open && (
        <div className="lg:hidden fixed inset-0 z-40 flex">
          <div className="w-64 h-full">{sidebar}</div>
          <button aria-label="Close menu" className="flex-1 bg-black/40" onClick={() => setOpen(false)} />
        </div>
      )}
      <div className="flex-1 min-w-0">
        <header className="lg:hidden flex items-center gap-3 px-4 py-3 bg-white border-b border-gray-100">
          <button aria-label="Open menu" onClick={() => setOpen(true)}>
            <Menu size={20} />
          </button>
          <BarChart3 size={18} className="text-brand-primaryDark" />
          <span className="font-semibold text-sm">Admin console</span>
        </header>
        {admin?.via === "key" && (
          <div className="bg-amber-50 border-b border-amber-100 text-amber-800 text-xs px-6 py-2">
            Signed in via the shared admin key — actions are attributed to “{admin.email}”. Use a personal operator account instead.
          </div>
        )}
        <main className="p-4 sm:p-6 lg:p-8 max-w-7xl mx-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
