import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2 } from "lucide-react";
import Layout from "../components/Layout";
import BillingDetailsCard from "../components/billing/BillingDetailsCard";
import CheckoutModals from "../components/billing/CheckoutModals";
import CurrentPlanCard from "../components/billing/CurrentPlanCard";
import PaymentHistory from "../components/billing/PaymentHistory";
import PlanPicker from "../components/billing/PlanPicker";
import { useBilling } from "../context/BillingContext";
import { fetchPayments } from "../api/billing";
import { useRazorpayCheckout } from "../hooks/useRazorpayCheckout";
import type { PaymentList } from "../types/billing";

const PAGE_SIZE = 10;

/** /billing — current plan + usage, plan cards with upgrade/renew/downgrade states, and payment history. */
export default function Billing() {
  const { t } = useTranslation();
  const { state, loading, error, refresh } = useBilling();
  const [payments, setPayments] = useState<PaymentList | null>(null);
  const [paymentsLoading, setPaymentsLoading] = useState(true);
  const [paymentsError, setPaymentsError] = useState(false);
  const [page, setPage] = useState(1);
  const [pricesVersion, setPricesVersion] = useState(0); // bumped when GSTIN/address change → plan prices re-fetched

  const loadPayments = useCallback(async (nextPage: number) => {
    setPaymentsLoading(true);
    setPaymentsError(false);
    try {
      const list = await fetchPayments(nextPage, PAGE_SIZE);
      setPayments(list);
      setPage(nextPage);
    } catch {
      setPaymentsError(true);
    } finally {
      setPaymentsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadPayments(1);
    void refresh(); // usage moves while the app is open — always show fresh numbers on this page
  }, [loadPayments, refresh]);

  // A purchase adds a payment row (and changes the plan): show it without a manual reload.
  const checkout = useRazorpayCheckout({ onSuccess: () => void loadPayments(1) });

  return (
    <Layout>
      <div className="max-w-5xl mx-auto flex flex-col gap-8">
        <header>
          <h1 className="text-2xl font-bold text-brand-secondary">{t("billing.title")}</h1>
          <p className="text-sm text-gray-500 mt-1">{t("billing.subtitle")}</p>
        </header>

        {!state && loading && (
          <div className="h-48 rounded-2xl bg-gray-100 animate-pulse" aria-busy="true" aria-label={t("billing.loading")} />
        )}

        {!state && !loading && error && (
          <div className="rounded-2xl bg-white border border-gray-200 p-8 text-center">
            <p className="text-sm text-gray-600">{t("billing.subscription_error")}</p>
            <button
              type="button"
              onClick={() => void refresh()}
              className="mt-3 inline-flex items-center gap-2 text-sm font-semibold text-brand-primaryDark hover:underline"
            >
              {loading && <Loader2 size={14} className="animate-spin" />}
              {t("billing.retry")}
            </button>
          </div>
        )}

        {state && <CurrentPlanCard state={state} />}

        <BillingDetailsCard onSaved={() => setPricesVersion((v) => v + 1)} />

        <section className="flex flex-col gap-2">
          <h2 className="text-base font-bold text-brand-secondary">{t("billing.plans_title")}</h2>
          <PlanPicker checkout={checkout} reloadKey={pricesVersion} />
          <p className="text-xs text-gray-400">{t("billing.gst_note")}</p>
        </section>

        <PaymentHistory
          data={payments}
          loading={paymentsLoading}
          error={paymentsError}
          onRetry={() => void loadPayments(page)}
          onPage={(p) => void loadPayments(p)}
        />
      </div>

      <CheckoutModals checkout={checkout} />
    </Layout>
  );
}
