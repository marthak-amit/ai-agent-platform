import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Check, ChevronDown, ChevronUp, Circle, Loader2, Rocket, Trash2 } from "lucide-react";
import { removeDemoProducts } from "../../api/client";
import { useSetup } from "../../context/SetupContext";
import { useToast } from "../../context/ToastContext";
import type { ChecklistItem, ChecklistKey } from "../../types/setup";
import { CHECKLIST_ROUTES } from "../../utils/setup";
import GoLiveModal from "./GoLiveModal";

const COLLAPSED_KEY = "setup_checklist_collapsed";

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

/**
 * The setup checklist pinned to the top of the dashboard until the account is live.
 * Status is derived from real state on the server, so there is nothing to tick — finishing the work in the app is
 * what completes a row. Collapsible; shows "x/6 done"; hidden once every required item is done (i.e. after go-live).
 */
export default function SetupChecklist() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const { checklist, refresh } = useSetup();
  const [params, setParams] = useSearchParams();
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [goLiveOpen, setGoLiveOpen] = useState(false);
  const [removing, setRemoving] = useState(false);

  // Coming back from another page: the facts may have changed (a product was added, WhatsApp connected, ...).
  useEffect(() => {
    void refresh();
  }, [refresh]);

  // "/dashboard?golive=1" (the empty-state CTA for the last step) opens the confirmation directly.
  useEffect(() => {
    if (params.get("golive") === "1" && checklist && !checklist.hidden && checklist.can_go_live) {
      setGoLiveOpen(true);
      const next = new URLSearchParams(params);
      next.delete("golive");
      setParams(next, { replace: true });
    }
  }, [params, checklist, setParams]);

  if (!checklist || checklist.hidden) return null;

  function toggle() {
    const next = !collapsed;
    setCollapsed(next);
    try {
      localStorage.setItem(COLLAPSED_KEY, next ? "1" : "0");
    } catch {
      // storage unavailable — it just stays expanded after a reload
    }
  }

  async function removeSamples() {
    setRemoving(true);
    try {
      await removeDemoProducts();
      toast.show(t("setup.samples_removed"), { kind: "success" });
      await refresh();
    } finally {
      setRemoving(false);
    }
  }

  function open(item: ChecklistItem) {
    if (item.key === "go_live") setGoLiveOpen(true);
    else navigate(CHECKLIST_ROUTES[item.key]);
  }

  const blocked = (key: ChecklistKey) => key === "go_live" && !checklist.can_go_live;

  return (
    <section
      className="bg-white rounded-2xl border border-brand-primary/20 overflow-hidden"
      style={{ boxShadow: "0 1px 3px rgba(0,0,0,0.08), 0 8px 24px rgba(0,0,0,0.04)" }}
      aria-label={t("setup.title")}
    >
      <header className="flex items-center gap-3 px-5 py-4">
        <span className="w-9 h-9 rounded-full bg-brand-primary/10 flex items-center justify-center shrink-0">
          <Rocket size={18} className="text-brand-primaryDark" />
        </span>
        <div className="flex-1 min-w-0">
          <h2 className="text-[15px] font-bold text-gray-900 leading-tight">{t("setup.title")}</h2>
          <div className="flex items-center gap-3 mt-1.5">
            <div className="h-1.5 w-32 sm:w-48 bg-gray-100 rounded-full overflow-hidden" aria-hidden="true">
              <div
                className="h-full bg-brand-primary rounded-full transition-all"
                style={{ width: `${Math.round((checklist.done_count / checklist.total) * 100)}%` }}
              />
            </div>
            <span className="text-xs font-semibold text-gray-500 tabular-nums">
              {t("setup.progress", { done: checklist.done_count, total: checklist.total })}
            </span>
          </div>
        </div>
        <button
          type="button"
          onClick={toggle}
          aria-expanded={!collapsed}
          aria-label={collapsed ? t("setup.expand") : t("setup.collapse")}
          className="w-8 h-8 flex items-center justify-center rounded-full text-gray-400 hover:bg-gray-100"
        >
          {collapsed ? <ChevronDown size={18} /> : <ChevronUp size={18} />}
        </button>
      </header>

      {!collapsed && (
        <>
          <ul className="border-t border-gray-100 divide-y divide-gray-50">
            {checklist.items.map((item) => {
              const isBlocked = blocked(item.key);
              return (
                <li key={item.key}>
                  <button
                    type="button"
                    onClick={() => open(item)}
                    disabled={isBlocked}
                    title={isBlocked ? t("setup.blocked") : undefined}
                    className="w-full flex items-center gap-3 px-5 py-3 text-left hover:bg-gray-50 disabled:opacity-60 disabled:hover:bg-transparent"
                  >
                    {item.done ? (
                      <span className="w-6 h-6 rounded-full bg-emerald-500 text-white flex items-center justify-center shrink-0">
                        <Check size={14} strokeWidth={3} />
                        <span className="sr-only">{t("setup.done")}</span>
                      </span>
                    ) : (
                      <span className="w-6 h-6 flex items-center justify-center shrink-0 text-gray-300">
                        <Circle size={22} />
                        <span className="sr-only">{t("setup.todo")}</span>
                      </span>
                    )}
                    <span className="flex-1 min-w-0">
                      <span className={`block text-sm font-semibold ${item.done ? "text-gray-400 line-through" : "text-gray-900"}`}>
                        {t(`setup.item.${item.key}.title`)}
                        {!item.required && (
                          <span className="ml-2 text-[10px] font-medium uppercase tracking-wide text-gray-400 no-underline">
                            {t("setup.optional")}
                          </span>
                        )}
                      </span>
                      {!item.done && <span className="block text-xs text-gray-500 mt-0.5">{t(`setup.item.${item.key}.desc`)}</span>}
                    </span>
                    {!item.done && (
                      <span className="text-xs font-semibold text-brand-primaryDark whitespace-nowrap">
                        {t(`setup.cta.${item.key}`)} →
                      </span>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>

          {checklist.demo_products > 0 && (
            <div className="flex items-center gap-3 px-5 py-3 bg-amber-50 border-t border-amber-100 text-sm text-amber-900">
              <span className="flex-1">
                {t("setup.samples", { count: checklist.demo_products })}{" "}
                <Link to="/catalogue" className="underline font-medium">
                  {t("nav.catalogue")}
                </Link>
              </span>
              <button
                type="button"
                onClick={removeSamples}
                disabled={removing}
                className="inline-flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 rounded-lg bg-white border border-amber-200 hover:bg-amber-100 disabled:opacity-60"
              >
                {removing ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
                {removing ? t("setup.removing") : t("setup.remove_samples")}
              </button>
            </div>
          )}
        </>
      )}

      {goLiveOpen && <GoLiveModal checklist={checklist} onClose={() => setGoLiveOpen(false)} />}
    </section>
  );
}
