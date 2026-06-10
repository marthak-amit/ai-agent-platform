import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { api, updateProfile } from "../api/client";
import Layout from "../components/Layout";
import { useAuth } from "../context/AuthContext";
import { User, Bot, Globe, CheckCircle2, Store, Copy, ExternalLink, FileDown, Palette, Award, FlaskConical, X, ChevronRight } from "lucide-react";
import QRCode from "qrcode";
import { SandboxUI } from "./Sandbox";

const LANG_OPTIONS = [
  { code: "en", label: "English", native: "English" },
  { code: "hi", label: "Hindi", native: "हिंदी" },
  { code: "gu", label: "Gujarati", native: "ગુજરાતી" },
];

type Tab = "profile" | "agent" | "language" | "catalogue" | "compare";

const TABS: { key: Tab; label: string; icon: typeof User }[] = [
  { key: "profile", label: "Profile", icon: User },
  { key: "agent", label: "Agent Config", icon: Bot },
  { key: "catalogue", label: "Catalogue", icon: Store },
  { key: "language", label: "Language", icon: Globe },
  { key: "compare", label: "Why Us", icon: Award },
];

const COMPARISON_POINTS: { ours: string; theirs: string }[] = [
  { ours: "Completes orders directly inside WhatsApp chat", theirs: "Redirects customers to \"contact our team\"" },
  { ours: "Hindi, Gujarati & English dashboard UI", theirs: "English-only dashboard" },
  { ours: "Built-in product variants (color, size, stock)", theirs: "No variant support" },
  { ours: "Local pricing — ₹999/month", theirs: "$89/month (≈ ₹7,400)" },
  { ours: "Personal onboarding support, in your language", theirs: "Self-serve docs only" },
];

export default function Settings() {
  const { t } = useTranslation();
  const { client, refreshProfile, changeLanguage } = useAuth();
  const [activeTab, setActiveTab] = useState<Tab>("profile");

  const [businessName, setBusinessName] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [briefingEnabled, setBriefingEnabled] = useState(true);
  const [briefingTime, setBriefingTime] = useState("09:00");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [briefingSending, setBriefingSending] = useState(false);
  const [briefingResult, setBriefingResult] = useState<string | null>(null);
  const [langSaved, setLangSaved] = useState(false);
  const [showSandbox, setShowSandbox] = useState(false);

  // Payment settings state
  const [acceptsCod, setAcceptsCod] = useState(false);
  const [upiId, setUpiId] = useState("");
  const [paymentSaving, setPaymentSaving] = useState(false);
  const [paymentSaved, setPaymentSaved] = useState(false);

  // Catalogue tab state
  const [catSlug, setCatSlug] = useState("");
  const [catTagline, setCatTagline] = useState("");
  const [catTheme, setCatTheme] = useState("#6366F1");
  const [catSaving, setCatSaving] = useState(false);
  const [catSaved, setCatSaved] = useState(false);
  const [catError, setCatError] = useState<string | null>(null);
  const [qrDataUrl, setQrDataUrl] = useState<string | null>(null);
  useEffect(() => {
    if (client) {
      setBusinessName(client.business_name);
      setSystemPrompt(client.gemini_system_prompt);
      setBriefingEnabled(client.briefing_enabled ?? true);
      setBriefingTime(client.briefing_time ?? "09:00");
      setCatSlug(client.catalogue_slug ?? "");
      setCatTagline(client.catalogue_tagline ?? "");
      setCatTheme(client.catalogue_theme_color ?? "#6366F1");
      setAcceptsCod((client as { accepts_cod?: boolean }).accepts_cod ?? false);
      setUpiId((client as { upi_id?: string }).upi_id ?? "");
    }
  }, [client]);

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setSaved(false);
    await updateProfile({
      business_name: businessName,
      gemini_system_prompt: systemPrompt,
      briefing_enabled: briefingEnabled,
      briefing_time: briefingTime,
    });
    await refreshProfile();
    setSaving(false);
    setSaved(true);
    setTimeout(() => setSaved(false), 3000);
  }

  async function handleLangChange(code: string) {
    await changeLanguage(code);
    setLangSaved(true);
    setTimeout(() => setLangSaved(false), 2000);
  }

  async function handleCatalogueSave(e: React.FormEvent) {
    e.preventDefault();
    setCatSaving(true);
    setCatError(null);
    try {
      await updateProfile({
        catalogue_slug: catSlug,
        catalogue_tagline: catTagline,
        catalogue_theme_color: catTheme,
      } as Parameters<typeof updateProfile>[0]);
      await refreshProfile();
      setCatSaved(true);
      setTimeout(() => setCatSaved(false), 3000);
      // Generate QR
      const url = `${window.location.origin}/shop/${catSlug}`;
      const dataUrl = await QRCode.toDataURL(url, { width: 256, margin: 2 });
      setQrDataUrl(dataUrl);
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Save failed.";
      setCatError(detail);
    } finally {
      setCatSaving(false);
    }
  }

  function copyCatLink() {
    navigator.clipboard.writeText(`${window.location.origin}/shop/${catSlug}`);
  }

  function downloadQr() {
    if (!qrDataUrl) return;
    const a = document.createElement("a");
    a.href = qrDataUrl;
    a.download = `${catSlug}-qr.png`;
    a.click();
  }

  async function downloadPdf() {
    const r = await fetch(`${(import.meta.env.VITE_API_URL as string) || "http://localhost:8000"}/shop/${catSlug}/pdf`);
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${catSlug}-catalogue.pdf`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function handlePaymentSave(e: React.FormEvent) {
    e.preventDefault();
    setPaymentSaving(true);
    await updateProfile({ accepts_cod: acceptsCod, upi_id: upiId });
    await refreshProfile();
    setPaymentSaving(false);
    setPaymentSaved(true);
    setTimeout(() => setPaymentSaved(false), 3000);
  }

  async function handleSendBriefingNow() {
    setBriefingSending(true);
    setBriefingResult(null);
    try {
      const { data } = await api.post<{ status: string; preview: string }>("/briefing/send-now");
      setBriefingResult(`Sent! Preview:\n${data.preview}`);
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to send briefing.";
      setBriefingResult(`Error: ${detail}`);
    } finally {
      setBriefingSending(false);
    }
  }

  const navigate = useNavigate();
  const currentLang = client?.dashboard_language || "en";
  const initials = (client?.email ?? "U").slice(0, 2).toUpperCase();

  return (
    <Layout>
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-900">{t("settings.title")}</h1>
      </div>

      <div className="flex gap-6">
        {/* Left tab nav */}
        <div className="w-48 shrink-0">
          <nav className="flex flex-col gap-1">
            {TABS.map((tab) => {
              const Icon = tab.icon;
              const active = activeTab === tab.key;
              return (
                <button
                  key={tab.key}
                  onClick={() => setActiveTab(tab.key)}
                  className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium text-left transition-all duration-150 ${
                    active ? "bg-indigo-600 text-white shadow-sm" : "text-gray-600 hover:bg-gray-100 hover:text-gray-900"
                  }`}
                >
                  <Icon size={16} className={active ? "text-white" : "text-gray-400"} />
                  {tab.label}
                </button>
              );
            })}
          </nav>
        </div>

        {/* Content area */}
        <div className="flex-1 min-w-0">

          {/* Profile tab */}
          {activeTab === "profile" && (
            <form onSubmit={handleSave} className="bg-white rounded-xl border border-gray-100 shadow-sm p-6 max-w-xl">
              <div className="flex items-center gap-4 mb-6">
                <div className="w-16 h-16 bg-indigo-600 rounded-full flex items-center justify-center text-white text-xl font-bold">
                  {initials}
                </div>
                <div>
                  <div className="font-semibold text-gray-900">{client?.email}</div>
                  <div className="text-xs text-gray-400 mt-0.5 capitalize">{client?.plan_slug ?? "starter"} plan</div>
                </div>
              </div>

              <div className="flex flex-col gap-4">
                <div>
                  <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
                    {t("settings.business_name")}
                  </label>
                  <input
                    type="text"
                    value={businessName}
                    onChange={(e) => setBusinessName(e.target.value)}
                    className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition-all"
                    placeholder="Raj's Electronics"
                  />
                </div>

                <div className="flex items-center gap-3 pt-2">
                  <button
                    type="submit"
                    disabled={saving}
                    className="bg-indigo-600 text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors"
                  >
                    {saving ? t("settings.saving") : t("settings.save_changes")}
                  </button>
                  {saved && (
                    <span className="flex items-center gap-1.5 text-sm text-green-600 font-medium">
                      <CheckCircle2 size={15} /> {t("settings.saved")}
                    </span>
                  )}
                </div>
              </div>
            </form>
          )}

          {/* Setup Guide — shown on profile tab when onboarding incomplete */}
          {activeTab === "profile" && client && !client.onboarding_completed && (
            <div className="bg-amber-50 border border-amber-200 rounded-xl p-5 max-w-xl mt-4">
              <h3 className="text-sm font-semibold text-amber-900 mb-1">Setup Guide</h3>
              <p className="text-xs text-amber-700 mb-3">
                Complete these steps to get your agent live.
              </p>
              <div className="flex flex-col gap-2 mb-4">
                {[
                  { step: 1, label: "Business profile", done: client.onboarding_step >= 1 },
                  { step: 2, label: "Products added", done: client.onboarding_step >= 2 },
                  { step: 3, label: "Agent configured", done: client.onboarding_step >= 3 },
                  { step: 4, label: "WhatsApp connected", done: client.onboarding_step >= 4 },
                  { step: 5, label: "Agent tested", done: client.onboarding_step >= 5 },
                ].map((item) => (
                  <div key={item.step} className="flex items-center gap-2 text-xs">
                    {item.done ? (
                      <CheckCircle2 size={14} className="text-green-500 shrink-0" />
                    ) : (
                      <div className="w-3.5 h-3.5 rounded-full border-2 border-amber-400 shrink-0" />
                    )}
                    <span className={item.done ? "text-gray-600 line-through" : "text-amber-800 font-medium"}>
                      {item.label}
                    </span>
                  </div>
                ))}
              </div>
              <button
                onClick={() => navigate("/onboarding")}
                className="flex items-center gap-2 bg-amber-500 hover:bg-amber-600 text-white text-xs font-semibold px-4 py-2 rounded-lg transition-colors"
              >
                Resume Setup <ChevronRight size={13} />
              </button>
            </div>
          )}

          {/* Agent Config tab */}
          {activeTab === "agent" && (
            <form onSubmit={handleSave} className="bg-white rounded-xl border border-gray-100 shadow-sm p-6 max-w-xl flex flex-col gap-5">
              <div>
                <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
                  {t("settings.agent_prompt")}
                </label>
                <p className="text-xs text-gray-400 mb-2">{t("settings.agent_prompt_hint")}</p>
                <textarea
                  value={systemPrompt}
                  onChange={(e) => setSystemPrompt(e.target.value)}
                  rows={8}
                  className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent resize-none"
                  placeholder="You are a helpful assistant for [Business name]. Help customers with..."
                />
              </div>

              <div className="border border-gray-100 rounded-xl p-4 flex flex-col gap-4 bg-gray-50">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm font-medium text-gray-800">Daily briefing</p>
                    <p className="text-xs text-gray-400 mt-0.5">WhatsApp summary of yesterday's activity every morning.</p>
                  </div>
                  <button
                    type="button"
                    onClick={() => setBriefingEnabled((v) => !v)}
                    className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${briefingEnabled ? "bg-indigo-600" : "bg-gray-300"}`}
                  >
                    <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${briefingEnabled ? "translate-x-6" : "translate-x-1"}`} />
                  </button>
                </div>

                {briefingEnabled && (
                  <div className="flex items-center gap-3">
                    <label className="text-sm text-gray-600 shrink-0">Send at</label>
                    <input
                      type="time"
                      value={briefingTime}
                      onChange={(e) => setBriefingTime(e.target.value)}
                      className="border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    />
                  </div>
                )}

                <div className="flex flex-col gap-2">
                  <button
                    type="button"
                    onClick={handleSendBriefingNow}
                    disabled={briefingSending}
                    className="self-start bg-indigo-50 text-indigo-700 border border-indigo-200 rounded-lg px-4 py-2 text-sm font-medium hover:bg-indigo-100 disabled:opacity-50 transition-colors"
                  >
                    {briefingSending ? "Sending…" : "Get briefing now"}
                  </button>
                  {briefingResult && (
                    <pre className="text-xs bg-white border border-gray-200 rounded-lg p-3 whitespace-pre-wrap text-gray-700 max-h-48 overflow-y-auto">
                      {briefingResult}
                    </pre>
                  )}
                </div>
              </div>

              <div className="flex items-center gap-3">
                <button
                  type="submit"
                  disabled={saving}
                  className="bg-indigo-600 text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors"
                >
                  {saving ? t("settings.saving") : t("settings.save_changes")}
                </button>
                {saved && (
                  <span className="flex items-center gap-1.5 text-sm text-green-600 font-medium">
                    <CheckCircle2 size={15} /> {t("settings.saved")}
                  </span>
                )}
              </div>
            </form>
          )}

          {activeTab === "agent" && (
            <form onSubmit={handlePaymentSave} className="bg-white rounded-xl border border-gray-100 shadow-sm p-6 max-w-xl flex flex-col gap-5 mt-4">
              <div>
                <h3 className="text-sm font-semibold text-gray-800 mb-1">Payment Settings</h3>
                <p className="text-xs text-gray-400">Most Surat textile traders prefer UPI-only. Enable COD only if you want to offer cash on delivery.</p>
              </div>

              <div className="flex items-center justify-between bg-amber-50 border border-amber-100 rounded-xl p-4">
                <div>
                  <p className="text-sm font-medium text-gray-800">Accept Cash on Delivery (COD)</p>
                  <p className="text-xs text-gray-500 mt-0.5">OFF = UPI only (recommended for textile traders)</p>
                </div>
                <button
                  type="button"
                  onClick={() => setAcceptsCod((v) => !v)}
                  className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${acceptsCod ? "bg-indigo-600" : "bg-gray-300"}`}
                >
                  <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${acceptsCod ? "translate-x-6" : "translate-x-1"}`} />
                </button>
              </div>

              <div>
                <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
                  UPI ID (fallback when Razorpay is not configured)
                </label>
                <input
                  type="text"
                  value={upiId}
                  onChange={(e) => setUpiId(e.target.value)}
                  placeholder="yourshop@paytm"
                  className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                />
                <p className="text-xs text-gray-400 mt-1">Sent as plain text when Razorpay QR is unavailable.</p>
              </div>

              <div className="flex items-center gap-3">
                <button
                  type="submit"
                  disabled={paymentSaving}
                  className="bg-indigo-600 text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors"
                >
                  {paymentSaving ? "Saving…" : "Save Payment Settings"}
                </button>
                {paymentSaved && (
                  <span className="flex items-center gap-1.5 text-sm text-green-600 font-medium">
                    <CheckCircle2 size={15} /> Saved
                  </span>
                )}
              </div>
            </form>
          )}

          {/* Test Agent section — shown inline under Agent Config */}
          {activeTab === "agent" && !showSandbox && (
            <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-6 max-w-xl mt-4 flex items-center justify-between">
              <div>
                <p className="text-sm font-semibold text-gray-800">Test Your Agent</p>
                <p className="text-xs text-gray-400 mt-0.5">Preview how your agent responds before going live.</p>
              </div>
              <button
                onClick={() => setShowSandbox(true)}
                className="flex items-center gap-2 bg-indigo-600 text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-indigo-700 transition-colors"
              >
                <FlaskConical size={15} />
                Test Your Agent
              </button>
            </div>
          )}

          {activeTab === "agent" && showSandbox && (
            <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-6 max-w-2xl mt-4">
              <div className="flex items-center justify-between mb-4">
                <p className="text-sm font-semibold text-gray-800 flex items-center gap-2">
                  <FlaskConical size={15} className="text-indigo-600" />
                  Agent Sandbox
                </p>
                <button
                  onClick={() => setShowSandbox(false)}
                  className="text-gray-400 hover:text-gray-600 transition-colors"
                >
                  <X size={16} />
                </button>
              </div>
              <SandboxUI />
            </div>
          )}

          {/* Catalogue tab */}
          {activeTab === "catalogue" && (
            <form onSubmit={handleCatalogueSave} className="bg-white rounded-xl border border-gray-100 shadow-sm p-6 max-w-xl flex flex-col gap-5">
              <div>
                <h2 className="text-base font-semibold text-gray-900 mb-1">Your Public Catalogue</h2>
                <p className="text-xs text-gray-400">Customers can browse and order without logging in.</p>
              </div>

              {/* Catalogue URL */}
              {catSlug && (
                <div className="bg-gray-50 rounded-xl p-4 border border-gray-200">
                  <p className="text-xs text-gray-400 mb-1.5 font-medium uppercase tracking-wide">Catalogue URL</p>
                  <div className="flex items-center gap-2">
                    <code className="text-sm text-indigo-700 bg-indigo-50 px-3 py-1.5 rounded-lg flex-1 truncate">
                      {window.location.origin}/shop/{catSlug}
                    </code>
                    <button
                      type="button"
                      onClick={copyCatLink}
                      title="Copy link"
                      className="p-2 text-gray-500 hover:text-gray-900 hover:bg-gray-100 rounded-lg transition-colors"
                    >
                      <Copy size={15} />
                    </button>
                    <a
                      href={`/shop/${catSlug}`}
                      target="_blank"
                      rel="noreferrer"
                      className="p-2 text-gray-500 hover:text-gray-900 hover:bg-gray-100 rounded-lg transition-colors"
                    >
                      <ExternalLink size={15} />
                    </a>
                  </div>
                </div>
              )}

              {/* Slug */}
              <div>
                <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
                  Catalogue Slug
                </label>
                <input
                  type="text"
                  value={catSlug}
                  onChange={(e) => setCatSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))}
                  className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                  placeholder="riyasarees"
                />
                <p className="text-xs text-gray-400 mt-1">Only lowercase letters, numbers, and hyphens.</p>
              </div>

              {/* Tagline */}
              <div>
                <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
                  Tagline
                </label>
                <input
                  type="text"
                  value={catTagline}
                  onChange={(e) => setCatTagline(e.target.value)}
                  className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                  placeholder="Premium Banarasi Sarees from Surat"
                />
              </div>

              {/* Theme color */}
              <div>
                <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
                  Theme Color
                </label>
                <div className="flex items-center gap-3">
                  <input
                    type="color"
                    value={catTheme}
                    onChange={(e) => setCatTheme(e.target.value)}
                    className="w-10 h-10 rounded-lg border border-gray-200 cursor-pointer p-0.5"
                  />
                  <div className="flex items-center gap-1.5">
                    <Palette size={14} className="text-gray-400" />
                    <span className="text-sm text-gray-600 font-mono">{catTheme}</span>
                  </div>
                </div>
              </div>

              {catError && (
                <p className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg">{catError}</p>
              )}

              <div className="flex items-center gap-3">
                <button
                  type="submit"
                  disabled={catSaving || !catSlug}
                  className="bg-indigo-600 text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors"
                >
                  {catSaving ? "Saving…" : "Save & Preview"}
                </button>
                {catSaved && (
                  <span className="flex items-center gap-1.5 text-sm text-green-600 font-medium">
                    <CheckCircle2 size={15} /> Saved
                  </span>
                )}
              </div>

              {/* QR Code */}
              {qrDataUrl && (
                <div className="border border-gray-100 rounded-xl p-4 flex flex-col gap-3">
                  <p className="text-sm font-medium text-gray-800">QR Code</p>
                  <p className="text-xs text-gray-400">Print and put in your shop for customers to scan.</p>
                  <img src={qrDataUrl} alt="QR code" className="w-32 h-32 rounded-xl border border-gray-200" />
                  <button
                    type="button"
                    onClick={downloadQr}
                    className="self-start flex items-center gap-1.5 bg-gray-100 text-gray-700 px-4 py-2 rounded-lg text-sm font-medium hover:bg-gray-200 transition-colors"
                  >
                    <FileDown size={14} />
                    Download QR Code
                  </button>
                </div>
              )}

              {/* PDF download */}
              {catSlug && (
                <div className="border border-gray-100 rounded-xl p-4">
                  <p className="text-sm font-medium text-gray-800 mb-1">PDF Catalogue</p>
                  <p className="text-xs text-gray-400 mb-3">Download a print-ready PDF with all your products.</p>
                  <button
                    type="button"
                    onClick={downloadPdf}
                    className="flex items-center gap-1.5 bg-gray-100 text-gray-700 px-4 py-2 rounded-lg text-sm font-medium hover:bg-gray-200 transition-colors"
                  >
                    <FileDown size={14} />
                    Download PDF Catalogue
                  </button>
                </div>
              )}
            </form>
          )}

          {/* Language tab */}
          {activeTab === "language" && (
            <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-6 max-w-xl">
              <h2 className="text-base font-semibold text-gray-900 mb-1">{t("settings.language")}</h2>
              <p className="text-xs text-gray-400 mb-5">Auto-saves on selection.</p>

              <div className="flex flex-col gap-3">
                {LANG_OPTIONS.map((opt) => (
                  <label
                    key={opt.code}
                    className={`flex items-center gap-4 p-4 rounded-xl border cursor-pointer transition-all ${
                      currentLang === opt.code
                        ? "border-indigo-300 bg-indigo-50"
                        : "border-gray-200 hover:border-gray-300 hover:bg-gray-50"
                    }`}
                  >
                    <input
                      type="radio"
                      name="dashboard_language"
                      value={opt.code}
                      checked={currentLang === opt.code}
                      onChange={() => handleLangChange(opt.code)}
                      className="w-4 h-4 accent-indigo-600"
                    />
                    <div>
                      <div className={`text-sm font-medium ${currentLang === opt.code ? "text-indigo-700" : "text-gray-700"}`}>{opt.label}</div>
                      <div className="text-xs text-gray-400">{opt.native}</div>
                    </div>
                    {currentLang === opt.code && <CheckCircle2 size={16} className="text-indigo-600 ml-auto" />}
                  </label>
                ))}
              </div>

              {langSaved && (
                <p className="flex items-center gap-1.5 text-sm text-green-600 font-medium mt-4">
                  <CheckCircle2 size={15} /> {t("settings.language_updated")}
                </p>
              )}
            </div>
          )}

          {/* Why Us / Competitor analysis tab */}
          {activeTab === "compare" && (
            <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-6 max-w-2xl">
              <h2 className="text-base font-semibold text-gray-900 mb-1 flex items-center gap-2">
                <Award size={18} className="text-indigo-600" />
                Why we're better than other platforms
              </h2>
              <p className="text-xs text-gray-400 mb-5">
                Use these talking points in your sales pitch — they're based on real
                weaknesses we've observed in competitor platforms like TailorTalk.
              </p>

              <div className="overflow-hidden rounded-xl border border-gray-100">
                <div className="grid grid-cols-2 bg-gray-50 text-xs font-semibold text-gray-500 uppercase tracking-wide">
                  <div className="px-4 py-2.5 border-r border-gray-100">Us ✅</div>
                  <div className="px-4 py-2.5">Them ❌</div>
                </div>
                {COMPARISON_POINTS.map((point, i) => (
                  <div key={i} className={`grid grid-cols-2 text-sm ${i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}`}>
                    <div className="px-4 py-3 border-r border-gray-100 text-gray-800 flex items-start gap-2">
                      <CheckCircle2 size={15} className="text-green-500 shrink-0 mt-0.5" />
                      {point.ours}
                    </div>
                    <div className="px-4 py-3 text-gray-400">{point.theirs}</div>
                  </div>
                ))}
              </div>

              <p className="text-xs text-gray-400 mt-5">
                Tip: Lead with "completes orders directly in WhatsApp" — that's the #1
                reason businesses switch. Competitor platforms often dead-end into
                "please contact our team to place an order", losing the sale.
              </p>
            </div>
          )}
        </div>
      </div>
    </Layout>
  );
}
