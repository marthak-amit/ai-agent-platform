import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { api, getTeam, inviteTeamMember, updateProfile, updateTeamMember } from "../api/client";
import Layout from "../components/Layout";
import { useAuth } from "../context/AuthContext";
import { User, Bot, Globe, CheckCircle2, Store, Copy, ExternalLink, FileDown, Palette, Award, FlaskConical, X, ChevronRight, CreditCard, AlertTriangle, UsersRound } from "lucide-react";
import QRCode from "qrcode";
import { SandboxUI } from "./Sandbox";
import type { PermissionKey, TeamMember } from "../types";

const LANG_OPTIONS = [
  { code: "en", label: "English", native: "English" },
  { code: "hi", label: "Hindi", native: "हिंदी" },
  { code: "gu", label: "Gujarati", native: "ગુજરાતી" },
];

// Mirrors app.models.user.PERMISSION_KEYS / MANAGER_PRESET_PERMISSIONS / STAFF_PRESET_PERMISSIONS.
const PERMISSION_OPTIONS: { key: PermissionKey; label: string }[] = [
  { key: "catalog_edit", label: "Edit catalogue" },
  { key: "comment_settings", label: "Comment reply settings" },
  { key: "nudge_settings", label: "Follow-up nudge settings" },
  { key: "order_view", label: "View orders" },
  { key: "manual_reply", label: "Chat takeover / manual reply" },
  { key: "mark_packed", label: "Mark orders packed" },
  { key: "manual_utility_send", label: "Send dispatch/shipping message" },
  { key: "analytics_view", label: "View analytics" },
];
const MANAGER_PRESET: PermissionKey[] = PERMISSION_OPTIONS.map((p) => p.key);
const STAFF_PRESET: PermissionKey[] = ["order_view", "manual_reply", "mark_packed", "manual_utility_send"];

type Tab = "profile" | "agent" | "language" | "catalogue" | "payment" | "compare" | "team";

const ALL_TABS: { key: Tab; label: string; icon: typeof User; ownerOnly?: boolean }[] = [
  { key: "profile", label: "Profile", icon: User },
  { key: "agent", label: "Agent Config", icon: Bot },
  { key: "catalogue", label: "Catalogue", icon: Store },
  { key: "payment", label: "Payment", icon: CreditCard },
  { key: "team", label: "Team", icon: UsersRound, ownerOnly: true },
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

  // Team tab state
  const [team, setTeam] = useState<TeamMember[]>([]);
  const [teamLoading, setTeamLoading] = useState(false);
  const [teamError, setTeamError] = useState<string | null>(null);
  const [savingMemberId, setSavingMemberId] = useState<number | null>(null);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<"manager" | "staff">("staff");
  const [inviting, setInviting] = useState(false);
  const [inviteLink, setInviteLink] = useState<string | null>(null);

  // Payment settings state
  const [acceptsCod, setAcceptsCod] = useState(false);
  const [upiId, setUpiId] = useState("");
  const [upiDisplayName, setUpiDisplayName] = useState("");
  const [codLimit, setCodLimit] = useState<number | "">("");
  const [acceptsUpi, setAcceptsUpi] = useState(true);
  const [acceptsBankTransfer, setAcceptsBankTransfer] = useState(false);
  const [bankAccountName, setBankAccountName] = useState("");
  const [bankAccountNumber, setBankAccountNumber] = useState("");
  const [bankIfsc, setBankIfsc] = useState("");
  const [razorpayKeyId, setRazorpayKeyId] = useState("");
  const [razorpayKeySecret, setRazorpayKeySecret] = useState("");
  const [paymentInstructions, setPaymentInstructions] = useState("");
  const [paymentSaving, setPaymentSaving] = useState(false);
  const [paymentSaved, setPaymentSaved] = useState(false);

  // Delivery time state
  const [deliveryMin, setDeliveryMin] = useState<number>(3);
  const [deliveryMax, setDeliveryMax] = useState<number>(7);
  const [deliverySaving, setDeliverySaving] = useState(false);
  const [deliverySaved, setDeliverySaved] = useState(false);

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
      const c = client as {
        accepts_cod?: boolean; upi_id?: string; upi_display_name?: string;
        cod_limit?: number | null; accepts_upi?: boolean; accepts_bank_transfer?: boolean;
        bank_account_name?: string; bank_account_number?: string; bank_ifsc?: string;
        razorpay_key_id?: string; razorpay_key_secret?: string; payment_instructions?: string;
        delivery_days_min?: number; delivery_days_max?: number;
      };
      setAcceptsCod(c.accepts_cod ?? false);
      setUpiId(c.upi_id ?? "");
      setUpiDisplayName(c.upi_display_name ?? "");
      setCodLimit(c.cod_limit ?? "");
      setAcceptsUpi(c.accepts_upi ?? true);
      setAcceptsBankTransfer(c.accepts_bank_transfer ?? false);
      setBankAccountName(c.bank_account_name ?? "");
      setBankAccountNumber(c.bank_account_number ?? "");
      setBankIfsc(c.bank_ifsc ?? "");
      setRazorpayKeyId(c.razorpay_key_id ?? "");
      setRazorpayKeySecret(c.razorpay_key_secret ?? "");
      setPaymentInstructions(c.payment_instructions ?? "");
      setDeliveryMin(c.delivery_days_min ?? 3);
      setDeliveryMax(c.delivery_days_max ?? 7);
    }
  }, [client]);

  async function loadTeam() {
    setTeamLoading(true);
    setTeamError(null);
    try {
      setTeam(await getTeam());
    } catch {
      setTeamError("Failed to load team members.");
    } finally {
      setTeamLoading(false);
    }
  }

  useEffect(() => {
    if (activeTab === "team" && client?.current_user.is_owner) {
      loadTeam();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);

  async function handleInvite(e: React.FormEvent) {
    e.preventDefault();
    setInviting(true);
    setTeamError(null);
    setInviteLink(null);
    try {
      const { invite_link } = await inviteTeamMember(inviteEmail, inviteRole);
      setInviteLink(invite_link);
      setInviteEmail("");
      await loadTeam();
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to create invite.";
      setTeamError(detail);
    } finally {
      setInviting(false);
    }
  }

  async function handleTogglePermission(member: TeamMember, key: PermissionKey) {
    const nextPermissions = member.permissions.includes(key)
      ? member.permissions.filter((p) => p !== key)
      : [...member.permissions, key];
    setSavingMemberId(member.id);
    try {
      const updated = await updateTeamMember(member.id, { permissions: nextPermissions });
      setTeam((prev) => prev.map((m) => (m.id === member.id ? updated : m)));
    } catch {
      setTeamError("Failed to update permissions.");
    } finally {
      setSavingMemberId(null);
    }
  }

  async function handleApplyPreset(member: TeamMember, role: "manager" | "staff") {
    const preset = role === "manager" ? MANAGER_PRESET : STAFF_PRESET;
    setSavingMemberId(member.id);
    try {
      const updated = await updateTeamMember(member.id, { role, permissions: preset });
      setTeam((prev) => prev.map((m) => (m.id === member.id ? updated : m)));
    } catch {
      setTeamError("Failed to update role.");
    } finally {
      setSavingMemberId(null);
    }
  }

  async function handleToggleActive(member: TeamMember) {
    setSavingMemberId(member.id);
    try {
      const updated = await updateTeamMember(member.id, { is_active: !member.is_active });
      setTeam((prev) => prev.map((m) => (m.id === member.id ? updated : m)));
    } catch {
      setTeamError("Failed to update status.");
    } finally {
      setSavingMemberId(null);
    }
  }

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
    await updateProfile({
      accepts_upi: acceptsUpi,
      upi_id: upiId,
      upi_display_name: upiDisplayName,
      accepts_cod: acceptsCod,
      cod_limit: codLimit === "" ? undefined : Number(codLimit),
      accepts_bank_transfer: acceptsBankTransfer,
      bank_account_name: bankAccountName,
      // Only send if changed from the masked placeholder
      ...(bankAccountNumber && !bankAccountNumber.startsWith("****") ? { bank_account_number: bankAccountNumber } : {}),
      bank_ifsc: bankIfsc,
      razorpay_key_id: razorpayKeyId,
      // Only send if changed from the masked placeholder
      ...(razorpayKeySecret && razorpayKeySecret !== "****" ? { razorpay_key_secret: razorpayKeySecret } : {}),
      payment_instructions: paymentInstructions,
    } as Parameters<typeof updateProfile>[0]);
    await refreshProfile();
    setPaymentSaving(false);
    setPaymentSaved(true);
    setTimeout(() => setPaymentSaved(false), 3000);
  }

  async function handleDeliverySave(e: React.FormEvent) {
    e.preventDefault();
    setDeliverySaving(true);
    await updateProfile({ delivery_days_min: deliveryMin, delivery_days_max: deliveryMax } as Parameters<typeof updateProfile>[0]);
    await refreshProfile();
    setDeliverySaving(false);
    setDeliverySaved(true);
    setTimeout(() => setDeliverySaved(false), 3000);
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
  const isOwner = !!client?.current_user.is_owner;
  const TABS = ALL_TABS.filter((tab) => !tab.ownerOnly || isOwner);

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
                    active ? "bg-brand-primaryDark text-white shadow-sm" : "text-gray-600 hover:bg-gray-100 hover:text-gray-900"
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
                <div className="w-16 h-16 bg-brand-primaryDark rounded-full flex items-center justify-center text-white text-xl font-bold">
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
                    className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent transition-all"
                    placeholder="Raj's Electronics"
                  />
                </div>

                <div className="flex items-center gap-3 pt-2">
                  <button
                    type="submit"
                    disabled={saving}
                    className="bg-brand-primaryDark text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-brand-primary/90 disabled:opacity-50 transition-colors"
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
                  className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent resize-none"
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
                    className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${briefingEnabled ? "bg-brand-primary" : "bg-gray-300"}`}
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
                      className="border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand-primary"
                    />
                  </div>
                )}

                <div className="flex flex-col gap-2">
                  <button
                    type="button"
                    onClick={handleSendBriefingNow}
                    disabled={briefingSending}
                    className="self-start bg-brand-primary/5 text-brand-primaryDark border border-brand-primary/20 rounded-lg px-4 py-2 text-sm font-medium hover:bg-brand-primary/10 disabled:opacity-50 transition-colors"
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
                  className="bg-brand-primaryDark text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-brand-primary/90 disabled:opacity-50 transition-colors"
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


          {/* Delivery time section */}
          {activeTab === "agent" && (
            <form onSubmit={handleDeliverySave} className="bg-white rounded-xl border border-gray-100 shadow-sm p-6 max-w-xl flex flex-col gap-5 mt-4">
              <div>
                <h3 className="text-sm font-semibold text-gray-800 mb-1">Delivery Time</h3>
                <p className="text-xs text-gray-400">Default delivery time shown to customers in WhatsApp messages and the public catalogue.</p>
              </div>
              <div className="flex gap-4 items-end">
                <div className="flex-1">
                  <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
                    Min Days
                  </label>
                  <input
                    type="number"
                    min={1}
                    value={deliveryMin}
                    onChange={(e) => setDeliveryMin(Number(e.target.value))}
                    className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent"
                  />
                </div>
                <div className="flex-1">
                  <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
                    Max Days
                  </label>
                  <input
                    type="number"
                    min={1}
                    value={deliveryMax}
                    onChange={(e) => setDeliveryMax(Number(e.target.value))}
                    className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent"
                  />
                </div>
                <div className="shrink-0 pb-0.5 text-sm text-gray-500">business days</div>
              </div>
              <p className="text-xs text-gray-400 -mt-3">
                Preview: <span className="font-medium text-gray-700">🚚 {deliveryMin}–{deliveryMax} business days</span>
              </p>
              <div className="flex items-center gap-3">
                <button
                  type="submit"
                  disabled={deliverySaving}
                  className="bg-brand-primaryDark text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-brand-primary/90 disabled:opacity-50 transition-colors"
                >
                  {deliverySaving ? "Saving…" : "Save Delivery Settings"}
                </button>
                {deliverySaved && (
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
                className="flex items-center gap-2 bg-brand-primaryDark text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-brand-primary/90 transition-colors"
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
                  <FlaskConical size={15} className="text-brand-primaryDark" />
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
                    <code className="text-sm text-brand-primaryDark bg-brand-primary/5 px-3 py-1.5 rounded-lg flex-1 truncate">
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
                  className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent"
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
                  className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent"
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
                  className="bg-brand-primaryDark text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-brand-primary/90 disabled:opacity-50 transition-colors"
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

          {/* Payment tab */}
          {activeTab === "payment" && (
            <form onSubmit={handlePaymentSave} className="flex flex-col gap-4 max-w-xl">

              {/* Section A — UPI */}
              <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-6 flex flex-col gap-4">
                <div className="flex items-center justify-between">
                  <div>
                    <h3 className="text-sm font-semibold text-gray-800">UPI Payments</h3>
                    <p className="text-xs text-gray-400 mt-0.5">Most common for Indian SMBs. GPay, PhonePe, Paytm.</p>
                  </div>
                  <button
                    type="button"
                    onClick={() => setAcceptsUpi((v) => !v)}
                    className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${acceptsUpi ? "bg-brand-primary" : "bg-gray-300"}`}
                  >
                    <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${acceptsUpi ? "translate-x-6" : "translate-x-1"}`} />
                  </button>
                </div>

                {acceptsUpi && (
                  <div className="flex flex-col gap-3">
                    <div>
                      <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">UPI ID</label>
                      <input
                        type="text"
                        value={upiId}
                        onChange={(e) => setUpiId(e.target.value)}
                        placeholder="riyasarees@paytm"
                        className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Display Name</label>
                      <input
                        type="text"
                        value={upiDisplayName}
                        onChange={(e) => setUpiDisplayName(e.target.value)}
                        placeholder="Riya Sarees"
                        className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent"
                      />
                      <p className="text-xs text-gray-400 mt-1">Name shown on UPI apps next to your UPI ID.</p>
                    </div>
                    {upiId && (
                      <div className="bg-brand-primary/5 border border-brand-primary/20 rounded-lg px-4 py-2.5 text-sm text-brand-primaryDark">
                        <span className="font-medium">Preview: </span>
                        Pay via UPI: <span className="font-mono">{upiId}</span>
                        {upiDisplayName && <span> ({upiDisplayName})</span>}
                      </div>
                    )}
                  </div>
                )}
              </div>

              {/* Section B — COD */}
              <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-6 flex flex-col gap-4">
                <div className="flex items-center justify-between">
                  <div>
                    <h3 className="text-sm font-semibold text-gray-800">Cash on Delivery</h3>
                    <p className="text-xs text-gray-400 mt-0.5">Customer pays at delivery. Not recommended for high-value orders.</p>
                  </div>
                  <button
                    type="button"
                    onClick={() => setAcceptsCod((v) => !v)}
                    className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${acceptsCod ? "bg-brand-primary" : "bg-gray-300"}`}
                  >
                    <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${acceptsCod ? "translate-x-6" : "translate-x-1"}`} />
                  </button>
                </div>

                {acceptsCod && (
                  <div className="flex flex-col gap-3">
                    <div>
                      <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">COD Limit (₹)</label>
                      <input
                        type="number"
                        min={0}
                        value={codLimit}
                        onChange={(e) => setCodLimit(e.target.value === "" ? "" : Number(e.target.value))}
                        placeholder="0 = no limit"
                        className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent"
                      />
                      <p className="text-xs text-gray-400 mt-1">Orders above this amount must prepay. Leave empty for no limit.</p>
                    </div>
                    <div className="flex items-start gap-2 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2.5">
                      <AlertTriangle size={14} className="text-amber-500 mt-0.5 shrink-0" />
                      <p className="text-xs text-amber-700">COD orders are not prepaid — confirm only after verifying customer intent.</p>
                    </div>
                  </div>
                )}
              </div>

              {/* Section C — Bank Transfer */}
              <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-6 flex flex-col gap-4">
                <div className="flex items-center justify-between">
                  <div>
                    <h3 className="text-sm font-semibold text-gray-800">Bank Transfer</h3>
                    <p className="text-xs text-gray-400 mt-0.5">For B2B / wholesale customers. Optional.</p>
                  </div>
                  <button
                    type="button"
                    onClick={() => setAcceptsBankTransfer((v) => !v)}
                    className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${acceptsBankTransfer ? "bg-brand-primary" : "bg-gray-300"}`}
                  >
                    <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${acceptsBankTransfer ? "translate-x-6" : "translate-x-1"}`} />
                  </button>
                </div>

                {acceptsBankTransfer && (
                  <div className="flex flex-col gap-3">
                    <div>
                      <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Account Name</label>
                      <input
                        type="text"
                        value={bankAccountName}
                        onChange={(e) => setBankAccountName(e.target.value)}
                        placeholder="Riya Sarees Pvt Ltd"
                        className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Account Number</label>
                      <input
                        type="text"
                        value={bankAccountNumber}
                        onChange={(e) => setBankAccountNumber(e.target.value)}
                        placeholder="1234567890"
                        className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">IFSC Code</label>
                      <input
                        type="text"
                        value={bankIfsc}
                        onChange={(e) => setBankIfsc(e.target.value.toUpperCase())}
                        placeholder="SBIN0001234"
                        className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm font-mono text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent"
                      />
                    </div>
                  </div>
                )}
              </div>

              {/* Section D — Razorpay */}
              <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-6 flex flex-col gap-4">
                <div>
                  <h3 className="text-sm font-semibold text-gray-800">Razorpay QR Codes</h3>
                  <p className="text-xs text-gray-400 mt-0.5">Generates a scannable QR for exact order amounts. Requires a Razorpay account.</p>
                </div>
                <div>
                  <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Key ID</label>
                  <input
                    type="text"
                    value={razorpayKeyId}
                    onChange={(e) => setRazorpayKeyId(e.target.value)}
                    placeholder="rzp_live_..."
                    className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm font-mono text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Key Secret</label>
                  <input
                    type="password"
                    value={razorpayKeySecret}
                    onChange={(e) => setRazorpayKeySecret(e.target.value)}
                    placeholder="Enter new secret to update"
                    className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm font-mono text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent"
                  />
                  <p className="text-xs text-gray-400 mt-1">Stored securely. Leave blank to keep existing secret.</p>
                </div>
                <a
                  href="https://dashboard.razorpay.com/app/keys"
                  target="_blank"
                  rel="noreferrer"
                  className="flex items-center gap-1 text-xs text-brand-primaryDark hover:underline"
                >
                  Get Razorpay API keys <ExternalLink size={11} />
                </a>
              </div>

              {/* Section E — Payment Instructions */}
              <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-6 flex flex-col gap-3">
                <div>
                  <h3 className="text-sm font-semibold text-gray-800">Additional Payment Instructions</h3>
                  <p className="text-xs text-gray-400 mt-0.5">Shown after payment details in WhatsApp messages.</p>
                </div>
                <textarea
                  value={paymentInstructions}
                  onChange={(e) => setPaymentInstructions(e.target.value.slice(0, 200))}
                  rows={3}
                  placeholder="e.g. Please mention your order number in the payment note"
                  className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent resize-none"
                />
                <p className="text-xs text-gray-400">{paymentInstructions.length}/200 characters</p>
              </div>

              {/* Save */}
              <div className="flex items-center gap-3 px-1">
                <button
                  type="submit"
                  disabled={paymentSaving}
                  className="bg-brand-primaryDark text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-brand-primary/90 disabled:opacity-50 transition-colors"
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

          {/* Team tab — Owner only */}
          {activeTab === "team" && isOwner && (
            <div className="flex flex-col gap-4 max-w-3xl">
              <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-6">
                <h2 className="text-base font-semibold text-gray-900 mb-1">Invite a team member</h2>
                <p className="text-xs text-gray-400 mb-4">
                  They'll get a one-time link to set their own password — no email is sent automatically, so copy and share it yourself.
                </p>
                <form onSubmit={handleInvite} className="flex flex-col sm:flex-row gap-3 items-start sm:items-end">
                  <div className="flex-1 w-full">
                    <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Email</label>
                    <input
                      type="email"
                      value={inviteEmail}
                      onChange={(e) => setInviteEmail(e.target.value)}
                      required
                      placeholder="teammate@business.com"
                      className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent"
                    />
                  </div>
                  <div className="w-full sm:w-40">
                    <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Role</label>
                    <select
                      value={inviteRole}
                      onChange={(e) => setInviteRole(e.target.value as "manager" | "staff")}
                      className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent"
                    >
                      <option value="manager">Manager</option>
                      <option value="staff">Staff</option>
                    </select>
                  </div>
                  <button
                    type="submit"
                    disabled={inviting}
                    className="bg-brand-primaryDark text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-brand-primary/90 disabled:opacity-50 transition-colors shrink-0"
                  >
                    {inviting ? "Creating…" : "Create invite"}
                  </button>
                </form>

                {inviteLink && (
                  <div className="mt-4 bg-brand-primary/5 border border-brand-primary/20 rounded-lg px-4 py-3 flex items-center gap-2">
                    <code className="text-xs text-brand-primaryDark flex-1 truncate">{inviteLink}</code>
                    <button
                      type="button"
                      onClick={() => navigator.clipboard.writeText(inviteLink)}
                      title="Copy link"
                      className="p-1.5 text-brand-primaryDark hover:bg-brand-primary/10 rounded-lg transition-colors shrink-0"
                    >
                      <Copy size={14} />
                    </button>
                  </div>
                )}
                {teamError && (
                  <p className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg mt-4">{teamError}</p>
                )}
              </div>

              <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-6">
                <h2 className="text-base font-semibold text-gray-900 mb-4">Team members</h2>
                {teamLoading ? (
                  <p className="text-sm text-gray-400">Loading…</p>
                ) : team.length === 0 ? (
                  <p className="text-sm text-gray-400">No team members yet — invite one above.</p>
                ) : (
                  <div className="flex flex-col gap-5">
                    {team.map((member) => (
                      <div key={member.id} className="border border-gray-100 rounded-xl p-4">
                        <div className="flex items-center justify-between gap-3 flex-wrap mb-3">
                          <div>
                            <div className="text-sm font-medium text-gray-900">{member.email}</div>
                            <div className="text-xs text-gray-400 capitalize">
                              {member.role}
                              {member.is_pending && <span className="ml-2 text-amber-600">· invite pending</span>}
                              {!member.is_active && <span className="ml-2 text-red-500">· removed</span>}
                            </div>
                          </div>
                          {member.role !== "owner" && (
                            <div className="flex items-center gap-2 shrink-0">
                              <select
                                value={member.role}
                                onChange={(e) => handleApplyPreset(member, e.target.value as "manager" | "staff")}
                                disabled={savingMemberId === member.id}
                                className="border border-gray-200 rounded-lg px-2.5 py-1.5 text-xs text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand-primary"
                              >
                                <option value="manager">Manager preset</option>
                                <option value="staff">Staff preset</option>
                              </select>
                              <button
                                type="button"
                                onClick={() => handleToggleActive(member)}
                                disabled={savingMemberId === member.id}
                                className={`text-xs font-medium px-3 py-1.5 rounded-lg border transition-colors ${
                                  member.is_active
                                    ? "border-red-200 text-red-600 hover:bg-red-50"
                                    : "border-emerald-200 text-emerald-600 hover:bg-emerald-50"
                                }`}
                              >
                                {member.is_active ? "Remove" : "Reactivate"}
                              </button>
                            </div>
                          )}
                        </div>

                        {member.role !== "owner" && (
                          <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-2">
                            {PERMISSION_OPTIONS.map((perm) => (
                              <label key={perm.key} className="flex items-center gap-2 text-xs text-gray-600 cursor-pointer">
                                <input
                                  type="checkbox"
                                  checked={member.permissions.includes(perm.key)}
                                  onChange={() => handleTogglePermission(member, perm.key)}
                                  disabled={savingMemberId === member.id}
                                  className="w-3.5 h-3.5 accent-brand-primary shrink-0"
                                />
                                {perm.label}
                              </label>
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
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
                        ? "border-brand-primary/40 bg-brand-primary/5"
                        : "border-gray-200 hover:border-gray-300 hover:bg-gray-50"
                    }`}
                  >
                    <input
                      type="radio"
                      name="dashboard_language"
                      value={opt.code}
                      checked={currentLang === opt.code}
                      onChange={() => handleLangChange(opt.code)}
                      className="w-4 h-4 accent-brand-primary"
                    />
                    <div>
                      <div className={`text-sm font-medium ${currentLang === opt.code ? "text-brand-primaryDark" : "text-gray-700"}`}>{opt.label}</div>
                      <div className="text-xs text-gray-400">{opt.native}</div>
                    </div>
                    {currentLang === opt.code && <CheckCircle2 size={16} className="text-brand-primaryDark ml-auto" />}
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
                <Award size={18} className="text-brand-primaryDark" />
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
