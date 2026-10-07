import { useState } from "react";
import { useTranslation } from "react-i18next";
import { CheckCircle2, Loader2 } from "lucide-react";
import { updateProfile } from "../../api/client";
import { parseApiError } from "../../api/billing";
import { useAuth } from "../../context/AuthContext";
import { useBilling } from "../../context/BillingContext";
import { gstTreatment, isValidGstin, normaliseGstin } from "../../utils/gst";

const ADDRESS_MAX = 300;

/**
 * GSTIN + billing address, printed on the tax invoice. The GSTIN also decides the tax split at checkout
 * (same state as the seller → CGST + SGST, another state → IGST), so plan prices are re-fetched after saving.
 */
export default function BillingDetailsCard({ onSaved }: { onSaved: () => void }) {
  const { t } = useTranslation();
  const { client, refreshProfile } = useAuth();
  const { state } = useBilling();
  const [gstin, setGstin] = useState(client?.gst_number ?? "");
  const [address, setAddress] = useState(client?.business_address ?? "");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  const valid = isValidGstin(gstin);
  const treatment = gstTreatment(gstin, state?.seller_state_code ?? "");
  const dirty =
    normaliseGstin(gstin) !== (client?.gst_number ?? "") || address.trim() !== (client?.business_address ?? "").trim();

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!valid || saving) return;
    setSaving(true);
    setSaved(false);
    setServerError(null);
    try {
      await updateProfile({ gst_number: normaliseGstin(gstin), business_address: address.trim() });
      await refreshProfile();
      setSaved(true);
      onSaved();
    } catch (err) {
      const p = parseApiError(err);
      setServerError(p.status === 422 ? t("billing.details_invalid_gstin") : t("billing.details_save_error"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="rounded-2xl bg-white border border-gray-200 p-6">
      <h2 className="text-base font-bold text-brand-secondary">{t("billing.details_title")}</h2>
      <p className="text-sm text-gray-500 mt-1">{t("billing.details_desc")}</p>

      <form onSubmit={save} className="mt-4 grid gap-4 md:grid-cols-2">
        <div>
          <label htmlFor="billing-gstin" className="block text-sm font-medium text-gray-700 mb-1">
            {t("billing.details_gstin")} <span className="text-gray-400 font-normal">({t("billing.optional")})</span>
          </label>
          <input
            id="billing-gstin"
            value={gstin}
            onChange={(e) => {
              setGstin(normaliseGstin(e.target.value).slice(0, 15));
              setSaved(false);
            }}
            placeholder="24ABCDE1234F1Z5"
            maxLength={15}
            autoComplete="off"
            aria-invalid={!valid}
            aria-describedby="billing-gstin-help"
            className={`w-full rounded-lg border px-3 py-2 text-sm font-mono tracking-wide focus:outline-none focus:ring-2 focus:ring-brand-primary ${
              valid ? "border-gray-300" : "border-brand-accent"
            }`}
          />
          <p id="billing-gstin-help" className={`mt-1 text-xs ${valid ? "text-gray-500" : "text-brand-accent"}`}>
            {!valid
              ? t("billing.details_invalid_gstin")
              : treatment
                ? t(treatment.intraState ? "billing.details_tax_intra" : "billing.details_tax_inter", { state: treatment.stateName })
                : t("billing.details_gstin_help")}
          </p>
        </div>

        <div>
          <label htmlFor="billing-address" className="block text-sm font-medium text-gray-700 mb-1">
            {t("billing.details_address")}
          </label>
          <textarea
            id="billing-address"
            value={address}
            onChange={(e) => {
              setAddress(e.target.value.slice(0, ADDRESS_MAX));
              setSaved(false);
            }}
            rows={3}
            className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-primary"
          />
        </div>

        <div className="md:col-span-2 flex flex-wrap items-center gap-3">
          <button
            type="submit"
            disabled={!valid || !dirty || saving}
            className="inline-flex items-center gap-2 rounded-lg bg-brand-primary hover:bg-brand-primaryDark disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-semibold px-4 py-2 transition-colors"
          >
            {saving && <Loader2 size={14} className="animate-spin" />}
            {t("billing.details_save")}
          </button>
          {saved && (
            <span role="status" className="inline-flex items-center gap-1.5 text-sm text-brand-primaryDark">
              <CheckCircle2 size={15} /> {t("billing.details_saved")}
            </span>
          )}
          {serverError && (
            <span role="alert" className="text-sm text-brand-accent">
              {serverError}
            </span>
          )}
        </div>
      </form>
    </section>
  );
}
