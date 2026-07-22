import { useEffect, useState } from "react";
import { X, Sparkles, Check, RotateCcw, AlertTriangle, Loader2 } from "lucide-react";
import {
  approveVariantPhoto,
  enhanceVariantPhoto,
  getStyleReferences,
  rejectVariantPhoto,
} from "../api/client";
import type { Product, ProductVariant, StyleReference } from "../types";

function variantLabel(v: ProductVariant): string {
  return [v.color, v.size].filter(Boolean).join(" / ") || `Variant ${v.id}`;
}

const STATUS_BADGE: Record<string, { label: string; className: string }> = {
  done: { label: "Ready to approve", className: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  flagged_color_mismatch: { label: "Color mismatch — flagged", className: "bg-amber-50 text-amber-700 border-amber-200" },
  failed: { label: "Generation failed", className: "bg-red-50 text-red-700 border-red-200" },
  pending: { label: "Generating…", className: "bg-gray-100 text-gray-500 border-gray-200" },
};

const STYLE_TYPE_ORDER: StyleReference["style_type"][] = ["dummy", "human_model", "hanging"];

function stylesByType(styles: StyleReference[]): Record<string, StyleReference[]> {
  return styles.reduce<Record<string, StyleReference[]>>((acc, s) => {
    (acc[s.style_type] ??= []).push(s);
    return acc;
  }, {});
}

export default function PhotoEnhanceModal({
  product,
  onClose,
  onVariantUpdated,
  resolveImage,
}: {
  product: Product;
  onClose: () => void;
  onVariantUpdated: (variant: ProductVariant) => void;
  resolveImage: (url: string) => string;
}) {
  const enhanceableVariants = product.variants.filter((v) => v.is_active && v.image_url);

  const [variants, setVariants] = useState<ProductVariant[]>(enhanceableVariants);
  const [activeId, setActiveId] = useState<number | null>(enhanceableVariants[0]?.id ?? null);
  const [styles, setStyles] = useState<StyleReference[]>([]);
  const [loadingStyles, setLoadingStyles] = useState(true);
  const [selectedStyleId, setSelectedStyleId] = useState<number | null>(null);
  const [generating, setGenerating] = useState(false);
  const [busyAction, setBusyAction] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      if (!product.category) { setLoadingStyles(false); return; }
      setLoadingStyles(true);
      try {
        const data: StyleReference[] = await getStyleReferences(product.category);
        if (!cancelled) {
          setStyles(data);
          setSelectedStyleId((prev) => prev ?? data[0]?.id ?? null);
        }
      } catch {
        if (!cancelled) setError("Could not load style options for this category.");
      } finally {
        if (!cancelled) setLoadingStyles(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, [product.category]);

  const active = variants.find((v) => v.id === activeId) ?? null;

  function updateVariant(patch: ProductVariant) {
    // The enhance/approve/reject endpoints return a minimal photo-state DTO
    // (id, image_url, enhanced_*) — merge onto the existing variant rather
    // than replacing it, so color/size/stock/etc. survive the update.
    setVariants((prev) => prev.map((v) => (v.id === patch.id ? { ...v, ...patch } : v)));
    const merged = { ...(variants.find((v) => v.id === patch.id) ?? patch), ...patch };
    onVariantUpdated(merged);
  }

  async function handleGenerate() {
    if (!active || selectedStyleId == null) return;
    setGenerating(true);
    setError(null);
    try {
      const updated: ProductVariant = await enhanceVariantPhoto(product.id, active.id, selectedStyleId);
      updateVariant(updated);
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail || "Photo generation failed. Please try again.");
    } finally {
      setGenerating(false);
    }
  }

  async function handleApprove() {
    if (!active) return;
    setBusyAction("approve");
    setError(null);
    try {
      const updated: ProductVariant = await approveVariantPhoto(product.id, active.id);
      updateVariant(updated);
    } catch {
      setError("Could not approve this photo.");
    } finally {
      setBusyAction(null);
    }
  }

  async function handleReject() {
    if (!active) return;
    setBusyAction("reject");
    setError(null);
    try {
      const updated: ProductVariant = await rejectVariantPhoto(product.id, active.id);
      updateVariant(updated);
    } catch {
      setError("Could not reject this photo.");
    } finally {
      setBusyAction(null);
    }
  }

  const badge = active?.enhanced_status ? STATUS_BADGE[active.enhanced_status] : null;

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />
      <div className="relative bg-white rounded-2xl shadow-xl w-full max-w-3xl z-10 flex flex-col max-h-[90vh]">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100 shrink-0">
          <div>
            <h2 className="text-lg font-bold text-gray-900 flex items-center gap-2">
              <Sparkles size={18} className="text-violet-500" /> Enhance Photos
            </h2>
            <p className="text-xs text-gray-500 mt-0.5 truncate">{product.name}</p>
          </div>
          <button onClick={onClose} className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-gray-100 text-gray-400 transition-colors">
            <X size={18} />
          </button>
        </div>

        {enhanceableVariants.length === 0 ? (
          <div className="p-8 text-center text-sm text-gray-500">
            No variants with an uploaded photo yet. Add a photo to a variant first (Edit product → Step 6 — Variant Photos).
          </div>
        ) : (
          <div className="flex flex-1 min-h-0">
            {/* Variant list */}
            <div className="w-48 border-r border-gray-100 overflow-y-auto shrink-0 p-2 flex flex-col gap-1">
              {variants.map((v) => (
                <button
                  key={v.id}
                  onClick={() => setActiveId(v.id)}
                  className={`text-left px-2.5 py-2 rounded-xl text-xs transition-colors flex items-center gap-2 ${
                    v.id === activeId ? "bg-violet-50 text-violet-700" : "hover:bg-gray-50 text-gray-600"
                  }`}
                >
                  <div className="w-8 h-8 rounded-lg overflow-hidden shrink-0 bg-gray-100">
                    {v.image_url && <img src={resolveImage(v.image_url)} alt="" className="w-full h-full object-cover" />}
                  </div>
                  <span className="truncate flex-1">{variantLabel(v)}</span>
                  {v.enhanced_status === "done" && v.enhanced_approved && <Check size={12} className="text-emerald-500 shrink-0" />}
                </button>
              ))}
            </div>

            {/* Detail panel */}
            <div className="flex-1 overflow-y-auto p-6 flex flex-col gap-4">
              {error && (
                <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2 flex items-center gap-2">
                  <AlertTriangle size={13} className="shrink-0" /> {error}
                </div>
              )}

              {active && (
                <>
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <div className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">Raw photo</div>
                      <div className="aspect-square rounded-xl overflow-hidden bg-gray-100">
                        {active.image_url && <img src={resolveImage(active.image_url)} alt="Raw" className="w-full h-full object-cover" />}
                      </div>
                    </div>
                    <div>
                      <div className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">Enhanced photo</div>
                      <div className="aspect-square rounded-xl overflow-hidden bg-gray-100 flex items-center justify-center">
                        {generating || active.enhanced_status === "pending" ? (
                          <Loader2 size={22} className="text-gray-400 animate-spin" />
                        ) : active.enhanced_image_url ? (
                          <img src={resolveImage(active.enhanced_image_url)} alt="Enhanced" className="w-full h-full object-cover" />
                        ) : (
                          <span className="text-xs text-gray-400 px-4 text-center">Pick a style and generate</span>
                        )}
                      </div>
                    </div>
                  </div>

                  {badge && (
                    <span className={`self-start text-[11px] font-medium px-2.5 py-1 rounded-full border ${badge.className}`}>
                      {badge.label}
                    </span>
                  )}

                  <div>
                    <div className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">Style</div>
                    {loadingStyles ? (
                      <div className="text-xs text-gray-400">Loading styles…</div>
                    ) : styles.length === 0 ? (
                      <div className="text-xs text-gray-400">No styles available for category "{product.category}" yet.</div>
                    ) : (
                      <div className="flex flex-col gap-3">
                        {STYLE_TYPE_ORDER.filter((type) => stylesByType(styles)[type]?.length).map((type) => (
                          <div key={type}>
                            <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-400 mb-1.5">
                              {type.replace("_", " ")}
                            </div>
                            <div className="flex gap-2 flex-wrap">
                              {stylesByType(styles)[type].map((s) => (
                                <button
                                  key={s.id}
                                  onClick={() => setSelectedStyleId(s.id)}
                                  title={s.description}
                                  className={`flex flex-col items-center gap-1 p-1.5 rounded-xl border transition-all ${
                                    selectedStyleId === s.id ? "border-violet-400 ring-2 ring-violet-100" : "border-gray-200 hover:border-gray-300"
                                  }`}
                                >
                                  <div className="w-16 h-16 rounded-lg overflow-hidden bg-gray-100">
                                    <img src={resolveImage(s.reference_image_url)} alt={s.style_type} className="w-full h-full object-cover" />
                                  </div>
                                </button>
                              ))}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>

                  <div className="flex items-center gap-2 mt-auto pt-2">
                    <button
                      onClick={handleGenerate}
                      disabled={generating || selectedStyleId == null}
                      className="flex-1 flex items-center justify-center gap-2 bg-brand-primaryDark text-white py-2.5 rounded-xl text-sm font-semibold hover:bg-brand-primary/90 disabled:opacity-50 transition-colors"
                    >
                      {generating ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
                      {active.enhanced_image_url ? "Retry with this style" : "Generate"}
                    </button>

                    {active.enhanced_status === "done" && !active.enhanced_approved && (
                      <button
                        onClick={handleApprove}
                        disabled={busyAction !== null}
                        className="flex items-center justify-center gap-1.5 px-4 py-2.5 rounded-xl text-sm font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200 hover:bg-emerald-100 disabled:opacity-50 transition-colors"
                      >
                        <Check size={14} /> Approve
                      </button>
                    )}

                    {active.enhanced_image_url && (
                      <button
                        onClick={handleReject}
                        disabled={busyAction !== null}
                        className="flex items-center justify-center gap-1.5 px-4 py-2.5 rounded-xl text-sm font-semibold bg-gray-100 text-gray-600 border border-gray-200 hover:bg-gray-200 disabled:opacity-50 transition-colors"
                      >
                        <RotateCcw size={14} /> Reject
                      </button>
                    )}
                  </div>

                  {active.enhanced_approved && (
                    <p className="text-[11px] text-emerald-600 flex items-center gap-1">
                      <Check size={11} /> This photo is live on your storefront/catalog.
                    </p>
                  )}
                </>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
