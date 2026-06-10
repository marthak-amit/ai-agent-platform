import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  addProduct,
  adjustStock,
  adjustVariantStocks,
  deleteProduct,
  getStockHistory,
  listProducts,
  updateProduct,
  uploadProductImage,
} from "../api/client";
import Layout from "../components/Layout";
import type { Product, ProductVariant, StockLog } from "../types";
import { Search, Plus, X, Package, AlertTriangle } from "lucide-react";
import { ColorDots, SizePills } from "../utils/variants";

// ── Constants ─────────────────────────────────────────────────────────────────

const CATEGORIES = ["Saree", "Lehenga", "Kurti", "Dupatta", "Jewellery", "Other"];
const STOCK_REASONS = [
  { value: "sold", label: "Sold" },
  { value: "restocked", label: "Restocked" },
  { value: "correction", label: "Correction" },
  { value: "damaged", label: "Damaged" },
];

const COLOR_PRESETS = [
  { name: "Red",    hex: "#EF4444", border: false },
  { name: "Pink",   hex: "#EC4899", border: false },
  { name: "Blue",   hex: "#3B82F6", border: false },
  { name: "Navy",   hex: "#1E3A5F", border: false },
  { name: "Green",  hex: "#10B981", border: false },
  { name: "Gold",   hex: "#F59E0B", border: false },
  { name: "Black",  hex: "#111827", border: false },
  { name: "White",  hex: "#F9FAFB", border: true  },
  { name: "Purple", hex: "#8B5CF6", border: false },
  { name: "Orange", hex: "#F97316", border: false },
  { name: "Maroon", hex: "#7F1D1D", border: false },
  { name: "Yellow", hex: "#EAB308", border: false },
];

const SIZE_PRESETS_CLOTHING = ["XS", "S", "M", "L", "XL", "XXL", "3XL"];
const SIZE_PRESETS_NUMBERS  = ["28", "30", "32", "34", "36", "38", "40"];

const API_BASE = import.meta.env.VITE_API_URL
  ? import.meta.env.VITE_API_URL.replace("/api", "")
  : "";

// ── Helpers ───────────────────────────────────────────────────────────────────

const toTitleCase = (str: string) =>
  str.replace(/\b\w/g, (c) => c.toUpperCase());

function variantKey(color?: string, size?: string): string {
  return `${color || ""}||${size || ""}`;
}

function getVariantKeys(vs: VariantState): string[] {
  if (vs.useColors && vs.useSizes && vs.colors.length > 0 && vs.sizes.length > 0)
    return vs.colors.flatMap((c) => vs.sizes.map((s) => variantKey(c, s)));
  if (vs.useColors && vs.colors.length > 0)
    return vs.colors.map((c) => variantKey(c));
  if (vs.useSizes && vs.sizes.length > 0)
    return vs.sizes.map((s) => variantKey(undefined, s));
  return [];
}

function totalVariantStock(vs: VariantState): number {
  return getVariantKeys(vs).reduce(
    (sum, k) => sum + (parseInt(vs.stockMatrix[k] || "0", 10) || 0),
    0,
  );
}

// ── Types ─────────────────────────────────────────────────────────────────────

interface ProductForm {
  name: string;
  category: string;
  description: string;
  price: string;
  stock: string;
  low_stock_alert: string;
  is_active: boolean;
  image_url: string;
}

interface VariantState {
  hasVariants: boolean;
  useColors: boolean;
  useSizes: boolean;
  colors: string[];
  sizes: string[];
  stockMatrix: Record<string, string>;
  useDiffPrice: boolean;
  priceMatrix: Record<string, string>;
  fillAll: string;
  customColorName: string;
  customColorHex: string;
  customSize: string;
  editVariantIds: Record<string, number>;
}

const EMPTY_FORM: ProductForm = {
  name: "",
  category: "",
  description: "",
  price: "",
  stock: "",
  low_stock_alert: "5",
  is_active: true,
  image_url: "",
};

const EMPTY_VARIANT_STATE: VariantState = {
  hasVariants: false,
  useColors: false,
  useSizes: false,
  colors: [],
  sizes: [],
  stockMatrix: {},
  useDiffPrice: false,
  priceMatrix: {},
  fillAll: "",
  customColorName: "",
  customColorHex: "#000000",
  customSize: "",
  editVariantIds: {},
};

// ── SKU pill ──────────────────────────────────────────────────────────────────

function SkuPill({ sku }: { sku: string }) {
  const [copied, setCopied] = useState(false);
  function copy(e: React.MouseEvent) {
    e.stopPropagation();
    navigator.clipboard.writeText(sku).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }
  return (
    <button
      onClick={copy}
      title="Click to copy SKU"
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-gray-100 hover:bg-gray-200 transition-colors"
    >
      <span className="font-mono text-[10px] font-semibold text-gray-600 tracking-wide">{sku}</span>
      <span className="text-[10px] text-gray-400">{copied ? "✓" : "⎘"}</span>
    </button>
  );
}

// ── Stock badge ───────────────────────────────────────────────────────────────

function StockBadge({ product }: { product: Product }) {
  if (product.stock === null)
    return <span className="text-xs text-gray-400">Untracked</span>;
  if (product.stock === 0)
    return <span className="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium bg-red-100 text-red-700">Out of stock</span>;
  if (product.stock <= product.low_stock_alert)
    return <span className="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium bg-amber-100 text-amber-700">{product.stock} left — low stock</span>;
  return <span className="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium bg-green-100 text-green-700">{product.stock} in stock</span>;
}

// ── Image drop-zone ───────────────────────────────────────────────────────────

function ImageDropZone({ preview, onFile }: { preview: string; onFile: (f: File) => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files[0];
    if (file && file.type.startsWith("image/")) onFile(file);
  }

  return (
    <div
      onClick={() => inputRef.current?.click()}
      onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
      className={`relative w-full rounded-xl border-2 border-dashed flex items-center justify-center cursor-pointer transition-all overflow-hidden
        ${dragging ? "border-indigo-500 bg-indigo-50" : "border-gray-200 bg-gray-50 hover:bg-gray-100"}`}
      style={{ height: 160 }}
    >
      {preview ? (
        <div className="w-full h-full relative group">
          <img src={preview} alt="preview" className="h-full w-full object-cover" />
          <div className="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center">
            <span className="text-white text-xs font-medium">Change image</span>
          </div>
        </div>
      ) : (
        <div className="text-center text-sm text-gray-400 select-none px-4">
          <div className="text-3xl mb-2">📷</div>
          <div>Drop image or <span className="text-indigo-600 font-medium">click to upload</span></div>
          <div className="text-xs text-gray-300 mt-1">JPEG, PNG, WebP · max 5 MB</div>
        </div>
      )}
      <input ref={inputRef} type="file" accept="image/*" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; if (f) onFile(f); }} />
    </div>
  );
}

// ── Variant builder ───────────────────────────────────────────────────────────

function VariantBuilder({ vs, setVs }: {
  vs: VariantState;
  setVs: React.Dispatch<React.SetStateAction<VariantState>>;
}) {
  function toggleColor(name: string) {
    setVs((prev) => {
      const colors = prev.colors.includes(name)
        ? prev.colors.filter((c) => c !== name)
        : [...prev.colors, name];
      return { ...prev, colors };
    });
  }

  function removeColor(name: string) {
    setVs((prev) => ({ ...prev, colors: prev.colors.filter((c) => c !== name) }));
  }

  function addCustomColor() {
    const name = vs.customColorName.trim();
    if (!name || vs.colors.includes(name)) return;
    setVs((prev) => ({
      ...prev,
      colors: [...prev.colors, name],
      customColorName: "",
      customColorHex: "#000000",
    }));
  }

  function toggleSize(s: string) {
    setVs((prev) => {
      const sizes = prev.sizes.includes(s)
        ? prev.sizes.filter((x) => x !== s)
        : [...prev.sizes, s];
      return { ...prev, sizes };
    });
  }

  function removeSize(s: string) {
    setVs((prev) => ({ ...prev, sizes: prev.sizes.filter((x) => x !== s) }));
  }

  function addCustomSize() {
    const s = vs.customSize.trim();
    if (!s || vs.sizes.includes(s)) return;
    setVs((prev) => ({ ...prev, sizes: [...prev.sizes, s], customSize: "" }));
  }

  function setStock(key: string, val: string) {
    setVs((prev) => ({ ...prev, stockMatrix: { ...prev.stockMatrix, [key]: val } }));
  }

  function setPrice(key: string, val: string) {
    setVs((prev) => ({ ...prev, priceMatrix: { ...prev.priceMatrix, [key]: val } }));
  }

  function applyFillAll() {
    if (!vs.fillAll) return;
    const keys = getVariantKeys(vs);
    const updates: Record<string, string> = {};
    keys.forEach((k) => {
      if (!vs.stockMatrix[k]) updates[k] = vs.fillAll;
    });
    setVs((prev) => ({ ...prev, stockMatrix: { ...prev.stockMatrix, ...updates } }));
  }

  const keys = getVariantKeys(vs);
  const total = totalVariantStock(vs);
  const variantCount = keys.length;

  const colorHexMap: Record<string, string> = {};
  COLOR_PRESETS.forEach((cp) => { colorHexMap[cp.name] = cp.hex; });

  const isBoth = vs.useColors && vs.useSizes && vs.colors.length > 0 && vs.sizes.length > 0;
  const isColorsOnly = vs.useColors && !vs.useSizes && vs.colors.length > 0;
  const isSizesOnly = !vs.useColors && vs.useSizes && vs.sizes.length > 0;

  return (
    <div className="border border-gray-200 rounded-xl overflow-hidden shrink-0">
      {/* Toggle */}
      <div className="flex items-center justify-between px-4 py-3 bg-gray-50">
        <div>
          <div className="text-sm font-medium text-gray-700">This product has variants</div>
          <div className="text-xs text-gray-400">(colors, sizes)</div>
        </div>
        <button
          type="button"
          onClick={() => setVs((prev) => ({ ...prev, hasVariants: !prev.hasVariants }))}
          className={`relative w-11 h-6 rounded-full transition-colors ${vs.hasVariants ? "bg-indigo-600" : "bg-gray-300"}`}
        >
          <span className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform ${vs.hasVariants ? "translate-x-5" : ""}`} />
        </button>
      </div>

      {vs.hasVariants && (
        <div className="p-4 flex flex-col gap-5">

          {/* Step 1: What varies */}
          <div>
            <div className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">Step 1 — What varies?</div>
            <div className="flex gap-4">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={vs.useColors}
                  onChange={(e) => setVs((prev) => ({ ...prev, useColors: e.target.checked }))}
                  className="rounded accent-indigo-600"
                />
                <span className="text-sm text-gray-700">Colors / Shades</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={vs.useSizes}
                  onChange={(e) => setVs((prev) => ({ ...prev, useSizes: e.target.checked }))}
                  className="rounded accent-indigo-600"
                />
                <span className="text-sm text-gray-700">Sizes</span>
              </label>
            </div>
          </div>

          {/* Step 2: Colors */}
          {vs.useColors && (
            <div>
              <div className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">Step 2 — Available Colors</div>

              {/* Color presets */}
              <div className="flex flex-wrap gap-2 mb-3">
                {COLOR_PRESETS.map((cp) => (
                  <button
                    key={cp.name}
                    type="button"
                    title={cp.name}
                    onClick={() => toggleColor(cp.name)}
                    className={`w-7 h-7 rounded-full transition-all relative ${cp.border ? "border border-gray-300" : ""} ${vs.colors.includes(cp.name) ? "ring-2 ring-offset-1 ring-indigo-500 scale-110" : "hover:scale-110"}`}
                    style={{ backgroundColor: cp.hex }}
                  />
                ))}
              </div>

              {/* Custom color input */}
              <div className="flex items-center gap-2 mb-3">
                <input
                  type="text"
                  placeholder="Color name"
                  value={vs.customColorName}
                  onChange={(e) => setVs((prev) => ({ ...prev, customColorName: e.target.value }))}
                  onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addCustomColor(); }}}
                  className="flex-1 border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
                <input
                  type="color"
                  value={vs.customColorHex}
                  onChange={(e) => setVs((prev) => ({ ...prev, customColorHex: e.target.value }))}
                  className="w-8 h-8 rounded border border-gray-200 cursor-pointer"
                />
                <button
                  type="button"
                  onClick={addCustomColor}
                  className="px-3 py-1.5 bg-indigo-50 text-indigo-600 rounded-lg text-sm font-medium hover:bg-indigo-100"
                >
                  + Add
                </button>
              </div>

              {/* Selected color pills */}
              {vs.colors.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {vs.colors.map((c) => (
                    <span
                      key={c}
                      className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium bg-gray-100 text-gray-700"
                    >
                      <span
                        className="w-3 h-3 rounded-full shrink-0 border border-black/10"
                        style={{ backgroundColor: colorHexMap[c] || "#888" }}
                      />
                      {c}
                      <button type="button" onClick={() => removeColor(c)} className="text-gray-400 hover:text-gray-700 ml-0.5">×</button>
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Step 3: Sizes */}
          {vs.useSizes && (
            <div>
              <div className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">Step 3 — Available Sizes</div>

              <div className="mb-2">
                <div className="text-[11px] text-gray-400 mb-1.5">Clothing</div>
                <div className="flex flex-wrap gap-1.5 mb-3">
                  {SIZE_PRESETS_CLOTHING.map((s) => (
                    <button
                      key={s}
                      type="button"
                      onClick={() => toggleSize(s)}
                      className={`px-3 py-1 rounded-lg text-xs font-medium border transition-all ${vs.sizes.includes(s) ? "bg-indigo-600 text-white border-indigo-600" : "bg-white text-gray-600 border-gray-200 hover:border-indigo-300"}`}
                    >
                      {s}
                    </button>
                  ))}
                </div>
                <div className="text-[11px] text-gray-400 mb-1.5">Numbers</div>
                <div className="flex flex-wrap gap-1.5">
                  {SIZE_PRESETS_NUMBERS.map((s) => (
                    <button
                      key={s}
                      type="button"
                      onClick={() => toggleSize(s)}
                      className={`px-3 py-1 rounded-lg text-xs font-medium border transition-all ${vs.sizes.includes(s) ? "bg-indigo-600 text-white border-indigo-600" : "bg-white text-gray-600 border-gray-200 hover:border-indigo-300"}`}
                    >
                      {s}
                    </button>
                  ))}
                </div>
              </div>

              {/* Custom size */}
              <div className="flex items-center gap-2 mb-3">
                <input
                  type="text"
                  placeholder="Custom size"
                  value={vs.customSize}
                  onChange={(e) => setVs((prev) => ({ ...prev, customSize: e.target.value }))}
                  onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addCustomSize(); }}}
                  className="flex-1 border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
                <button
                  type="button"
                  onClick={addCustomSize}
                  className="px-3 py-1.5 bg-indigo-50 text-indigo-600 rounded-lg text-sm font-medium hover:bg-indigo-100"
                >
                  + Add
                </button>
              </div>

              {/* Selected size pills */}
              {vs.sizes.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {vs.sizes.map((s) => (
                    <span key={s} className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-gray-100 text-gray-700">
                      {s}
                      <button type="button" onClick={() => removeSize(s)} className="text-gray-400 hover:text-gray-700">×</button>
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Step 4: Stock grid */}
          {keys.length > 0 && (
            <div>
              <div className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">Step 4 — Stock per Variant</div>

              {/* Fill all */}
              <div className="flex items-center gap-2 mb-3">
                <span className="text-xs text-gray-500">Fill empty with:</span>
                <input
                  type="number"
                  min="0"
                  value={vs.fillAll}
                  onChange={(e) => setVs((prev) => ({ ...prev, fillAll: e.target.value }))}
                  placeholder="qty"
                  className="w-16 border border-gray-200 rounded-lg px-2 py-1 text-sm text-center focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
                <button
                  type="button"
                  onClick={applyFillAll}
                  className="px-3 py-1 bg-gray-100 text-gray-600 rounded-lg text-xs font-medium hover:bg-gray-200"
                >
                  Apply
                </button>
              </div>

              {/* Matrix: both colors and sizes */}
              {isBoth && (
                <div className="overflow-x-auto">
                  <table className="text-xs w-full">
                    <thead>
                      <tr>
                        <th className="text-left text-gray-400 font-medium pb-2 pr-2 min-w-[60px]"></th>
                        {vs.sizes.map((s) => (
                          <th key={s} className="text-center text-gray-500 font-medium pb-2 px-1 min-w-[48px]">{s}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {vs.colors.map((c) => (
                        <tr key={c}>
                          <td className="pr-2 py-1">
                            <span className="inline-flex items-center gap-1 font-medium text-gray-700">
                              <span className="w-2.5 h-2.5 rounded-full border border-black/10" style={{ backgroundColor: colorHexMap[c] || "#888" }} />
                              {c}
                            </span>
                          </td>
                          {vs.sizes.map((s) => {
                            const k = variantKey(c, s);
                            const val = vs.stockMatrix[k] || "";
                            return (
                              <td key={s} className="px-1 py-1">
                                <input
                                  type="number"
                                  min="0"
                                  value={val}
                                  onChange={(e) => setStock(k, e.target.value)}
                                  className={`w-12 border rounded text-center text-xs py-1 focus:outline-none focus:ring-1 focus:ring-indigo-500 ${!val || val === "0" ? "border-gray-200 bg-gray-50 text-gray-400" : "border-indigo-200 bg-white text-gray-900"}`}
                                />
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {/* List: colors only */}
              {isColorsOnly && (
                <div className="flex flex-col gap-1.5">
                  {vs.colors.map((c) => {
                    const k = variantKey(c);
                    return (
                      <div key={c} className="flex items-center gap-3">
                        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-gray-700 w-24">
                          <span className="w-2.5 h-2.5 rounded-full border border-black/10" style={{ backgroundColor: colorHexMap[c] || "#888" }} />
                          {c}
                        </span>
                        <input
                          type="number" min="0"
                          value={vs.stockMatrix[k] || ""}
                          onChange={(e) => setStock(k, e.target.value)}
                          placeholder="0"
                          className="w-20 border border-gray-200 rounded-lg px-2 py-1 text-sm text-center focus:outline-none focus:ring-2 focus:ring-indigo-500"
                        />
                        <span className="text-xs text-gray-400">pieces</span>
                      </div>
                    );
                  })}
                </div>
              )}

              {/* List: sizes only */}
              {isSizesOnly && (
                <div className="flex flex-col gap-1.5">
                  {vs.sizes.map((s) => {
                    const k = variantKey(undefined, s);
                    return (
                      <div key={s} className="flex items-center gap-3">
                        <span className="text-xs font-medium text-gray-700 w-12">{s}</span>
                        <input
                          type="number" min="0"
                          value={vs.stockMatrix[k] || ""}
                          onChange={(e) => setStock(k, e.target.value)}
                          placeholder="0"
                          className="w-20 border border-gray-200 rounded-lg px-2 py-1 text-sm text-center focus:outline-none focus:ring-2 focus:ring-indigo-500"
                        />
                        <span className="text-xs text-gray-400">pieces</span>
                      </div>
                    );
                  })}
                </div>
              )}

              {/* Total */}
              <div className="mt-3 text-xs text-gray-500 bg-gray-50 rounded-lg px-3 py-2">
                Total stock: <span className="font-bold text-gray-900">{total} pieces</span> across <span className="font-bold text-gray-900">{variantCount}</span> variant{variantCount !== 1 ? "s" : ""}
              </div>

              {/* Step 5: Per-variant price */}
              <div className="mt-3 flex items-center justify-between py-1 border-t border-gray-100 pt-3">
                <div className="text-xs text-gray-600">Some variants have different price?</div>
                <button
                  type="button"
                  onClick={() => setVs((prev) => ({ ...prev, useDiffPrice: !prev.useDiffPrice }))}
                  className={`relative w-9 h-5 rounded-full transition-colors ${vs.useDiffPrice ? "bg-indigo-600" : "bg-gray-300"}`}
                >
                  <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform ${vs.useDiffPrice ? "translate-x-4" : ""}`} />
                </button>
              </div>

              {vs.useDiffPrice && keys.length > 0 && (
                <div className="mt-2 flex flex-col gap-1.5">
                  {keys.map((k) => {
                    const [c, s] = k.split("||");
                    const label = [c, s].filter(Boolean).join(" / ");
                    return (
                      <div key={k} className="flex items-center gap-2">
                        <span className="text-xs text-gray-600 flex-1 truncate">{label}</span>
                        <div className="relative">
                          <span className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400 text-xs">₹</span>
                          <input
                            type="number" min="0" step="0.01"
                            value={vs.priceMatrix[k] || ""}
                            onChange={(e) => setPrice(k, e.target.value)}
                            placeholder="same as base"
                            className="w-28 border border-gray-200 rounded-lg pl-5 pr-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-indigo-500"
                          />
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Product card ──────────────────────────────────────────────────────────────

function ProductCard({
  product: p,
  onEdit,
  onStock,
  onToggle,
  onDelete,
  resolveImage,
}: {
  product: Product;
  onEdit: () => void;
  onStock: () => void;
  onToggle: () => void;
  onDelete: () => void;
  resolveImage: (url: string) => string;
}) {
  const imgSrc = p.image_url ? resolveImage(p.image_url) : null;

  const PLACEHOLDER_MAP: Record<string, { emoji: string; bg: string }> = {
    Saree:     { emoji: "🥻", bg: "#EDE9FE" },
    Lehenga:   { emoji: "👗", bg: "#FCE7F3" },
    Kurti:     { emoji: "👕", bg: "#DBEAFE" },
    Dupatta:   { emoji: "🧣", bg: "#FEF3C7" },
    Jewellery: { emoji: "💍", bg: "#FDF2F8" },
    Other:     { emoji: "📦", bg: "#F3F4F6" },
  };
  const placeholder = PLACEHOLDER_MAP[p.category ?? ""] ?? { emoji: "📦", bg: "#F3F4F6" };

  return (
    <div className={`bg-white rounded-xl border border-gray-100 shadow-sm hover:shadow-md transition-shadow duration-200 overflow-hidden flex flex-col ${!p.is_active ? "opacity-60" : ""}`}>
      <div className="h-40 flex items-center justify-center overflow-hidden shrink-0" style={imgSrc ? {} : { backgroundColor: placeholder.bg }}>
        {imgSrc ? (
          <img src={imgSrc} alt={p.name} className="w-full h-full object-cover" />
        ) : (
          <span className="text-5xl">{placeholder.emoji}</span>
        )}
      </div>

      <div className="p-4 flex flex-col gap-2.5 flex-1">
        <div className="flex items-center gap-2 flex-wrap">
          {p.sku && <SkuPill sku={p.sku} />}
          {!p.is_active && (
            <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-gray-100 text-gray-500">Inactive</span>
          )}
        </div>

        <h3 className="font-semibold text-gray-900 text-sm leading-snug">{toTitleCase(p.name)}</h3>

        {p.category && (
          <span className="text-xs text-gray-400">{p.category}</span>
        )}

        <div className="text-xl font-bold text-indigo-600">
          ₹{p.price.toLocaleString("en-IN")}
        </div>

        {p.has_variants && (p.available_colors.length > 0 || p.available_sizes.length > 0) && (
          <div className="flex flex-col gap-1.5">
            <ColorDots colors={p.available_colors} />
            <SizePills variants={p.variants} sizes={p.available_sizes} />
          </div>
        )}

        <StockBadge product={p} />

        <div className="flex items-center gap-1.5 mt-auto pt-2 border-t border-gray-50">
          <button onClick={onEdit} className="flex-1 text-xs text-gray-600 border border-gray-200 rounded-lg py-1.5 hover:border-indigo-300 hover:text-indigo-600 transition-all">Edit</button>
          <button onClick={onStock} className="flex-1 text-xs text-gray-600 border border-gray-200 rounded-lg py-1.5 hover:border-green-300 hover:text-green-600 transition-all">Stock</button>
          <button
            onClick={onToggle}
            className={`flex-1 text-xs border rounded-lg py-1.5 transition-all ${
              p.is_active
                ? "text-gray-600 border-gray-200 hover:border-amber-300 hover:text-amber-600"
                : "text-indigo-600 border-indigo-200 hover:bg-indigo-50"
            }`}
          >
            {p.is_active ? "Pause" : "Activate"}
          </button>
          <button onClick={onDelete} className="text-xs text-gray-300 border border-gray-100 rounded-lg px-2.5 py-1.5 hover:text-red-500 hover:border-red-200 transition-all">🗑</button>
        </div>
      </div>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function Catalogue() {
  const { t } = useTranslation();
  const [products, setProducts] = useState<Product[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [search, setSearch] = useState("");
  const [catFilter, setCatFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | "active" | "inactive">("all");
  const [lowStockFilter, setLowStockFilter] = useState(false);

  const [modal, setModal] = useState<"add" | "edit" | "stock" | null>(null);
  const [selected, setSelected] = useState<Product | null>(null);
  const [stockHistory, setStockHistory] = useState<StockLog[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);

  const [form, setForm] = useState<ProductForm>(EMPTY_FORM);
  const [variantState, setVariantState] = useState<VariantState>(EMPTY_VARIANT_STATE);
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [imagePreview, setImagePreview] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Simple stock adjustment (no variants)
  const [stockAmt, setStockAmt] = useState("1");
  const [stockSign, setStockSign] = useState<1 | -1>(1);
  const [stockReason, setStockReason] = useState("sold");
  const [adjusting, setAdjusting] = useState(false);

  // Per-variant stock adjustment
  const [variantStockInputs, setVariantStockInputs] = useState<Record<number, string>>({});

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await listProducts();
      setProducts(data);
    } catch {
      setError("Could not load products.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const totalValue = products.reduce((s, p) => s + p.price * (p.stock ?? 0), 0);
  const activeCount = products.filter((p) => p.is_active).length;
  const lowStockCount = products.filter((p) => p.stock !== null && p.stock <= p.low_stock_alert).length;

  const visible = products.filter((p) => {
    if (catFilter && p.category !== catFilter) return false;
    if (statusFilter === "active" && !p.is_active) return false;
    if (statusFilter === "inactive" && p.is_active) return false;
    if (lowStockFilter && (p.stock === null || p.stock > p.low_stock_alert)) return false;
    if (search) {
      const q = search.toLowerCase();
      if (
        !p.name.toLowerCase().includes(q) &&
        !(p.description ?? "").toLowerCase().includes(q) &&
        !(p.category ?? "").toLowerCase().includes(q) &&
        !(p.sku ?? "").toLowerCase().includes(q)
      ) return false;
    }
    return true;
  });

  function openAdd() {
    setForm(EMPTY_FORM);
    setVariantState(EMPTY_VARIANT_STATE);
    setImageFile(null);
    setImagePreview("");
    setFormError(null);
    setModal("add");
  }

  async function openEdit(p: Product) {
    setSelected(p);
    setForm({
      name: p.name,
      category: p.category ?? "",
      description: p.description ?? "",
      price: String(p.price),
      stock: p.stock != null ? String(p.stock) : "",
      low_stock_alert: String(p.low_stock_alert),
      is_active: p.is_active,
      image_url: p.image_url ?? "",
    });

    // Pre-fill variant state from existing variants
    if (p.has_variants && p.variants.length > 0) {
      const activeVariants = p.variants.filter((v) => v.is_active);
      const colors = [...new Set(activeVariants.map((v) => v.color).filter(Boolean))] as string[];
      const sizes = [...new Set(activeVariants.map((v) => v.size).filter(Boolean))] as string[];
      const stockMatrix: Record<string, string> = {};
      const priceMatrix: Record<string, string> = {};
      const editVariantIds: Record<string, number> = {};

      activeVariants.forEach((v) => {
        const k = variantKey(v.color ?? undefined, v.size ?? undefined);
        stockMatrix[k] = String(v.stock);
        if (v.price != null) priceMatrix[k] = String(v.price);
        editVariantIds[k] = v.id;
      });

      const hasDiffPrice = activeVariants.some((v) => v.price != null);

      setVariantState({
        ...EMPTY_VARIANT_STATE,
        hasVariants: true,
        useColors: colors.length > 0,
        useSizes: sizes.length > 0,
        colors,
        sizes,
        stockMatrix,
        priceMatrix,
        useDiffPrice: hasDiffPrice,
        editVariantIds,
      });
    } else {
      setVariantState(EMPTY_VARIANT_STATE);
    }

    setImageFile(null);
    setImagePreview(p.image_url ? resolveImageUrl(p.image_url) : "");
    setFormError(null);
    setStockHistory([]);
    setModal("edit");
    setLoadingHistory(true);
    try {
      const logs = await getStockHistory(p.id);
      setStockHistory(logs);
    } catch { /* optional */ }
    finally { setLoadingHistory(false); }
  }

  function openStock(p: Product) {
    setSelected(p);
    setStockAmt("1");
    setStockSign(1);
    setStockReason("sold");
    // Pre-fill per-variant stock inputs
    const inputs: Record<number, string> = {};
    p.variants.filter((v) => v.is_active).forEach((v) => {
      inputs[v.id] = String(v.stock);
    });
    setVariantStockInputs(inputs);
    setModal("stock");
  }

  function closeModal() {
    setModal(null);
    setSelected(null);
    setFormError(null);
  }

  function resolveImageUrl(url: string): string {
    if (!url) return "";
    if (url.startsWith("http")) return url;
    return `${API_BASE}${url}`;
  }

  function handleImageFile(f: File) {
    setImageFile(f);
    setImagePreview(URL.createObjectURL(f));
    setForm((prev) => ({ ...prev, image_url: "" }));
  }

  function buildVariantsPayload() {
    const keys = getVariantKeys(variantState);
    return keys.map((k) => {
      const [color, size] = k.split("||");
      const existingId = variantState.editVariantIds[k];
      return {
        ...(existingId ? { id: existingId } : {}),
        color: color || undefined,
        size: size || undefined,
        stock: parseInt(variantState.stockMatrix[k] || "0", 10) || 0,
        price: variantState.useDiffPrice && variantState.priceMatrix[k]
          ? parseFloat(variantState.priceMatrix[k]) || undefined
          : undefined,
      };
    });
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setFormError(null);
    try {
      let imageUrl = form.image_url;
      if (imageFile) {
        try { imageUrl = await uploadProductImage(imageFile); }
        catch { setFormError("Image upload failed. Product saved without image."); imageUrl = form.image_url; }
      }

      const hasVariants = variantState.hasVariants && getVariantKeys(variantState).length > 0;
      const variants = hasVariants ? buildVariantsPayload() : undefined;
      const totalStock = hasVariants ? variants!.reduce((s, v) => s + v.stock, 0) : undefined;

      const payload = {
        name: form.name.trim(),
        price: parseFloat(form.price),
        stock: hasVariants ? totalStock : (form.stock !== "" ? parseInt(form.stock, 10) : undefined),
        description: form.description.trim() || undefined,
        image_url: imageUrl || undefined,
        category: form.category || undefined,
        is_active: form.is_active,
        low_stock_alert: parseInt(form.low_stock_alert, 10) || 5,
        has_variants: hasVariants,
        variants,
      };

      if (modal === "add") {
        const created = await addProduct(payload);
        setProducts((prev) => [...prev, created]);
      } else if (modal === "edit" && selected) {
        const updated = await updateProduct(selected.id, payload);
        setProducts((prev) => prev.map((p) => (p.id === selected.id ? updated : p)));
      }
      closeModal();
    } catch {
      setFormError("Failed to save product. Please try again.");
    } finally {
      setSaving(false);
    }
  }

  async function handleAdjust() {
    if (!selected) return;
    setAdjusting(true);
    try {
      if (selected.has_variants && selected.variants.length > 0) {
        const adjustments = selected.variants
          .filter((v) => v.is_active)
          .map((v) => ({
            variant_id: v.id,
            new_stock: parseInt(variantStockInputs[v.id] || String(v.stock), 10) || 0,
          }));
        const updated = await adjustVariantStocks(selected.id, adjustments, stockReason);
        setProducts((prev) => prev.map((p) => (p.id === selected.id ? updated : p)));
      } else {
        const delta = stockSign * (parseInt(stockAmt, 10) || 0);
        const updated = await adjustStock(selected.id, delta, stockReason);
        setProducts((prev) => prev.map((p) => (p.id === selected.id ? updated : p)));
      }
      closeModal();
    } catch { /* keep open */ }
    finally { setAdjusting(false); }
  }

  async function toggleActive(p: Product) {
    try {
      const updated = await updateProduct(p.id, { is_active: !p.is_active });
      setProducts((prev) => prev.map((x) => (x.id === p.id ? updated : x)));
    } catch { /* silent */ }
  }

  async function handleDelete(p: Product) {
    if (!confirm(`Delete "${toTitleCase(p.name)}"? This cannot be undone.`)) return;
    try {
      await deleteProduct(p.id);
      setProducts((prev) => prev.filter((x) => x.id !== p.id));
    } catch { /* silent */ }
  }

  const activeVariants = (selected?.variants ?? []).filter((v: ProductVariant) => v.is_active);

  return (
    <Layout>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">{t("catalogue.title")}</h1>
          <p className="text-sm text-gray-400 mt-0.5">{products.length} products</p>
        </div>
        <button
          onClick={openAdd}
          className="flex items-center gap-2 bg-indigo-600 text-white px-4 py-2.5 rounded-lg text-sm font-semibold hover:bg-indigo-700 active:scale-95 transition-all shadow-sm"
        >
          <Plus size={16} /> Add Product
        </button>
      </div>

      {/* Low stock banner */}
      {lowStockCount > 0 && (
        <div className="mb-5 flex items-center justify-between bg-amber-50 border border-amber-200 rounded-xl px-4 py-3">
          <div className="flex items-center gap-2 text-amber-700 text-sm font-medium">
            <AlertTriangle size={16} />
            {lowStockCount} product{lowStockCount > 1 ? "s" : ""} running low on stock
          </div>
          <button onClick={() => { setLowStockFilter(true); setStatusFilter("all"); }} className="text-amber-600 text-sm font-medium hover:underline">
            View →
          </button>
        </div>
      )}

      {/* Stats row */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-5">
        {[
          { label: t("catalogue.total_products"), value: products.length, color: "text-gray-900" },
          { label: t("catalogue.active"), value: activeCount, color: "text-green-600" },
          { label: t("catalogue.low_stock"), value: lowStockCount, color: "text-amber-600" },
          { label: "Inventory Value", value: `₹${totalValue.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`, color: "text-indigo-600" },
        ].map((s) => (
          <div key={s.label} className="bg-white rounded-xl border border-gray-100 shadow-sm px-5 py-4">
            <div className={`text-2xl font-bold ${s.color}`}>{s.value}</div>
            <div className="text-xs text-gray-400 mt-0.5 uppercase tracking-wide">{s.label}</div>
          </div>
        ))}
      </div>

      {/* Filter bar */}
      <div className="bg-white rounded-xl border border-gray-100 shadow-sm px-4 py-3 mb-5 flex flex-wrap gap-3 items-center">
        <div className="relative flex-1 min-w-[160px]">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            type="text"
            placeholder="Search products…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full pl-8 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-400 bg-gray-50"
          />
        </div>

        <select
          value={catFilter}
          onChange={(e) => setCatFilter(e.target.value)}
          className="text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-400 bg-white"
        >
          <option value="">All categories</option>
          {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>

        <div className="flex rounded-lg border border-gray-200 overflow-hidden text-sm">
          {(["all", "active", "inactive"] as const).map((s) => (
            <button
              key={s}
              onClick={() => setStatusFilter(s)}
              className={`px-3 py-2 capitalize transition-colors ${statusFilter === s ? "bg-indigo-600 text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}
            >
              {s}
            </button>
          ))}
        </div>

        <button
          onClick={() => setLowStockFilter((v) => !v)}
          className={`flex items-center gap-1.5 px-3 py-2 rounded-lg border text-sm transition-all ${
            lowStockFilter ? "bg-amber-100 border-amber-300 text-amber-700" : "bg-white border-gray-200 text-gray-600 hover:bg-gray-50"
          }`}
        >
          <AlertTriangle size={13} /> Low stock
        </button>

        {(search || catFilter || statusFilter !== "all" || lowStockFilter) && (
          <button
            onClick={() => { setSearch(""); setCatFilter(""); setStatusFilter("all"); setLowStockFilter(false); }}
            className="text-sm text-gray-400 hover:text-gray-600 ml-auto"
          >
            Clear filters
          </button>
        )}
      </div>

      {/* Product grid */}
      {loading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {[...Array(6)].map((_, i) => (
            <div key={i} className="bg-white rounded-xl border border-gray-100 h-72 animate-pulse" />
          ))}
        </div>
      ) : error ? (
        <div className="bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-600">{error}</div>
      ) : visible.length === 0 ? (
        <div className="bg-white rounded-xl border border-gray-100 shadow-sm px-5 py-16 text-center">
          <Package size={48} className="text-gray-200 mx-auto mb-3" />
          <p className="text-gray-400 text-sm">
            {products.length === 0
              ? "No products yet. Add your first product to help the AI answer customer queries."
              : "No products match the current filters."}
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {visible.map((p) => (
            <ProductCard
              key={p.id}
              product={p}
              onEdit={() => openEdit(p)}
              onStock={() => openStock(p)}
              onToggle={() => toggleActive(p)}
              onDelete={() => handleDelete(p)}
              resolveImage={resolveImageUrl}
            />
          ))}
        </div>
      )}

      {/* ── Add / Edit side panel ──────────────────────────────────────────── */}
      {(modal === "add" || modal === "edit") && (
        <div className="fixed inset-0 z-40 flex justify-end">
          <div className="flex-1 bg-black/40" onClick={closeModal} />
          <div className="w-full max-w-[520px] bg-white flex flex-col" style={{ height: "100vh" }}>
            {/* Fixed header */}
            <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100 shrink-0 bg-white">
              <h2 className="text-lg font-bold text-gray-900">
                {modal === "add" ? "Add Product" : `Edit — ${toTitleCase(selected?.name ?? "")}`}
              </h2>
              <button onClick={closeModal} className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition-colors">
                <X size={18} />
              </button>
            </div>

            {/* Scrollable content */}
            <form onSubmit={handleSave} className="flex flex-col flex-1 overflow-hidden">
              <div className="flex-1 overflow-y-auto px-6 py-5 flex flex-col gap-5">

                {/* Image upload — first element */}
                <div>
                  <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-2">Product Image</label>
                  <ImageDropZone preview={imagePreview} onFile={handleImageFile} />
                </div>

                <div>
                  <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Product Name <span className="text-red-400">*</span></label>
                  <input required type="text" value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} placeholder="e.g. Banarasi Silk Saree"
                    className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent" />
                </div>

                <div>
                  <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Category</label>
                  <select value={form.category} onChange={(e) => setForm((f) => ({ ...f, category: e.target.value }))}
                    className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 bg-white">
                    <option value="">Select category…</option>
                    {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                  <p className="text-[10px] text-gray-400 mt-1">SKU is auto-generated from category.</p>
                </div>

                <div>
                  <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Description</label>
                  <textarea rows={2} value={form.description} onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))} placeholder="Optional product description"
                    className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 resize-none" />
                </div>

                <div>
                  <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Price (₹) <span className="text-red-400">*</span></label>
                  <div className="relative">
                    <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 text-sm">₹</span>
                    <input required type="number" min="0" step="0.01" value={form.price} onChange={(e) => setForm((f) => ({ ...f, price: e.target.value }))} placeholder="0"
                      className="w-full border border-gray-200 rounded-lg pl-7 pr-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500" />
                  </div>
                </div>

                {/* Stock Qty — hidden when variants are on */}
                {!variantState.hasVariants && (
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Stock Qty</label>
                      <input type="number" min="0" value={form.stock} onChange={(e) => setForm((f) => ({ ...f, stock: e.target.value }))} placeholder="Optional"
                        className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500" />
                    </div>
                    <div>
                      <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Low Stock Alert</label>
                      <input type="number" min="0" value={form.low_stock_alert} onChange={(e) => setForm((f) => ({ ...f, low_stock_alert: e.target.value }))}
                        className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500" />
                    </div>
                  </div>
                )}

                {/* Variant builder */}
                <VariantBuilder vs={variantState} setVs={setVariantState} />

                <div className="flex items-center justify-between py-1">
                  <div>
                    <div className="text-sm font-medium text-gray-700">Active</div>
                    <div className="text-xs text-gray-400">Inactive products are hidden from AI</div>
                  </div>
                  <button type="button" onClick={() => setForm((f) => ({ ...f, is_active: !f.is_active }))}
                    className={`relative w-11 h-6 rounded-full transition-colors ${form.is_active ? "bg-indigo-600" : "bg-gray-300"}`}>
                    <span className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform ${form.is_active ? "translate-x-5" : ""}`} />
                  </button>
                </div>

                {formError && <p className="text-xs text-red-500 bg-red-50 rounded-lg px-3 py-2">{formError}</p>}

                {/* Stock history (edit only) */}
                {modal === "edit" && (
                  <div className="border-t border-gray-100 pt-4">
                    <h3 className="text-xs font-medium uppercase tracking-wide text-gray-400 mb-3">Stock History</h3>
                    {loadingHistory ? (
                      <p className="text-xs text-gray-400">Loading…</p>
                    ) : stockHistory.length === 0 ? (
                      <p className="text-xs text-gray-400">No adjustments yet.</p>
                    ) : (
                      <div className="space-y-1.5 max-h-36 overflow-y-auto">
                        {stockHistory.map((log) => (
                          <div key={log.id} className="flex items-center justify-between text-xs text-gray-600 bg-gray-50 rounded-lg px-3 py-2">
                            <span className={`font-semibold ${log.adjustment >= 0 ? "text-green-600" : "text-red-500"}`}>
                              {log.adjustment >= 0 ? "+" : ""}{log.adjustment}
                            </span>
                            <span className="capitalize text-gray-500">{log.reason}</span>
                            <span className="text-gray-400">{log.stock_before} → {log.stock_after}</span>
                            <span className="text-gray-300">{new Date(log.created_at).toLocaleDateString("en-IN", { day: "numeric", month: "short" })}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>

              {/* Fixed footer */}
              <div className="shrink-0 px-6 py-4 border-t border-gray-100 bg-white">
                <button
                  type="submit"
                  disabled={saving}
                  className="w-full bg-indigo-600 text-white py-3 rounded-lg text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors"
                >
                  {saving ? "Saving…" : modal === "add" ? "Add Product" : "Save Changes"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* ── Stock adjustment modal ─────────────────────────────────────────── */}
      {modal === "stock" && selected && (
        <div className="fixed inset-0 z-40 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={closeModal} />
          <div className="relative bg-white rounded-2xl shadow-xl w-full max-w-sm z-10 flex flex-col max-h-[90vh]">
            <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100 shrink-0">
              <div>
                <h2 className="text-lg font-bold text-gray-900">Adjust Stock</h2>
                <p className="text-xs text-gray-500 truncate mt-0.5">{toTitleCase(selected.name)}</p>
              </div>
              <button onClick={closeModal} className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-gray-100 text-gray-400 transition-colors">
                <X size={18} />
              </button>
            </div>

            <div className="overflow-y-auto flex-1 p-6">
              {selected.has_variants && activeVariants.length > 0 ? (
                /* Per-variant adjustment */
                <div className="flex flex-col gap-3">
                  <p className="text-xs text-gray-500 mb-1">Set new stock for each variant:</p>
                  {activeVariants.map((v: ProductVariant) => {
                    const label = [v.color, v.size].filter(Boolean).join(" / ") || `Variant ${v.id}`;
                    return (
                      <div key={v.id} className="flex items-center justify-between gap-3">
                        <span className="text-sm text-gray-700 flex-1 truncate">{label}</span>
                        <div className="flex items-center gap-2 shrink-0">
                          <span className="text-xs text-gray-400">was {v.stock}</span>
                          <input
                            type="number" min="0"
                            value={variantStockInputs[v.id] ?? String(v.stock)}
                            onChange={(e) => setVariantStockInputs((prev) => ({ ...prev, [v.id]: e.target.value }))}
                            className="w-16 border border-gray-200 rounded-lg px-2 py-1.5 text-sm text-center focus:outline-none focus:ring-2 focus:ring-indigo-500"
                          />
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                /* Simple +/- adjustment */
                <div>
                  <div className="bg-gray-50 rounded-xl px-4 py-3 mb-5">
                    <div className="text-sm font-semibold text-gray-800 truncate">{toTitleCase(selected.name)}</div>
                    <div className="text-xs text-gray-500 mt-0.5">Current stock: <span className="font-bold text-gray-900">{selected.stock ?? "—"}</span></div>
                  </div>

                  <div className="flex gap-2 mb-4">
                    <button onClick={() => setStockSign(1)}
                      className={`flex-1 py-2.5 rounded-xl text-sm font-medium transition-all border ${stockSign === 1 ? "bg-green-100 text-green-700 border-green-200" : "bg-white text-gray-500 border-gray-200 hover:bg-gray-50"}`}>
                      + Add
                    </button>
                    <button onClick={() => setStockSign(-1)}
                      className={`flex-1 py-2.5 rounded-xl text-sm font-medium transition-all border ${stockSign === -1 ? "bg-red-100 text-red-700 border-red-200" : "bg-white text-gray-500 border-gray-200 hover:bg-gray-50"}`}>
                      − Remove
                    </button>
                  </div>

                  <input type="number" min="1" value={stockAmt} onChange={(e) => setStockAmt(e.target.value)}
                    className="w-full border border-gray-200 rounded-xl px-4 py-3 text-center text-2xl font-bold focus:outline-none focus:ring-2 focus:ring-indigo-500 mb-3" />

                  {selected.stock !== null && stockAmt && (
                    <div className="text-center text-sm text-gray-500 mb-4">
                      New stock: <span className="font-bold text-gray-900">{Math.max(0, selected.stock + stockSign * (parseInt(stockAmt, 10) || 0))}</span>
                    </div>
                  )}
                </div>
              )}

              <select value={stockReason} onChange={(e) => setStockReason(e.target.value)}
                className="w-full border border-gray-200 rounded-xl px-4 py-2.5 text-sm mt-4 focus:outline-none focus:ring-2 focus:ring-indigo-500 bg-white">
                {STOCK_REASONS.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
              </select>
            </div>

            <div className="shrink-0 px-6 py-4 border-t border-gray-100">
              <button
                onClick={handleAdjust}
                disabled={adjusting || (!selected.has_variants && (!stockAmt || parseInt(stockAmt, 10) <= 0))}
                className="w-full bg-indigo-600 text-white py-3 rounded-xl text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors"
              >
                {adjusting ? "Saving…" : "Confirm Adjustment"}
              </button>
            </div>
          </div>
        </div>
      )}
    </Layout>
  );
}
