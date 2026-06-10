import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { getLeads, updateLeadStatus } from "../api/client";
import Layout from "../components/Layout";
import type { Lead } from "../types";
import { Search } from "lucide-react";

const STATUSES = ["hot", "warm", "cold"] as const;

const STATUS_CONFIG: Record<string, { pill: string; count_bg: string; count_text: string }> = {
  hot:  { pill: "bg-red-100 text-red-700",   count_bg: "bg-red-50",   count_text: "text-red-600"  },
  warm: { pill: "bg-amber-100 text-amber-700", count_bg: "bg-amber-50", count_text: "text-amber-600" },
  cold: { pill: "bg-blue-100 text-blue-700",  count_bg: "bg-blue-50",  count_text: "text-blue-600"  },
};


export default function Leads() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [leads, setLeads] = useState<Lead[]>([]);
  const [filter, setFilter] = useState<string>("all");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getLeads().then((data) => {
      setLeads(data);
      setLoading(false);
    });
  }, []);

  async function handleStatusChange(leadId: number, newStatus: string) {
    await updateLeadStatus(leadId, newStatus);
    setLeads((prev) =>
      prev.map((l) => (l.id === leadId ? { ...l, status: newStatus as Lead["status"] } : l))
    );
  }

  const hot = leads.filter((l) => l.status === "hot").length;
  const warm = leads.filter((l) => l.status === "warm").length;
  const cold = leads.filter((l) => l.status === "cold").length;
  const total = leads.length || 1;

  const filtered = leads.filter((l) => {
    if (filter !== "all" && l.status !== filter) return false;
    if (search && !l.phone_number.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  });

  const filterLabels: Record<string, string> = {
    all:  t("leads.all"),
    hot:  t("leads.hot"),
    warm: t("leads.warm"),
    cold: t("leads.cold"),
  };

  return (
    <Layout>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">{t("leads.title")}</h1>
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-3 gap-4 mb-6">
        {([["hot", hot], ["warm", warm], ["cold", cold]] as const).map(([status, count]) => {
          const cfg = STATUS_CONFIG[status];
          const pct = Math.round((count / total) * 100);
          return (
            <div key={status} className="bg-white rounded-xl border border-gray-100 shadow-sm p-5">
              <div className="flex items-center justify-between mb-2">
                <span className={`text-xs font-medium uppercase tracking-wide px-2 py-0.5 rounded-full ${cfg.pill}`}>
                  {status}
                </span>
                <span className={`text-xs font-medium ${cfg.count_text}`}>{pct}%</span>
              </div>
              <p className="text-3xl font-bold text-gray-900">{count}</p>
              <p className="text-xs text-gray-400 mt-0.5">out of {leads.length} total</p>
            </div>
          );
        })}
      </div>

      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <div className="relative">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            type="text"
            placeholder="Search phone..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-8 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-400 bg-white w-48"
          />
        </div>
        <div className="flex gap-1.5">
          {["all", ...STATUSES].map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3.5 py-1.5 rounded-lg text-sm font-medium capitalize transition-all duration-150 ${
                filter === f
                  ? "bg-indigo-600 text-white shadow-sm"
                  : "bg-white border border-gray-200 text-gray-600 hover:border-indigo-300 hover:text-indigo-600"
              }`}
            >
              {filterLabels[f] ?? f}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="bg-white rounded-xl border border-gray-100 shadow-sm overflow-hidden">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="h-14 border-b border-gray-50 animate-pulse bg-gray-50" />
          ))}
        </div>
      ) : (
        <div className="bg-white rounded-xl border border-gray-100 shadow-sm overflow-hidden">
          {/* Table header */}
          <div className="grid grid-cols-[40px_1fr_120px_180px] gap-4 px-5 py-3 bg-gray-50 border-b border-gray-100">
            <span className="text-xs font-medium uppercase tracking-wide text-gray-400">#</span>
            <span className="text-xs font-medium uppercase tracking-wide text-gray-400">Phone</span>
            <span className="text-xs font-medium uppercase tracking-wide text-gray-400">Status</span>
            <span className="text-xs font-medium uppercase tracking-wide text-gray-400 text-right">Action</span>
          </div>

          <div className="divide-y divide-gray-50">
            {filtered.map((lead, idx) => {
              const sCfg = STATUS_CONFIG[lead.status] ?? STATUS_CONFIG.cold;
              return (
                <div
                  key={lead.id}
                  className="grid grid-cols-[40px_1fr_120px_180px] gap-4 px-5 py-3.5 hover:bg-gray-50 transition-colors items-center"
                >
                  <span className="text-xs text-gray-400">{idx + 1}</span>
                  <span className="text-sm font-medium text-gray-900">{lead.phone_number}</span>
                  <span className={`text-xs font-medium px-2.5 py-1 rounded-full capitalize w-fit ${sCfg.pill}`}>
                    {lead.status}
                  </span>
                  <div className="flex items-center gap-2 justify-end">
                    <select
                      value={lead.status}
                      onChange={(e) => handleStatusChange(lead.id, e.target.value)}
                      className="text-xs border border-gray-200 rounded-lg px-2 py-1 text-gray-600 focus:outline-none focus:ring-2 focus:ring-indigo-400"
                    >
                      {STATUSES.map((s) => (
                        <option key={s} value={s}>{filterLabels[s] ?? s}</option>
                      ))}
                    </select>
                    <button
                      onClick={() => navigate("/conversations")}
                      className="text-xs text-indigo-600 font-medium hover:underline whitespace-nowrap"
                    >
                      View →
                    </button>
                  </div>
                </div>
              );
            })}
          </div>

          {filtered.length === 0 && (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <p className="text-4xl mb-2">👥</p>
              <p className="text-sm text-gray-400">No leads found.</p>
            </div>
          )}
        </div>
      )}
    </Layout>
  );
}
