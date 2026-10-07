import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { fetchPlans } from "../../api/billing";
import { useBilling } from "../../context/BillingContext";
import type { RazorpayCheckout } from "../../hooks/useRazorpayCheckout";
import type { Plan } from "../../types/billing";
import { planAction, subscriptionFingerprint } from "../../utils/billing";
import { getPendingPlan } from "../../utils/pendingPlan";
import PlanCard from "./PlanCard";

/** Loads GET /billing/plans and renders the plan cards; shared by the billing page and the onboarding step. */
export default function PlanPicker({ checkout, reloadKey = 0 }: { checkout: RazorpayCheckout; /** Bump to re-fetch prices (e.g. after the GSTIN changed the tax split). */ reloadKey?: number }) {
  const { t } = useTranslation();
  const { state } = useBilling();
  const [plans, setPlans] = useState<Plan[] | null>(null);
  const [error, setError] = useState(false);
  // Plan chosen on the marketing site; read once so it stays stable while the page re-renders.
  const [pendingCode] = useState(getPendingPlan);
  const pickRef = useRef<HTMLDivElement>(null);

  // `is_current` comes from the plan list, so reload it whenever what the tenant owns changes (after a purchase).
  const fingerprint = subscriptionFingerprint(state);

  const load = useCallback(async () => {
    setError(false);
    try {
      const list = await fetchPlans();
      setPlans([...list.plans].sort((a, b) => a.sort_order - b.sort_order));
    } catch {
      setError(true);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, fingerprint, reloadKey]);

  // Bring the preselected card into view once the cards exist.
  const pickReady = plans !== null;
  useEffect(() => {
    if (pickReady) pickRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [pickReady]);

  if (error && !plans) {
    return (
      <div className="rounded-2xl bg-white border border-gray-200 p-8 text-center">
        <p className="text-sm text-gray-600">{t("billing.plans_error")}</p>
        <button type="button" onClick={() => void load()} className="mt-3 text-sm font-semibold text-brand-primaryDark hover:underline">
          {t("billing.retry")}
        </button>
      </div>
    );
  }

  if (!plans) {
    return (
      <div className="grid gap-5 md:grid-cols-3" aria-busy="true" aria-label={t("billing.loading")}>
        {[0, 1, 2].map((i) => (
          <div key={i} className="h-80 rounded-2xl bg-gray-100 animate-pulse" />
        ))}
      </div>
    );
  }

  if (plans.length === 0) {
    return (
      <div className="rounded-2xl bg-white border border-gray-200 p-8 text-center text-sm text-gray-500">
        {t("billing.plans_empty")}
      </div>
    );
  }

  return (
    <div className="grid gap-5 pt-3 md:grid-cols-3">
      {plans.map((plan) => {
        const action = planAction(plan, plans, state);
        // Only worth highlighting when buying it is actually possible and not just a renewal.
        const picked = plan.code === pendingCode && (action.kind === "subscribe" || action.kind === "upgrade");
        return (
        <div key={plan.code} ref={picked ? pickRef : undefined} className="flex flex-col">
        <PlanCard
          plan={plan}
          picked={picked}
          action={action}
          isCurrent={state?.subscription?.plan.code === plan.code}
          periodEnd={state?.subscription?.current_period_end}
          busy={checkout.busy}
          loading={checkout.busy && checkout.planCode === plan.code}
          onSelect={(code) => void checkout.start(code)}
        />
        </div>
        );
      })}
    </div>
  );
}
