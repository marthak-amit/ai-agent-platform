import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import Layout from "../components/Layout";
import { getProfile, testWhatsApp, updateChannelCredentials } from "../api/client";
import { useAuth } from "../context/AuthContext";
import { CheckCircle2, X, Package, Copy, ExternalLink, Share2, FileDown } from "lucide-react";
import QRCode from "qrcode";

const APP_BASE_URL = (import.meta.env.VITE_APP_URL as string) || "http://localhost:5173";

interface Profile {
  whatsapp_phone_number_id?: string;
  whatsapp_access_token?: string;
  instagram_access_token?: string;
  instagram_account_id?: string;
  api_key?: string;
}

function CopyButton({ text }: { text: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  function copy() {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    });
  }
  return (
    <button
      onClick={copy}
      className="ml-2 px-2.5 py-1 text-xs rounded-lg bg-white/10 hover:bg-white/20 text-gray-400 hover:text-gray-600 transition-colors border border-gray-200"
    >
      {copied ? t("channels.copied") : t("channels.copy")}
    </button>
  );
}

function StatusBadge({ state }: { state: "connected" | "disconnected" | "error" | "ready" }) {
  const map = {
    connected: "bg-green-100 text-green-700",
    disconnected: "bg-gray-100 text-gray-500",
    error: "bg-red-100 text-red-600",
    ready: "bg-blue-100 text-blue-700",
  };
  const label = { connected: "Connected", disconnected: "Not connected", error: "Error", ready: "Ready to embed" };
  return (
    <span className={`text-xs font-medium px-2.5 py-1 rounded-full flex items-center gap-1.5 ${map[state]}`}>
      {state === "connected" && <CheckCircle2 size={11} />}
      {label[state]}
    </span>
  );
}

function Step({ n, text }: { n: number; text: string }) {
  return (
    <div className="flex items-start gap-3">
      <span className="shrink-0 w-6 h-6 rounded-full bg-indigo-100 text-indigo-600 text-xs font-bold flex items-center justify-center mt-0.5">
        {n}
      </span>
      <span className="text-sm text-gray-600 leading-relaxed">{text}</span>
    </div>
  );
}

function FieldInput({
  label, value, onChange, type = "text", placeholder,
}: { label: string; value: string; onChange: (v: string) => void; type?: string; placeholder?: string }) {
  return (
    <div>
      <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">{label}</label>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition-all"
      />
    </div>
  );
}

function WidgetPreviewModal({ apiKey, onClose }: { apiKey: string; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md p-6 m-4">
        <div className="flex justify-between items-center mb-5">
          <h3 className="font-bold text-gray-900">Widget preview</h3>
          <button onClick={onClose} className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-gray-100 text-gray-400 transition-colors">
            <X size={18} />
          </button>
        </div>
        <div className="bg-gray-50 rounded-xl border border-gray-200 p-4 min-h-48 relative">
          <p className="text-xs text-gray-400 text-center pt-6">Your chat widget will appear as a button in the bottom-right corner of your website.</p>
          <div className="absolute bottom-4 right-4 w-12 h-12 rounded-full bg-indigo-600 flex items-center justify-center shadow-lg cursor-pointer">
            <svg className="w-6 h-6 text-white" fill="currentColor" viewBox="0 0 24 24">
              <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z" />
            </svg>
          </div>
        </div>
        <p className="text-xs text-gray-400 mt-3">API key: <span className="font-mono">{apiKey || "not generated yet"}</span></p>
        <button onClick={onClose} className="mt-4 w-full py-2.5 rounded-lg bg-indigo-600 text-white text-sm font-semibold hover:bg-indigo-700 transition-colors">
          Close
        </button>
      </div>
    </div>
  );
}

function CatalogueCard({ slug }: { slug: string }) {
  const [copied, setCopied] = useState(false);
  const [qrDataUrl, setQrDataUrl] = useState<string | null>(null);
  const catalogueUrl = `${APP_BASE_URL}/shop/${slug}`;

  function copyLink() {
    navigator.clipboard.writeText(catalogueUrl).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  function openCatalogue() {
    window.open(catalogueUrl, "_blank");
  }

  function shareOnWhatsApp() {
    const text = encodeURIComponent(`Browse our catalogue: ${catalogueUrl}`);
    window.open(`https://wa.me/?text=${text}`, "_blank");
  }

  async function downloadQrCode() {
    const dataUrl = qrDataUrl ?? (await QRCode.toDataURL(catalogueUrl, { width: 512, margin: 2 }));
    if (!qrDataUrl) setQrDataUrl(dataUrl);
    const a = document.createElement("a");
    a.href = dataUrl;
    a.download = `${slug}-qr.png`;
    a.click();
  }

  return (
    <div className="bg-white rounded-2xl border-2 border-green-200 shadow-sm overflow-hidden mb-6">
      <div className="flex flex-col items-center pt-8 pb-4 px-6 text-center border-b border-gray-50">
        <div className="w-12 h-12 flex items-center justify-center text-green-500">
          <Package size={48} />
        </div>
        <h2 className="font-bold text-gray-900 mt-4 text-lg">Digital Product Catalogue</h2>
        <p className="text-xs text-gray-400 mt-1 mb-3">Share this link with your customers</p>
        <span className="text-xs font-medium px-2.5 py-1 rounded-full flex items-center gap-1.5 bg-green-100 text-green-700">
          ✅ Live
        </span>
      </div>

      <div className="p-6 space-y-4">
        <div className="flex items-center bg-gray-50 border border-gray-200 rounded-lg px-3 py-2.5 font-mono text-xs text-gray-600">
          <span className="flex-1 truncate">{catalogueUrl}</span>
          <button
            onClick={copyLink}
            className="ml-2 px-2.5 py-1 text-xs rounded-lg bg-white/10 hover:bg-white/20 text-gray-400 hover:text-gray-600 transition-colors border border-gray-200 flex items-center gap-1.5"
          >
            <Copy size={13} />
            {copied ? "Copied!" : "Copy"}
          </button>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <button
            onClick={openCatalogue}
            className="flex items-center justify-center gap-1.5 py-2.5 rounded-lg border border-gray-200 text-gray-700 text-sm font-semibold hover:bg-gray-50 transition-colors"
          >
            <ExternalLink size={14} />
            Open Catalogue
          </button>
          <button
            onClick={shareOnWhatsApp}
            className="flex items-center justify-center gap-1.5 py-2.5 rounded-lg border border-green-200 text-green-700 text-sm font-semibold hover:bg-green-50 transition-colors"
          >
            <Share2 size={14} />
            Share on WhatsApp
          </button>
          <button
            onClick={downloadQrCode}
            className="flex items-center justify-center gap-1.5 py-2.5 rounded-lg border border-gray-200 text-gray-700 text-sm font-semibold hover:bg-gray-50 transition-colors"
          >
            <FileDown size={14} />
            Download QR Code
          </button>
        </div>

        <div className="bg-gray-50 rounded-lg px-4 py-3 text-xs text-gray-500 leading-relaxed">
          Share this link on:
          <ul className="list-disc list-inside mt-1 space-y-0.5">
            <li>Instagram bio (replace linktree)</li>
            <li>WhatsApp status</li>
            <li>Print as QR code for your shop</li>
          </ul>
        </div>
      </div>
    </div>
  );
}

const BASE_URL = (import.meta.env.VITE_API_URL ?? "https://yourplatform.com/api").replace(/\/api$/, "");

export default function Channels() {
  const { t } = useTranslation();
  const { client } = useAuth();
  const [profile, setProfile] = useState<Profile>({});

  const [waPhoneId, setWaPhoneId] = useState("");
  const [waToken, setWaToken] = useState("");
  const [waSaving, setWaSaving] = useState(false);
  const [waSaved, setWaSaved] = useState(false);
  const [waTesting, setWaTesting] = useState(false);
  const [waTestResult, setWaTestResult] = useState<{ ok: boolean; msg: string } | null>(null);

  const [igToken, setIgToken] = useState("");
  const [igAccountId, setIgAccountId] = useState("");
  const [igSaving, setIgSaving] = useState(false);
  const [igSaved, setIgSaved] = useState(false);

  const [showWidgetPreview, setShowWidgetPreview] = useState(false);

  const verifyToken = useRef(
    Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2)
  ).current;

  useEffect(() => {
    getProfile().then((p: Profile) => {
      setProfile(p);
      setWaPhoneId(p.whatsapp_phone_number_id ?? "");
      setWaToken(p.whatsapp_access_token ?? "");
      setIgToken(p.instagram_access_token ?? "");
      setIgAccountId(p.instagram_account_id ?? "");
    });
  }, []);

  const waConnected = !!(profile.whatsapp_phone_number_id && profile.whatsapp_access_token);
  const igConnected = !!(profile.instagram_access_token && profile.instagram_account_id);
  const embedCode = `<script\n  src="${BASE_URL}/widget.js"\n  data-api-key="${profile.api_key ?? "vp_xxxxx"}">\n</script>`;

  async function saveWhatsApp() {
    setWaSaving(true);
    setWaTestResult(null);
    try {
      await updateChannelCredentials({ whatsapp_phone_number_id: waPhoneId, whatsapp_access_token: waToken });
      setProfile((p) => ({ ...p, whatsapp_phone_number_id: waPhoneId, whatsapp_access_token: waToken }));
      setWaSaved(true);
      setTimeout(() => setWaSaved(false), 2000);
    } catch { /* retry */ }
    finally { setWaSaving(false); }
  }

  async function runWhatsAppTest() {
    setWaTesting(true);
    setWaTestResult(null);
    try {
      const res = await testWhatsApp();
      setWaTestResult({ ok: true, msg: res.message });
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Test failed. Check your credentials.";
      setWaTestResult({ ok: false, msg: detail });
    } finally {
      setWaTesting(false);
    }
  }

  async function saveInstagram() {
    setIgSaving(true);
    try {
      await updateChannelCredentials({ instagram_access_token: igToken, instagram_account_id: igAccountId });
      setProfile((p) => ({ ...p, instagram_access_token: igToken, instagram_account_id: igAccountId }));
      setIgSaved(true);
      setTimeout(() => setIgSaved(false), 2000);
    } catch { /* silent */ }
    finally { setIgSaving(false); }
  }

  const channelCards = [
    {
      key: "whatsapp",
      title: "WhatsApp Business",
      subtitle: "Receive and reply to customer messages automatically",
      connected: waConnected,
      icon: (
        <div className="w-16 h-16 rounded-2xl bg-green-500 flex items-center justify-center shadow-sm">
          <svg className="w-8 h-8 text-white" fill="currentColor" viewBox="0 0 24 24">
            <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347z" />
            <path d="M11.999 2C6.477 2 2 6.477 2 12c0 1.89.525 3.66 1.438 5.168L2 22l4.932-1.41A9.956 9.956 0 0012 22c5.523 0 10-4.477 10-10S17.523 2 12 2zm0 18c-1.66 0-3.208-.46-4.532-1.257l-.324-.192-3.367.964.983-3.288-.21-.337A7.958 7.958 0 014 12c0-4.418 3.582-8 8-8s8 3.582 8 8-3.582 8-8 8z" />
          </svg>
        </div>
      ),
      content: (
        <div className="space-y-4">
          <div className="space-y-2">
            <Step n={1} text="Get your WhatsApp Business number" />
            <Step n={2} text="Enter your Meta Phone Number ID below" />
            <Step n={3} text="Enter your WhatsApp Access Token below" />
            <Step n={4} text="Set the webhook URL in Meta Developer Dashboard → Webhooks" />
          </div>

          <div>
            <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Webhook URL</label>
            <div className="flex items-center bg-gray-50 border border-gray-200 rounded-lg px-3 py-2.5 font-mono text-xs text-gray-600">
              <span className="flex-1 truncate">{BASE_URL}/webhook</span>
              <CopyButton text={`${BASE_URL}/webhook`} />
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
              Webhook Verify Token <span className="font-normal normal-case text-gray-400">(paste in Meta Dashboard)</span>
            </label>
            <div className="flex items-center bg-gray-50 border border-gray-200 rounded-lg px-3 py-2.5 font-mono text-xs text-gray-600">
              <span className="flex-1 truncate">{verifyToken}</span>
              <CopyButton text={verifyToken} />
            </div>
          </div>

          <FieldInput label="Phone Number ID" value={waPhoneId} onChange={setWaPhoneId} placeholder="e.g. 123456789012345" />
          <FieldInput label="Access Token" value={waToken} onChange={setWaToken} type="password" placeholder="EAAxxxxxxxx…" />

          {waTestResult && (
            <div className={`rounded-lg px-4 py-2.5 text-sm ${waTestResult.ok ? "bg-green-50 text-green-700 border border-green-200" : "bg-red-50 text-red-600 border border-red-200"}`}>
              {waTestResult.msg}
            </div>
          )}

          <div className="flex gap-3">
            <button onClick={saveWhatsApp} disabled={waSaving || !waPhoneId || !waToken}
              className="flex-1 py-2.5 rounded-lg bg-indigo-600 text-white text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors">
              {waSaving ? t("channels.saving") : waSaved ? "✓ Saved" : t("channels.save")}
            </button>
            <button onClick={runWhatsAppTest} disabled={waTesting || !waConnected}
              className="flex-1 py-2.5 rounded-lg border border-indigo-200 text-indigo-700 text-sm font-semibold hover:bg-indigo-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
              {waTesting ? t("channels.testing") : t("channels.test")}
            </button>
          </div>
        </div>
      ),
    },
    {
      key: "instagram",
      title: "Instagram",
      subtitle: "Auto-reply to DMs via Instagram Business",
      connected: igConnected,
      icon: (
        <div className="w-16 h-16 rounded-2xl flex items-center justify-center shadow-sm" style={{ background: "radial-gradient(circle at 30% 107%, #fdf497 0%, #fdf497 5%, #fd5949 45%, #d6249f 60%, #285AEB 90%)" }}>
          <svg className="w-8 h-8 text-white" fill="currentColor" viewBox="0 0 24 24">
            <path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zM12 0C8.741 0 8.333.014 7.053.072 2.695.272.273 2.69.073 7.052.014 8.333 0 8.741 0 12c0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98C8.333 23.986 8.741 24 12 24c3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98C15.668.014 15.259 0 12 0zm0 5.838a6.162 6.162 0 100 12.324 6.162 6.162 0 000-12.324zM12 16a4 4 0 110-8 4 4 0 010 8zm6.406-11.845a1.44 1.44 0 100 2.881 1.44 1.44 0 000-2.881z" />
          </svg>
        </div>
      ),
      content: (
        <div className="space-y-4">
          <div className="space-y-2">
            <Step n={1} text="Connect Instagram Business account to a Facebook Page" />
            <Step n={2} text="Open Meta Developer Console → your app → Instagram" />
            <Step n={3} text='Generate a long-lived access token with "instagram_basic" scope' />
            <Step n={4} text="Enter your token and Instagram Account ID below" />
          </div>

          <div>
            <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Webhook URL</label>
            <div className="flex items-center bg-gray-50 border border-gray-200 rounded-lg px-3 py-2.5 font-mono text-xs text-gray-600">
              <span className="flex-1 truncate">{BASE_URL}/instagram</span>
              <CopyButton text={`${BASE_URL}/instagram`} />
            </div>
          </div>

          <FieldInput label="Instagram Account ID" value={igAccountId} onChange={setIgAccountId} placeholder="e.g. 17841400008460056" />
          <FieldInput label="Access Token" value={igToken} onChange={setIgToken} type="password" placeholder="EAAxxxxxxxx…" />

          <button onClick={saveInstagram} disabled={igSaving || !igToken || !igAccountId}
            className="w-full py-2.5 rounded-lg bg-indigo-600 text-white text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors">
            {igSaving ? t("channels.saving") : igSaved ? "✓ Saved" : t("channels.save")}
          </button>
        </div>
      ),
    },
    {
      key: "website",
      title: "Website Widget",
      subtitle: "Embed a live chat widget on any website",
      connected: false,
      isReady: true,
      icon: (
        <div className="w-16 h-16 rounded-2xl bg-indigo-600 flex items-center justify-center shadow-sm">
          <svg className="w-8 h-8 text-white" fill="currentColor" viewBox="0 0 24 24">
            <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z" />
          </svg>
        </div>
      ),
      content: (
        <div className="space-y-4">
          <p className="text-sm text-gray-600">
            Paste the code snippet below before the <code className="bg-gray-100 px-1 rounded text-xs">{"</body>"}</code> tag on your website.
          </p>
          <div>
            <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Embed code</label>
            <div className="relative bg-gray-900 rounded-xl p-4 font-mono text-xs text-green-400 whitespace-pre leading-relaxed overflow-x-auto">
              {embedCode}
              <div className="absolute top-3 right-3">
                <CopyButton text={embedCode} />
              </div>
            </div>
          </div>
          <button onClick={() => setShowWidgetPreview(true)}
            className="w-full py-2.5 rounded-lg border border-indigo-200 text-indigo-700 text-sm font-semibold hover:bg-indigo-50 transition-colors">
            Preview Widget
          </button>
        </div>
      ),
    },
  ] as const;

  return (
    <Layout>
      {showWidgetPreview && (
        <WidgetPreviewModal apiKey={profile.api_key ?? ""} onClose={() => setShowWidgetPreview(false)} />
      )}

      <div className="max-w-2xl mx-auto">
        <div className="mb-8">
          <h1 className="text-2xl font-bold text-gray-900">{t("channels.title")}</h1>
          <p className="text-gray-400 text-sm mt-1">{t("channels.subtitle")}</p>
        </div>

        {client?.catalogue_slug && <CatalogueCard slug={client.catalogue_slug} />}

        <div className="space-y-6">
          {channelCards.map((card) => (
            <div key={card.key} className="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
              {/* Card header */}
              <div className="flex flex-col items-center pt-8 pb-4 px-6 text-center border-b border-gray-50">
                {card.icon}
                <h2 className="font-bold text-gray-900 mt-4 text-lg">{card.title}</h2>
                <p className="text-xs text-gray-400 mt-1 mb-3">{card.subtitle}</p>
                <StatusBadge state={"isReady" in card && card.isReady ? "ready" : card.connected ? "connected" : "disconnected"} />
              </div>

              {/* Content */}
              <div className="p-6">{card.content}</div>
            </div>
          ))}
        </div>
      </div>
    </Layout>
  );
}
