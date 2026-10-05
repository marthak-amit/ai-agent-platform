import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { AlertTriangle, ArrowLeft, Bell, Loader2, RefreshCw, Trash2, Upload } from "lucide-react";
import Layout from "../components/Layout";
import { useRealtime } from "../context/RealtimeContext";
import { useToast } from "../context/ToastContext";
import {
  apiError,
  deletePaymentQr,
  fetchPaymentSettings,
  mediaSrc,
  savePaymentSettings,
  uploadPaymentQr,
} from "../api/chat";
import type { PaymentSettings } from "../types/chat";
import { requestBrowserPermission } from "../utils/notify";

const UPI_RE = /^[a-zA-Z0-9._-]{2,256}@[a-zA-Z][a-zA-Z0-9]{1,63}$/;

type PreviewLang = "en" | "hi" | "gu";

/** Settings → Payment details: UPI ID, payee name, static QR, bot/expiry knobs, alerts, and a live message preview. */
export default function PaymentDetails() {
  const { t } = useTranslation();
  const toast = useToast();
  const { notifyPrefs, setNotifyPrefs, clearSetupAlert } = useRealtime();

  const [settings, setSettings] = useState<PaymentSettings | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [upiId, setUpiId] = useState("");
  const [payee, setPayee] = useState("");
  const [resumeMins, setResumeMins] = useState("30");
  const [expiryHours, setExpiryHours] = useState("24");
  const [saving, setSaving] = useState(false);
  const [qrBusy, setQrBusy] = useState(false);
  const [previewLang, setPreviewLang] = useState<PreviewLang>("en");
  const [touched, setTouched] = useState(false);
  const [permission, setPermission] = useState<NotificationPermission | "unsupported">(
    typeof Notification === "undefined" ? "unsupported" : Notification.permission,
  );
  const fileInput = useRef<HTMLInputElement>(null);

  function hydrate(s: PaymentSettings) {
    setSettings(s);
    setUpiId(s.upi_id ?? "");
    setPayee(s.upi_payee_name ?? "");
    setResumeMins(String(s.bot_auto_resume_minutes));
    setExpiryHours(String(s.payment_expiry_hours));
  }

  async function load() {
    setLoadError(false);
    setSettings(null);
    try {
      hydrate(await fetchPaymentSettings());
    } catch {
      setLoadError(true);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const upiValid = upiId.trim() === "" || UPI_RE.test(upiId.trim());
  const dirty =
    !!settings &&
    (upiId.trim() !== (settings.upi_id ?? "") ||
      payee.trim() !== (settings.upi_payee_name ?? "") ||
      Number(resumeMins) !== settings.bot_auto_resume_minutes ||
      Number(expiryHours) !== settings.payment_expiry_hours);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (!upiValid) return;
    setSaving(true);
    try {
      const next = await savePaymentSettings({
        upi_id: upiId.trim(),
        upi_payee_name: payee.trim(),
        bot_auto_resume_minutes: Math.max(0, Math.min(1440, Number(resumeMins) || 0)),
        payment_expiry_hours: Math.max(1, Math.min(720, Number(expiryHours) || 24)),
      });
      hydrate(next);
      if (next.upi_configured) clearSetupAlert();
      toast.show(t("payments.pd_saved"), { kind: "success" });
    } catch (err) {
      toast.show(apiError(err).code === "INVALID_UPI_ID" ? t("payments.pd_invalid_upi") : t("payments.pd_save_failed"), { kind: "error" });
    } finally {
      setSaving(false);
    }
  }

  async function onQrPicked(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    setQrBusy(true);
    try {
      setSettings(await uploadPaymentQr(f));
    } catch {
      toast.show(t("payments.pd_save_failed"), { kind: "error" });
    } finally {
      setQrBusy(false);
    }
  }

  async function removeQr() {
    setQrBusy(true);
    try {
      setSettings(await deletePaymentQr());
    } catch {
      toast.show(t("payments.pd_save_failed"), { kind: "error" });
    } finally {
      setQrBusy(false);
    }
  }

  async function toggleBrowser(on: boolean) {
    if (on) {
      const result = await requestBrowserPermission();
      setPermission(result);
      if (result !== "granted") return;
    }
    setNotifyPrefs({ ...notifyPrefs, browser: on });
  }

  // preview reflects unsaved edits to UPI ID / payee, replacing the saved values in the rendered sample
  const preview = settings
    ? settings.preview[previewLang]
        .replace(settings.upi_id ?? "yourname@bank", upiId.trim() || "yourname@bank")
        .replace(settings.upi_payee_name ?? "\u0000", payee.trim() || "\u0000")
    : "";

  return (
    <Layout>
      <div className="max-w-3xl mx-auto space-y-4">
        <Link to="/settings" className="inline-flex items-center gap-1 text-sm text-brand-primaryDark font-medium hover:underline">
          <ArrowLeft size={14} /> {t("nav.settings")}
        </Link>
        <div>
          <h1 className="text-xl font-bold text-brand-secondary">{t("payments.pd_title")}</h1>
          <p className="text-sm text-gray-500">{t("payments.pd_subtitle")}</p>
        </div>

        {loadError ? (
          <div className="bg-white border border-gray-100 rounded-2xl p-10 flex flex-col items-center gap-2 text-center">
            <AlertTriangle size={28} className="text-red-400" />
            <p className="text-sm text-gray-600">{t("payments.pd_error")}</p>
            <button onClick={() => void load()} className="flex items-center gap-1.5 text-sm font-medium text-brand-primaryDark hover:underline">
              <RefreshCw size={14} /> {t("payments.retry")}
            </button>
          </div>
        ) : !settings ? (
          <div className="space-y-3" aria-busy="true" aria-label={t("payments.pd_loading")}>
            {[...Array(3)].map((_, i) => <div key={i} className="h-40 bg-white border border-gray-100 rounded-2xl animate-pulse" />)}
          </div>
        ) : (
          <>
            {/* The missing-UPI alert is the global banner in Layout (driven by setup_alert_active). */}

            <form onSubmit={save} className="bg-white border border-gray-100 rounded-2xl shadow-sm p-5 space-y-4" noValidate>
              <div>
                <label htmlFor="upi" className="block text-xs font-semibold uppercase tracking-wide text-gray-400 mb-1.5">{t("payments.pd_upi_id")}</label>
                <input
                  id="upi"
                  value={upiId}
                  onChange={(e) => setUpiId(e.target.value)}
                  onBlur={() => setTouched(true)}
                  placeholder="riyasarees@okaxis"
                  autoComplete="off"
                  aria-invalid={touched && !upiValid}
                  className={`w-full border rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand-primary ${touched && !upiValid ? "border-red-400" : "border-gray-200"}`}
                />
                <p className={`text-xs mt-1 ${touched && !upiValid ? "text-red-600" : "text-gray-400"}`}>
                  {touched && !upiValid ? t("payments.pd_invalid_upi") : t("payments.pd_upi_hint")}
                </p>
              </div>
              <div>
                <label htmlFor="payee" className="block text-xs font-semibold uppercase tracking-wide text-gray-400 mb-1.5">{t("payments.pd_payee")}</label>
                <input
                  id="payee"
                  value={payee}
                  maxLength={100}
                  onChange={(e) => setPayee(e.target.value)}
                  placeholder="Riya Sarees"
                  className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand-primary"
                />
                <p className="text-xs text-gray-400 mt-1">{t("payments.pd_payee_hint")}</p>
              </div>

              <div className="grid sm:grid-cols-2 gap-4">
                <div>
                  <label htmlFor="resume" className="block text-xs font-semibold uppercase tracking-wide text-gray-400 mb-1.5">{t("payments.pd_auto_resume")}</label>
                  <input id="resume" type="number" min={0} max={1440} value={resumeMins} onChange={(e) => setResumeMins(e.target.value)}
                    className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand-primary" />
                  <p className="text-xs text-gray-400 mt-1">{t("payments.pd_auto_resume_hint")}</p>
                </div>
                <div>
                  <label htmlFor="expiry" className="block text-xs font-semibold uppercase tracking-wide text-gray-400 mb-1.5">{t("payments.pd_expiry")}</label>
                  <input id="expiry" type="number" min={1} max={720} value={expiryHours} onChange={(e) => setExpiryHours(e.target.value)}
                    className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand-primary" />
                </div>
              </div>

              <button
                type="submit"
                disabled={saving || !dirty || (touched && !upiValid)}
                className="flex items-center gap-2 bg-brand-primary text-white font-semibold px-5 py-2.5 rounded-xl hover:bg-brand-primaryDark disabled:opacity-50 transition-colors"
              >
                {saving && <Loader2 size={15} className="animate-spin" />} {t("payments.pd_save")}
              </button>
            </form>

            {/* static QR */}
            <section className="bg-white border border-gray-100 rounded-2xl shadow-sm p-5">
              <h2 className="text-sm font-semibold text-brand-secondary">{t("payments.pd_qr")}</h2>
              <p className="text-xs text-gray-400 mt-0.5 mb-3">{t("payments.pd_qr_hint")}</p>
              <div className="flex items-center gap-4">
                {settings.upi_qr_url ? (
                  <img src={mediaSrc(settings.upi_qr_url)} alt="UPI QR" className="w-28 h-28 object-contain border border-gray-200 rounded-xl bg-white" />
                ) : (
                  <div className="w-28 h-28 border-2 border-dashed border-gray-200 rounded-xl flex items-center justify-center text-gray-300"><Upload size={22} /></div>
                )}
                <div className="flex flex-col gap-2">
                  <input ref={fileInput} type="file" accept="image/jpeg,image/png,image/webp" className="hidden" onChange={onQrPicked} />
                  <button type="button" onClick={() => fileInput.current?.click()} disabled={qrBusy}
                    className="flex items-center gap-1.5 text-sm font-medium px-3.5 py-2 border border-gray-200 rounded-lg hover:bg-brand-bg disabled:opacity-50">
                    {qrBusy ? <Loader2 size={14} className="animate-spin" /> : <Upload size={14} />} {t("payments.pd_qr_upload")}
                  </button>
                  {settings.upi_qr_url && (
                    <button type="button" onClick={() => void removeQr()} disabled={qrBusy}
                      className="flex items-center gap-1.5 text-sm font-medium px-3.5 py-2 text-red-600 hover:bg-red-50 rounded-lg disabled:opacity-50">
                      <Trash2 size={14} /> {t("payments.pd_qr_remove")}
                    </button>
                  )}
                </div>
              </div>
            </section>

            {/* preview */}
            <section className="bg-white border border-gray-100 rounded-2xl shadow-sm p-5">
              <div className="flex items-center justify-between gap-2 mb-3">
                <h2 className="text-sm font-semibold text-brand-secondary">{t("payments.pd_preview")}</h2>
                <div className="flex gap-1" role="tablist">
                  {(["en", "hi", "gu"] as PreviewLang[]).map((l) => (
                    <button key={l} role="tab" aria-selected={previewLang === l} onClick={() => setPreviewLang(l)}
                      className={`text-xs font-medium px-2.5 py-1 rounded ${previewLang === l ? "bg-brand-primary/15 text-brand-primaryDark" : "text-gray-400 hover:bg-gray-100"}`}>
                      {l.toUpperCase()}
                    </button>
                  ))}
                </div>
              </div>
              <div className="bg-[#E7F5EC] rounded-2xl p-4">
                <div className="max-w-sm bg-white rounded-2xl rounded-tl-sm shadow-sm px-4 py-3 text-sm text-gray-800 whitespace-pre-wrap break-words" data-testid="payment-preview">
                  {preview}
                </div>
              </div>
            </section>

            {/* alerts */}
            <section className="bg-white border border-gray-100 rounded-2xl shadow-sm p-5 space-y-3">
              <h2 className="text-sm font-semibold text-brand-secondary flex items-center gap-2"><Bell size={15} /> {t("payments.pd_notify_title")}</h2>
              <label className="flex items-center gap-3 text-sm text-gray-700 cursor-pointer">
                <input type="checkbox" checked={notifyPrefs.sound} onChange={(e) => setNotifyPrefs({ ...notifyPrefs, sound: e.target.checked })} className="w-4 h-4 accent-brand-primary" />
                {t("payments.pd_notify_sound")}
              </label>
              <label className="flex items-center gap-3 text-sm text-gray-700 cursor-pointer">
                <input type="checkbox" checked={notifyPrefs.browser && permission === "granted"} disabled={permission === "unsupported"}
                  onChange={(e) => void toggleBrowser(e.target.checked)} className="w-4 h-4 accent-brand-primary" />
                {t("payments.pd_notify_browser")}
              </label>
              {permission === "denied" && <p className="text-xs text-amber-700">{t("payments.pd_notify_blocked")}</p>}
              {permission === "unsupported" && <p className="text-xs text-gray-400">{t("payments.pd_notify_unsupported")}</p>}
            </section>
          </>
        )}
      </div>
    </Layout>
  );
}
