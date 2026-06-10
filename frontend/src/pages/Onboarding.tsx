import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  addProduct,
  updateOnboardingStep,
  updateProfile,
} from "../api/client";
import { api } from "../api/client";
import { useAuth } from "../context/AuthContext";
import { SandboxUI } from "./Sandbox";
import {
  CheckCircle2,
  ChevronRight,
  Copy,
  ExternalLink,
  Loader2,
  SkipForward,
} from "lucide-react";

// ── constants ─────────────────────────────────────────────────────────────────

const BUSINESS_TYPES = [
  { value: "textile", label: "Textile / Fashion" },
  { value: "clinic", label: "Clinic / Healthcare" },
  { value: "realestate", label: "Real Estate" },
  { value: "restaurant", label: "Restaurant / Food" },
  { value: "coaching", label: "Coaching / Education" },
  { value: "other", label: "Other" },
];

const TONE_PRESETS = [
  {
    key: "professional",
    label: "Professional & Formal",
    desc: "Precise, courteous, business-like",
    suffix:
      "Maintain a polished and professional tone at all times. Use clear, precise language. Address customers respectfully.",
  },
  {
    key: "friendly",
    label: "Friendly & Casual",
    desc: "Warm, approachable, conversational",
    suffix:
      "Be warm, friendly, and conversational. Use everyday language. Add a personal touch to every reply.",
  },
  {
    key: "sales",
    label: "Sales-Focused",
    desc: "Persuasive, conversion-driven",
    suffix:
      "Always guide the customer toward a purchase. Highlight product benefits, create urgency, and make it easy to place an order.",
  },
  {
    key: "helpful",
    label: "Helpful & Informative",
    desc: "Detailed, educational, supportive",
    suffix:
      "Focus on providing complete, accurate information. Answer every question thoroughly. Help customers make informed decisions.",
  },
];

const TONE_EXAMPLES: Record<string, { customer: string; agent: string }> = {
  professional: {
    customer: "Do you have blue sarees in stock?",
    agent:
      "Good day! Yes, we currently have several blue saree options available. May I assist you with size, fabric, or price preferences?",
  },
  friendly: {
    customer: "Do you have blue sarees in stock?",
    agent:
      "Hey! Great choice — blue sarees look amazing 😍 Yes, we have a few in stock! Want me to share the options?",
  },
  sales: {
    customer: "Do you have blue sarees in stock?",
    agent:
      "Yes! Our blue sarees are selling fast 🔥 Only a few pieces left. Which one catches your eye — shall I reserve one for you?",
  },
  helpful: {
    customer: "Do you have blue sarees in stock?",
    agent:
      "Yes! We have blue sarees in cotton, silk, and georgette. Each fabric has different care requirements. Which material suits your occasion best?",
  },
};

const BASE_PROMPTS: Record<string, string> = {
  textile:
    "You are a friendly and knowledgeable sales assistant for {name}, a textile/fashion business. {desc} Help customers explore our collection, check pricing and stock, and place orders.",
  clinic:
    "You are a helpful patient-support assistant for {name}. {desc} Help patients book appointments, answer questions about services, and share general health information. Always remind patients that specific medical advice must come from a qualified doctor.",
  realestate:
    "You are a professional real-estate assistant for {name}. {desc} Help clients find the right property, understand their requirements, share listing details, and arrange site visits.",
  restaurant:
    "You are an enthusiastic assistant for {name}, a food and restaurant business. {desc} Help customers browse the menu, place orders, check delivery times, and answer questions about ingredients and specials.",
  coaching:
    "You are a supportive assistant for {name}, an education and coaching center. {desc} Help students understand course offerings, fees, schedules, and enrollment process.",
  other:
    "You are a helpful AI assistant for {name}. {desc} Answer customer questions professionally, help them find products or services, and guide them toward a purchase or booking.",
};

interface ProductRow {
  name: string;
  price: string;
  stock: string;
  category: string;
}

const EMPTY_ROW: ProductRow = { name: "", price: "", stock: "", category: "" };

function buildPrompt(
  type: string,
  name: string,
  desc: string,
  toneKey: string
): string {
  const base = BASE_PROMPTS[type] ?? BASE_PROMPTS["other"];
  const toneSuffix =
    TONE_PRESETS.find((t) => t.key === toneKey)?.suffix ?? "";
  return (
    base
      .replace("{name}", name || "our business")
      .replace("{desc}", desc ? desc.trim().replace(/\.$/, "") + "." : "") +
    " " +
    toneSuffix
  );
}

// ── step bar ─────────────────────────────────────────────────────────────────

const STEP_LABELS = [
  "Profile",
  "Products",
  "Agent",
  "WhatsApp",
  "Test",
  "Done",
];

function StepBar({ current }: { current: number }) {
  return (
    <div className="px-8 pt-8 pb-6">
      <div className="flex items-center gap-1">
        {STEP_LABELS.map((label, i) => {
          const n = i + 1;
          const done = n < current;
          const active = n === current;
          return (
            <div key={n} className="flex-1 flex flex-col items-center gap-1">
              <div
                className={`w-full h-1.5 rounded-full transition-colors ${
                  done || active ? "bg-indigo-600" : "bg-gray-200"
                }`}
              />
              <span
                className={`text-[10px] font-medium ${
                  active
                    ? "text-indigo-600"
                    : done
                    ? "text-indigo-400"
                    : "text-gray-400"
                }`}
              >
                {label}
              </span>
            </div>
          );
        })}
      </div>
      <p className="text-xs text-gray-400 mt-2">
        Step {Math.min(current, 6)} of 6
      </p>
    </div>
  );
}

// ── skip link ────────────────────────────────────────────────────────────────

function SkipLink({
  onClick,
  label = "Skip this step",
}: {
  onClick: () => void;
  label?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 mx-auto transition-colors"
    >
      <SkipForward size={12} />
      {label}
    </button>
  );
}

// ── main component ────────────────────────────────────────────────────────────

export default function Onboarding() {
  const { client, refreshProfile } = useAuth();
  const navigate = useNavigate();

  // Initialise wizard step from server state — resume if partially complete
  const [step, setStep] = useState<number>(() => {
    const s = client?.onboarding_step ?? 0;
    return s >= 6 ? 6 : s + 1;
  });

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  // Step 1
  const [businessName, setBusinessName] = useState(client?.business_name ?? "");
  const [businessType, setBusinessType] = useState(
    client?.business_type ?? "other"
  );
  const [businessDesc, setBusinessDesc] = useState(
    client?.business_description ?? ""
  );
  const [city, setCity] = useState("");
  const [phone, setPhone] = useState(client?.phone ?? "");

  // Step 2
  const [products, setProducts] = useState<ProductRow[]>([{ ...EMPTY_ROW }]);
  const fileRef = useRef<HTMLInputElement>(null);

  // Step 3
  const [toneKey, setToneKey] = useState("friendly");
  const [systemPrompt, setSystemPrompt] = useState("");

  // Step 4 — WhatsApp
  const [waNumber, setWaNumber] = useState(client?.whatsapp_number ?? "");
  const [waPhoneId, setWaPhoneId] = useState("");
  const [waToken, setWaToken] = useState("");

  // Step 6 — summary
  const [productCount, setProductCount] = useState(0);
  const [catUrl, setCatUrl] = useState("");
  const [copiedCat, setCopiedCat] = useState(false);

  // Redirect if already completed
  useEffect(() => {
    if (client?.onboarding_completed) {
      navigate("/dashboard", { replace: true });
    }
  }, [client, navigate]);

  // Sync prompt whenever type / tone / name / desc change
  useEffect(() => {
    setSystemPrompt(buildPrompt(businessType, businessName, businessDesc, toneKey));
  }, [businessType, businessName, businessDesc, toneKey]);

  // ── helpers ──────────────────────────────────────────────────────────────

  async function advance(nextStep: number) {
    await updateOnboardingStep(nextStep);
    await refreshProfile();
    setStep(nextStep + 1);
    setError("");
  }

  function addRow() {
    if (products.length < 5) setProducts((p) => [...p, { ...EMPTY_ROW }]);
  }

  function removeRow(i: number) {
    setProducts((p) => p.filter((_, idx) => idx !== i));
  }

  function updateRow(i: number, field: keyof ProductRow, value: string) {
    setProducts((p) =>
      p.map((row, idx) => (idx === i ? { ...row, [field]: value } : row))
    );
  }

  function handleCsvUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      const text = ev.target?.result as string;
      const rows = text
        .trim()
        .split("\n")
        .slice(1) // skip header
        .map((line) => {
          const cols = line.split(",").map((c) => c.trim().replace(/^"|"$/g, ""));
          return {
            name: cols[0] ?? "",
            price: cols[1] ?? "",
            stock: cols[2] ?? "",
            category: cols[3] ?? "",
          };
        })
        .filter((r) => r.name)
        .slice(0, 5);
      if (rows.length) setProducts(rows);
    };
    reader.readAsText(file);
  }

  // ── step handlers ─────────────────────────────────────────────────────────

  async function handleStep1() {
    if (!businessName.trim() || !businessDesc.trim()) return;
    setSubmitting(true);
    try {
      await updateProfile({
        business_name: businessName,
        phone: phone || undefined,
      } as Parameters<typeof updateProfile>[0]);
      // store type + desc via setup-agent light call
      await api.patch("/auth/me", {
        business_name: businessName,
        phone: phone || undefined,
      });
      // persist type & description via dedicated field on client (re-use PATCH /auth/me
      // which now accepts business_type + business_description when added below, but those
      // aren't in UpdateMeRequest yet — so we store them via the onboarding endpoint)
      // We'll call setup-agent with minimum payload to persist type + desc
      await api.post("/onboarding/setup-agent", {
        business_name: businessName,
        business_type: businessType === "restaurant" || businessType === "coaching"
          ? "other"
          : businessType,
        business_description: businessDesc,
      });
      await advance(1);
    } catch {
      setError("Failed to save profile. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleStep2(skip = false) {
    setSubmitting(true);
    try {
      if (!skip) {
        const valid = products.filter((r) => r.name.trim() && r.price.trim());
        const count = valid.length;
        if (count > 0) {
          await Promise.allSettled(
            valid.map((r) =>
              addProduct({
                name: r.name.trim(),
                price: parseFloat(r.price),
                stock: r.stock ? parseInt(r.stock, 10) : undefined,
                category: r.category || undefined,
              })
            )
          );
          setProductCount(count);
        }
      }
      await advance(2);
    } catch {
      setError("Failed to save products. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleStep3() {
    setSubmitting(true);
    try {
      await updateProfile({ gemini_system_prompt: systemPrompt } as Parameters<typeof updateProfile>[0]);
      await advance(3);
    } catch {
      setError("Failed to save agent config. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleStep4(skip = false) {
    setSubmitting(true);
    try {
      if (!skip && (waPhoneId.trim() || waToken.trim())) {
        await updateProfile({
          whatsapp_phone_number_id: waPhoneId || undefined,
          whatsapp_access_token: waToken || undefined,
        } as Parameters<typeof updateProfile>[0]);
      }
      await advance(4);
    } catch {
      setError("Failed to save WhatsApp credentials. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleStep5() {
    // Sandbox auto-advances step 5 on first message; just move wizard forward
    await advance(5);
  }

  async function handleStep6() {
    setSubmitting(true);
    try {
      await updateOnboardingStep(6);
      await refreshProfile();
      // Build catalogue URL if slug exists
      if (client?.catalogue_slug) {
        setCatUrl(`${window.location.origin}/shop/${client.catalogue_slug}`);
      }
      setStep(7); // "done" screen
    } catch {
      // Still navigate even if step update fails
      navigate("/dashboard");
    } finally {
      setSubmitting(false);
    }
  }

  function copyCat() {
    navigator.clipboard.writeText(catUrl);
    setCopiedCat(true);
    setTimeout(() => setCopiedCat(false), 2000);
  }

  // ── render ────────────────────────────────────────────────────────────────

  // Count products added (approximate from DB via profile)
  const addedProducts = productCount;
  const waConnected = !!(waPhoneId && waToken);

  return (
    <div className="min-h-screen bg-gradient-to-br from-indigo-50 via-white to-purple-50 flex items-center justify-center px-4 py-12">
      <div className="bg-white rounded-2xl shadow-lg border border-gray-100 w-full max-w-2xl">
        {step <= 6 && <StepBar current={step} />}

        <div className="px-8 pb-10">

          {/* ── STEP 1: Business Profile ──────────────────────────────────── */}
          {step === 1 && (
            <div className="flex flex-col gap-5">
              <div>
                <h2 className="text-2xl font-bold text-gray-900">Tell us about your business</h2>
                <p className="text-sm text-gray-400 mt-1">Help your AI agent represent you perfectly.</p>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div className="col-span-2">
                  <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide mb-1.5">
                    Business Name *
                  </label>
                  <input
                    type="text"
                    value={businessName}
                    onChange={(e) => setBusinessName(e.target.value)}
                    className="w-full border border-gray-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    placeholder="Raj's Textiles"
                  />
                </div>

                <div className="col-span-2 sm:col-span-1">
                  <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide mb-1.5">
                    Business Type *
                  </label>
                  <select
                    value={businessType}
                    onChange={(e) => setBusinessType(e.target.value)}
                    className="w-full border border-gray-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 bg-white"
                  >
                    {BUSINESS_TYPES.map((t) => (
                      <option key={t.value} value={t.value}>
                        {t.label}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="col-span-2 sm:col-span-1">
                  <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide mb-1.5">
                    City / Location
                  </label>
                  <input
                    type="text"
                    value={city}
                    onChange={(e) => setCity(e.target.value)}
                    className="w-full border border-gray-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    placeholder="Surat"
                  />
                </div>

                <div className="col-span-2">
                  <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide mb-1.5">
                    Phone Number
                  </label>
                  <input
                    type="tel"
                    value={phone}
                    onChange={(e) => setPhone(e.target.value)}
                    className="w-full border border-gray-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    placeholder="+91 98765 43210"
                  />
                </div>

                <div className="col-span-2">
                  <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide mb-1.5">
                    Business Description *
                  </label>
                  <textarea
                    value={businessDesc}
                    onChange={(e) => setBusinessDesc(e.target.value)}
                    rows={3}
                    className="w-full border border-gray-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 resize-none"
                    placeholder="Tell us about your business so AI can represent you better — what you sell, who your customers are, what makes you special..."
                  />
                </div>
              </div>

              {error && <p className="text-sm text-red-600">{error}</p>}

              <button
                onClick={handleStep1}
                disabled={submitting || !businessName.trim() || !businessDesc.trim()}
                className="flex items-center justify-center gap-2 bg-indigo-600 text-white rounded-xl py-3 text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors"
              >
                {submitting ? <Loader2 size={16} className="animate-spin" /> : null}
                Continue <ChevronRight size={16} />
              </button>
            </div>
          )}

          {/* ── STEP 2: Products ─────────────────────────────────────────── */}
          {step === 2 && (
            <div className="flex flex-col gap-5">
              <div>
                <h2 className="text-2xl font-bold text-gray-900">Add your products</h2>
                <p className="text-sm text-gray-400 mt-1">
                  Your AI agent will use these to answer customer questions. You can add more later.
                </p>
              </div>

              {/* Column headers */}
              <div className="grid grid-cols-[1fr_80px_70px_100px_24px] gap-2 text-xs font-medium text-gray-400 uppercase tracking-wide px-1">
                <span>Name</span>
                <span>Price ₹</span>
                <span>Stock</span>
                <span>Category</span>
                <span />
              </div>

              <div className="flex flex-col gap-2">
                {products.map((row, i) => (
                  <div key={i} className="grid grid-cols-[1fr_80px_70px_100px_24px] gap-2 items-center">
                    <input
                      type="text"
                      value={row.name}
                      onChange={(e) => updateRow(i, "name", e.target.value)}
                      placeholder="Banarasi Silk Saree"
                      className="border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    />
                    <input
                      type="number"
                      value={row.price}
                      onChange={(e) => updateRow(i, "price", e.target.value)}
                      placeholder="2450"
                      className="border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    />
                    <input
                      type="number"
                      value={row.stock}
                      onChange={(e) => updateRow(i, "stock", e.target.value)}
                      placeholder="50"
                      className="border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    />
                    <input
                      type="text"
                      value={row.category}
                      onChange={(e) => updateRow(i, "category", e.target.value)}
                      placeholder="Sarees"
                      className="border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    />
                    {products.length > 1 ? (
                      <button
                        onClick={() => removeRow(i)}
                        className="text-gray-300 hover:text-red-400 text-lg leading-none"
                      >
                        ×
                      </button>
                    ) : (
                      <span />
                    )}
                  </div>
                ))}
              </div>

              <div className="flex items-center gap-4">
                {products.length < 5 && (
                  <button
                    onClick={addRow}
                    className="text-sm text-indigo-600 hover:underline"
                  >
                    + Add another
                  </button>
                )}
                <button
                  onClick={() => fileRef.current?.click()}
                  className="text-sm text-gray-500 hover:text-gray-700 flex items-center gap-1"
                >
                  <ExternalLink size={13} /> Import CSV
                </button>
                <input
                  ref={fileRef}
                  type="file"
                  accept=".csv"
                  className="hidden"
                  onChange={handleCsvUpload}
                />
                <span className="text-xs text-gray-300 ml-auto">
                  CSV: name, price, stock, category
                </span>
              </div>

              {error && <p className="text-sm text-red-600">{error}</p>}

              <div className="flex gap-3">
                <button
                  onClick={() => setStep(1)}
                  className="flex-1 border border-gray-200 text-gray-700 rounded-xl py-2.5 text-sm font-medium hover:bg-gray-50 transition-colors"
                >
                  ← Back
                </button>
                <button
                  onClick={() => handleStep2(false)}
                  disabled={submitting}
                  className="flex-1 flex items-center justify-center gap-2 bg-indigo-600 text-white rounded-xl py-2.5 text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors"
                >
                  {submitting ? <Loader2 size={16} className="animate-spin" /> : null}
                  Continue <ChevronRight size={16} />
                </button>
              </div>
              <SkipLink onClick={() => handleStep2(true)} />
            </div>
          )}

          {/* ── STEP 3: Agent Config ─────────────────────────────────────── */}
          {step === 3 && (
            <div className="flex flex-col gap-5">
              <div>
                <h2 className="text-2xl font-bold text-gray-900">Set up your AI agent</h2>
                <p className="text-sm text-gray-400 mt-1">Choose a tone and fine-tune the personality.</p>
              </div>

              {/* Tone presets */}
              <div>
                <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">Tone</p>
                <div className="grid grid-cols-2 gap-2">
                  {TONE_PRESETS.map((t) => (
                    <label
                      key={t.key}
                      className={`flex items-start gap-3 border rounded-xl p-3 cursor-pointer transition-colors ${
                        toneKey === t.key
                          ? "border-indigo-500 bg-indigo-50"
                          : "border-gray-200 hover:border-gray-300"
                      }`}
                    >
                      <input
                        type="radio"
                        name="tone"
                        value={t.key}
                        checked={toneKey === t.key}
                        onChange={() => setToneKey(t.key)}
                        className="mt-0.5 accent-indigo-600"
                      />
                      <div>
                        <p className="text-sm font-medium text-gray-800">{t.label}</p>
                        <p className="text-xs text-gray-400">{t.desc}</p>
                      </div>
                    </label>
                  ))}
                </div>
              </div>

              {/* Example conversation */}
              <div className="bg-[#ECE5DD] rounded-xl p-4">
                <p className="text-xs font-medium text-gray-500 mb-3">Preview</p>
                <div className="flex flex-col gap-2">
                  <div className="flex justify-start">
                    <div className="bg-white rounded-2xl rounded-tl-sm px-3 py-2 text-sm shadow-sm max-w-[80%]">
                      {TONE_EXAMPLES[toneKey]?.customer}
                    </div>
                  </div>
                  <div className="flex justify-end">
                    <div className="bg-[#DCF8C6] rounded-2xl rounded-tr-sm px-3 py-2 text-sm shadow-sm max-w-[80%]">
                      {TONE_EXAMPLES[toneKey]?.agent}
                    </div>
                  </div>
                </div>
              </div>

              {/* Editable prompt */}
              <div>
                <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-1.5">
                  System Prompt (editable)
                </p>
                <textarea
                  value={systemPrompt}
                  onChange={(e) => setSystemPrompt(e.target.value)}
                  rows={5}
                  className="w-full border border-gray-200 rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 resize-none"
                />
              </div>

              {error && <p className="text-sm text-red-600">{error}</p>}

              <div className="flex gap-3">
                <button
                  onClick={() => setStep(2)}
                  className="flex-1 border border-gray-200 text-gray-700 rounded-xl py-2.5 text-sm font-medium hover:bg-gray-50 transition-colors"
                >
                  ← Back
                </button>
                <button
                  onClick={handleStep3}
                  disabled={submitting}
                  className="flex-1 flex items-center justify-center gap-2 bg-indigo-600 text-white rounded-xl py-2.5 text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors"
                >
                  {submitting ? <Loader2 size={16} className="animate-spin" /> : null}
                  Continue <ChevronRight size={16} />
                </button>
              </div>
            </div>
          )}

          {/* ── STEP 4: Connect WhatsApp ─────────────────────────────────── */}
          {step === 4 && (
            <div className="flex flex-col gap-5">
              <div>
                <h2 className="text-2xl font-bold text-gray-900">Connect WhatsApp</h2>
                <p className="text-sm text-gray-400 mt-1">
                  Your AI agent will reply to messages on this number. You'll need a Meta Business account.
                </p>
              </div>

              <div className="flex flex-col gap-4">
                <div>
                  <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide mb-1.5">
                    WhatsApp Business Number
                  </label>
                  <input
                    type="tel"
                    value={waNumber}
                    onChange={(e) => setWaNumber(e.target.value)}
                    className="w-full border border-gray-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    placeholder="+91 98765 43210"
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide mb-1.5">
                    Phone Number ID (from Meta Developer Console)
                  </label>
                  <input
                    type="text"
                    value={waPhoneId}
                    onChange={(e) => setWaPhoneId(e.target.value)}
                    className="w-full border border-gray-200 rounded-xl px-4 py-2.5 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    placeholder="123456789012345"
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide mb-1.5">
                    Access Token
                  </label>
                  <input
                    type="password"
                    value={waToken}
                    onChange={(e) => setWaToken(e.target.value)}
                    className="w-full border border-gray-200 rounded-xl px-4 py-2.5 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    placeholder="EAAGm..."
                  />
                </div>
              </div>

              <div className="bg-blue-50 border border-blue-100 rounded-xl px-4 py-3 text-xs text-blue-700">
                You can always connect WhatsApp later from <strong>Settings → Channels</strong>. It takes about 5 minutes with a Meta Business account.
              </div>

              {error && <p className="text-sm text-red-600">{error}</p>}

              <div className="flex gap-3">
                <button
                  onClick={() => setStep(3)}
                  className="flex-1 border border-gray-200 text-gray-700 rounded-xl py-2.5 text-sm font-medium hover:bg-gray-50 transition-colors"
                >
                  ← Back
                </button>
                <button
                  onClick={() => handleStep4(false)}
                  disabled={submitting}
                  className="flex-1 flex items-center justify-center gap-2 bg-indigo-600 text-white rounded-xl py-2.5 text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors"
                >
                  {submitting ? <Loader2 size={16} className="animate-spin" /> : null}
                  Connect WhatsApp <ChevronRight size={16} />
                </button>
              </div>
              <SkipLink onClick={() => handleStep4(true)} label="Skip for now — I'll connect later" />
            </div>
          )}

          {/* ── STEP 5: Test Agent ────────────────────────────────────────── */}
          {step === 5 && (
            <div className="flex flex-col gap-5">
              <div>
                <h2 className="text-2xl font-bold text-gray-900">Test your agent</h2>
                <p className="text-sm text-gray-400 mt-1">
                  Send a message to see how your agent replies. Messages here won't reach real customers.
                </p>
              </div>

              <SandboxUI />

              {error && <p className="text-sm text-red-600">{error}</p>}

              <div className="flex gap-3">
                <button
                  onClick={() => setStep(4)}
                  className="flex-1 border border-gray-200 text-gray-700 rounded-xl py-2.5 text-sm font-medium hover:bg-gray-50 transition-colors"
                >
                  ← Back to configure
                </button>
                <button
                  onClick={handleStep5}
                  disabled={submitting}
                  className="flex-1 flex items-center justify-center gap-2 bg-indigo-600 text-white rounded-xl py-2.5 text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors"
                >
                  {submitting ? <Loader2 size={16} className="animate-spin" /> : null}
                  This looks good! <ChevronRight size={16} />
                </button>
              </div>
            </div>
          )}

          {/* ── STEP 6: Ready summary ─────────────────────────────────────── */}
          {step === 6 && (
            <div className="flex flex-col gap-6">
              <div className="text-center">
                <div className="text-5xl mb-3">🎉</div>
                <h2 className="text-2xl font-bold text-gray-900">Your AI agent is ready!</h2>
                <p className="text-sm text-gray-400 mt-1">
                  Review your setup and go live whenever you're ready.
                </p>
              </div>

              {/* Summary checklist */}
              <div className="bg-gray-50 rounded-xl border border-gray-100 p-5 flex flex-col gap-3">
                {[
                  { label: "Business profile complete", done: true },
                  {
                    label: `${addedProducts > 0 ? addedProducts : "0"} product${addedProducts !== 1 ? "s" : ""} added`,
                    done: addedProducts > 0,
                    skipped: addedProducts === 0,
                  },
                  { label: "Agent configured", done: true },
                  {
                    label: "WhatsApp connected",
                    done: waConnected,
                    skipped: !waConnected,
                  },
                  { label: "Agent tested", done: true },
                ].map((item, i) => (
                  <div key={i} className="flex items-center gap-3 text-sm">
                    {item.done ? (
                      <CheckCircle2 size={18} className="text-green-500 shrink-0" />
                    ) : (
                      <span className="text-lg leading-none shrink-0">⏭️</span>
                    )}
                    <span
                      className={
                        item.done ? "text-gray-800" : "text-gray-400"
                      }
                    >
                      {item.label}
                      {item.skipped && !item.done && (
                        <span className="ml-1 text-xs text-gray-400">(skipped)</span>
                      )}
                    </span>
                  </div>
                ))}
              </div>

              {/* Catalogue URL */}
              {catUrl && (
                <div>
                  <p className="text-xs text-gray-500 mb-1.5">Your public catalogue link:</p>
                  <div className="flex gap-2">
                    <code className="flex-1 bg-gray-50 border border-gray-200 rounded-lg px-3 py-2 text-xs font-mono truncate text-gray-700">
                      {catUrl}
                    </code>
                    <button
                      onClick={copyCat}
                      className="flex items-center gap-1.5 border border-gray-200 rounded-lg px-3 py-2 text-xs text-gray-600 hover:bg-gray-50 transition-colors"
                    >
                      <Copy size={13} />
                      {copiedCat ? "Copied!" : "Copy"}
                    </button>
                  </div>
                </div>
              )}

              <div className="flex gap-3">
                <button
                  onClick={handleStep6}
                  disabled={submitting}
                  className="flex-1 flex items-center justify-center gap-2 bg-indigo-600 text-white rounded-xl py-3 text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors"
                >
                  {submitting ? <Loader2 size={16} className="animate-spin" /> : null}
                  Go to Dashboard
                </button>
                {catUrl && (
                  <button
                    onClick={copyCat}
                    className="flex items-center gap-2 border border-gray-200 text-gray-700 rounded-xl px-4 py-3 text-sm font-medium hover:bg-gray-50 transition-colors"
                  >
                    <Copy size={15} /> Share Catalogue
                  </button>
                )}
              </div>
            </div>
          )}

          {/* ── STEP 7: Navigate after done ──────────────────────────────── */}
          {step === 7 && (
            <div className="flex flex-col items-center gap-4 py-8">
              <Loader2 size={32} className="animate-spin text-indigo-600" />
              <p className="text-sm text-gray-500">Taking you to your dashboard...</p>
            </div>
          )}

        </div>
      </div>
    </div>
  );
}
