import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import {
  ArrowLeft, ShoppingCart, Bell, Share2,
  Minus, Plus, CheckCircle, AlertTriangle,
  XCircle, X, Copy, Check, Heart,
} from "lucide-react";
import { publicApi, type PublicProduct, type ProductDetailResponse } from "../../api/publicApi";
import { colorHex, sortSizes, stockForSize } from "../../utils/variants";

// ── Helpers ───────────────────────────────────────────────────────────────────

function toTitleCase(str: string): string {
  return str.replace(/\b\w/g, (c) => c.toUpperCase());
}

const CATEGORY_EMOJI: Record<string, string> = {
  saree: "🥻", lehenga: "👘", kurti: "👗",
  dress: "👗", suit: "👔", shirt: "👕",
};

function categoryEmoji(category?: string | null): string {
  return CATEGORY_EMOJI[category?.toLowerCase() ?? ""] ?? "📦";
}

function formatPrice(n: number): string {
  return "₹" + n.toLocaleString("en-IN");
}

// ── Stock Pill ────────────────────────────────────────────────────────────────

function StockPill({ product, available, stock }: { product: PublicProduct; available?: boolean; stock?: number }) {
  const isAvailable = available ?? product.is_available;
  const stockCount = stock ?? product.stock;

  if (!isAvailable) {
    return (
      <span className="inline-flex items-center gap-1 bg-red-50 text-red-600 text-[11px] font-semibold px-3 py-1 rounded-full border border-red-100">
        <XCircle size={11} />
        Out of Stock
      </span>
    );
  }
  if (stockCount !== null && stockCount !== undefined && stockCount <= product.low_stock_alert) {
    return (
      <motion.span
        animate={{ opacity: [1, 0.65, 1] }}
        transition={{ repeat: Infinity, duration: 1.6 }}
        className="inline-flex items-center gap-1 bg-amber-50 text-amber-600 text-[11px] font-semibold px-3 py-1 rounded-full border border-amber-100"
      >
        <AlertTriangle size={11} />
        ⚡ Only {stockCount} left
      </motion.span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 bg-green-50 text-green-600 text-[11px] font-semibold px-3 py-1 rounded-full border border-green-100">
      <CheckCircle size={11} />
      In Stock
    </span>
  );
}

// ── Variant Selector ──────────────────────────────────────────────────────────

function VariantSelector({
  product, themeColor, selectedColor, selectedSize, onColorChange, onSizeChange,
}: {
  product: PublicProduct;
  themeColor: string;
  selectedColor: string | null;
  selectedSize: string | null;
  onColorChange: (c: string) => void;
  onSizeChange: (s: string) => void;
}) {
  const { available_colors: colors, available_sizes: sizes, variants } = product;
  if (colors.length === 0 && sizes.length === 0) return null;

  function colorInStock(color: string): boolean {
    return variants.filter((v) => v.color === color).reduce((s, v) => s + v.stock, 0) > 0;
  }

  function sizeInStock(size: string): boolean {
    if (selectedColor) {
      return variants.some((v) => v.color === selectedColor && v.size === size && v.stock > 0);
    }
    return stockForSize(variants, size) > 0;
  }

  const selectedVariant = variants.find(
    (v) =>
      (colors.length === 0 || v.color === selectedColor) &&
      (sizes.length === 0 || v.size === selectedSize)
  );
  const stock = selectedVariant?.stock ?? 0;

  return (
    <div className="mb-5 flex flex-col gap-5">
      {colors.length > 0 && (
        <div>
          <p className="text-xs font-bold text-gray-700 mb-2.5 uppercase tracking-wide">
            Colour{selectedColor ? `: ${selectedColor}` : ""}
          </p>
          <div className="flex items-center gap-3 flex-wrap">
            {colors.map((color) => {
              const inStock = colorInStock(color);
              const selected = selectedColor === color;
              return (
                <button
                  key={color}
                  type="button"
                  onClick={() => inStock && onColorChange(color)}
                  disabled={!inStock}
                  className="flex flex-col items-center gap-1.5 disabled:cursor-not-allowed"
                >
                  <span
                    className="relative w-10 h-10 rounded-full border-2 flex items-center justify-center"
                    style={{
                      backgroundColor: colorHex(color),
                      borderColor: selected ? themeColor : "transparent",
                      boxShadow: selected ? `0 0 0 2px ${themeColor}33` : "0 0 0 1px rgba(0,0,0,0.08)",
                    }}
                  >
                    {!inStock && (
                      <span className="absolute inset-0 rounded-full bg-white/60 flex items-center justify-center">
                        <X size={16} className="text-gray-500" strokeWidth={3} />
                      </span>
                    )}
                  </span>
                  <span className={`text-[11px] font-medium ${inStock ? "text-gray-600" : "text-gray-300"}`}>
                    {color}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {sizes.length > 0 && (
        <div>
          <p className="text-xs font-bold text-gray-700 mb-2.5 uppercase tracking-wide">
            Size{selectedSize ? `: ${selectedSize}` : ""}
          </p>
          <div className="flex items-center gap-2 flex-wrap">
            {sortSizes(sizes).map((size) => {
              const inStock = sizeInStock(size);
              const selected = selectedSize === size;
              return (
                <button
                  key={size}
                  type="button"
                  onClick={() => inStock && onSizeChange(size)}
                  disabled={!inStock}
                  className={`min-w-[44px] h-10 px-3 rounded-lg border-2 text-sm font-semibold transition-colors ${
                    !inStock
                      ? "border-gray-100 text-gray-300 line-through bg-gray-50 cursor-not-allowed"
                      : selected
                      ? "text-white"
                      : "border-gray-200 text-gray-700 hover:border-gray-300"
                  }`}
                  style={selected && inStock ? { backgroundColor: themeColor, borderColor: themeColor } : undefined}
                >
                  {size}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {selectedVariant && (
        <p className="text-sm font-medium" style={{ color: stock > 0 ? themeColor : "#EF4444" }}>
          {stock > 0
            ? `${stock} piece${stock === 1 ? "" : "s"} available${
                selectedColor ? ` in ${selectedColor}${selectedSize ? ` ${selectedSize}` : ""}` : ""
              }`
            : `Out of stock${selectedColor ? ` in ${selectedColor}${selectedSize ? ` ${selectedSize}` : ""}` : ""}`}
        </p>
      )}
    </div>
  );
}

// ── Quantity Selector ─────────────────────────────────────────────────────────

function QtySelector({
  qty, max, onChange,
}: { qty: number; max: number; onChange: (n: number) => void }) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-gray-400 font-medium">Qty</span>
      <div className="flex items-center bg-gray-100 rounded-xl overflow-hidden">
        <motion.button
          whileTap={{ scale: 0.85 }}
          onClick={() => onChange(Math.max(1, qty - 1))}
          className="w-9 h-9 flex items-center justify-center text-gray-500 hover:text-gray-800 transition-colors"
        >
          <Minus size={14} />
        </motion.button>
        <span className="w-8 text-center text-sm font-bold text-gray-900">{qty}</span>
        <motion.button
          whileTap={{ scale: 0.85 }}
          onClick={() => onChange(Math.min(max, qty + 1))}
          className="w-9 h-9 flex items-center justify-center text-gray-500 hover:text-gray-800 transition-colors"
        >
          <Plus size={14} />
        </motion.button>
      </div>
    </div>
  );
}

// ── Notify Modal ──────────────────────────────────────────────────────────────

function NotifyModal({
  product, slug, themeColor, onClose,
}: { product: PublicProduct; slug: string; themeColor: string; onClose: () => void }) {
  const [phone, setPhone] = useState("");
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!product.sku) return;
    setLoading(true);
    await publicApi.notifyRestock(slug, product.sku, phone);
    setDone(true);
    setLoading(false);
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/50"
      onClick={onClose}
    >
      <motion.div
        initial={{ y: "100%" }}
        animate={{ y: 0 }}
        exit={{ y: "100%" }}
        transition={{ type: "spring", damping: 28, stiffness: 280 }}
        onClick={(e) => e.stopPropagation()}
        className="bg-white w-full max-w-md rounded-t-[28px] sm:rounded-2xl p-6 pb-10 sm:pb-6 shadow-2xl"
      >
        <div className="w-10 h-1 bg-gray-200 rounded-full mx-auto mb-6 sm:hidden" />

        {done ? (
          <div className="text-center py-4">
            <div
              className="w-16 h-16 rounded-full flex items-center justify-center mx-auto mb-4"
              style={{ backgroundColor: themeColor + "18" }}
            >
              <Bell size={28} style={{ color: themeColor }} />
            </div>
            <h3 className="text-lg font-bold text-gray-900 mb-2">You're on the list!</h3>
            <p className="text-sm text-gray-500 leading-relaxed mb-6">
              We'll WhatsApp you the moment{" "}
              <strong className="text-gray-700">{toTitleCase(product.name)}</strong> is back.
            </p>
            <button
              onClick={onClose}
              className="text-sm font-semibold px-6 py-2 rounded-full"
              style={{ backgroundColor: themeColor + "18", color: themeColor }}
            >
              Done
            </button>
          </div>
        ) : (
          <>
            <div className="flex items-start justify-between mb-2">
              <div>
                <h3 className="text-lg font-bold text-gray-900">Notify me when available</h3>
                <p className="text-sm text-gray-400 mt-0.5 leading-snug">
                  We'll WhatsApp you when{" "}
                  <span className="text-gray-600 font-medium">{toTitleCase(product.name)}</span> is back.
                </p>
              </div>
              <button
                onClick={onClose}
                className="w-8 h-8 flex items-center justify-center rounded-full bg-gray-100 shrink-0 ml-3"
              >
                <X size={15} className="text-gray-500" />
              </button>
            </div>

            <form onSubmit={submit} className="flex flex-col gap-3 mt-5">
              <div
                className="flex items-center border border-gray-200 rounded-xl overflow-hidden focus-within:ring-2 transition-shadow"
                style={{ "--tw-ring-color": themeColor } as React.CSSProperties}
              >
                <span className="px-3 py-3.5 text-gray-500 text-sm bg-gray-50 border-r border-gray-200 shrink-0">
                  +91
                </span>
                <input
                  type="tel"
                  value={phone}
                  onChange={(e) => setPhone(e.target.value)}
                  placeholder="Your WhatsApp number"
                  required
                  className="flex-1 px-3 py-3.5 text-sm focus:outline-none bg-white"
                />
              </div>
              <motion.button
                type="submit"
                disabled={loading}
                whileTap={{ scale: 0.97 }}
                className="py-3.5 rounded-xl text-white font-semibold text-sm disabled:opacity-60 flex items-center justify-center gap-2"
                style={{ backgroundColor: themeColor }}
              >
                <Bell size={15} />
                {loading ? "Saving…" : "Notify Me"}
              </motion.button>
              <button
                type="button"
                onClick={onClose}
                className="text-sm text-gray-400 text-center py-1"
              >
                Cancel
              </button>
            </form>
          </>
        )}
      </motion.div>
    </div>
  );
}

// ── Related Products ──────────────────────────────────────────────────────────

function RelatedProducts({
  slug, currentProductId, category, themeColor,
}: { slug: string; currentProductId: number; category: string | null; themeColor: string }) {
  const [products, setProducts] = useState<PublicProduct[]>([]);

  useEffect(() => {
    publicApi
      .getCatalogue(slug)
      .then((d) => {
        const related = d.products
          .filter((p) => p.id !== currentProductId && (category ? p.category === category : true))
          .slice(0, 8);
        setProducts(related);
      })
      .catch(() => {});
  }, [slug, currentProductId, category]);

  if (products.length === 0) return null;

  function orderOnWhatsApp(p: PublicProduct, businessWa: string | null) {
    if (!businessWa) return;
    const msg = encodeURIComponent(
      `Hi! 👋 I want to order:\n\n🛍️ *${p.name}*\n📦 SKU: ${p.sku || "N/A"}\n💰 Price: ${formatPrice(p.price)}\n\nPlease confirm my order!`
    );
    window.open(`https://wa.me/${businessWa}?text=${msg}`, "_blank");
  }

  return (
    <div>
      <div className="mb-3">
        <h3 className="text-base font-bold text-gray-900">More from this collection</h3>
        <p className="text-xs text-gray-400 mt-0.5">You might also like</p>
      </div>
      <div className="flex gap-3 overflow-x-auto pb-2 scrollbar-hide -mx-4 px-4">
        {products.map((p) => (
          <div key={p.id} className="shrink-0 w-36 flex flex-col">
            <Link
              to={`/shop/${slug}/product/${p.sku || p.id}`}
              className="block w-full aspect-square rounded-xl overflow-hidden bg-gray-50 mb-2 hover:opacity-90 transition-opacity"
            >
              {p.image_url ? (
                <img src={p.image_url} alt={p.name} className="w-full h-full object-cover" />
              ) : (
                <div
                  className="w-full h-full flex items-center justify-center"
                  style={{ background: `linear-gradient(135deg, ${themeColor}22, ${themeColor}08)` }}
                >
                  <span className="text-4xl">{categoryEmoji(p.category)}</span>
                </div>
              )}
            </Link>
            <Link
              to={`/shop/${slug}/product/${p.sku || p.id}`}
              className="text-[13px] font-semibold text-gray-800 line-clamp-2 leading-snug mb-1 hover:opacity-80"
            >
              {toTitleCase(p.name)}
            </Link>
            <p className="text-sm font-bold mb-2" style={{ color: themeColor }}>
              {formatPrice(p.price)}
            </p>
            {p.is_available && (
              <button
                onClick={() => orderOnWhatsApp(p, null)}
                className="w-full text-xs font-semibold py-1.5 rounded-lg text-white transition-opacity hover:opacity-85"
                style={{ backgroundColor: themeColor }}
              >
                Order
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Order Card (desktop inline) ───────────────────────────────────────────────

function DesktopOrderCard({
  qty, total, themeColor, maxQty, available, orderLabel, onQtyChange, onOrder, onNotify,
}: {
  qty: number;
  total: number;
  themeColor: string;
  maxQty: number;
  available: boolean;
  orderLabel: string;
  onQtyChange: (n: number) => void;
  onOrder: () => void;
  onNotify: () => void;
}) {
  return (
    <div className="rounded-2xl border border-gray-200 bg-white p-6 shadow-sm">
      <h4 className="text-sm font-bold text-gray-900 mb-0.5">Place Your Order</h4>
      <p className="text-xs text-gray-400 mb-5">Via WhatsApp — Fast & Easy</p>

      {available ? (
        <>
          <div className="flex items-center justify-between mb-5">
            <QtySelector qty={qty} max={maxQty} onChange={onQtyChange} />
            <div className="text-right">
              <p className="text-[11px] text-gray-400 mb-0.5">Total</p>
              <p className="text-xl font-bold" style={{ color: themeColor }}>
                {formatPrice(total)}
              </p>
            </div>
          </div>

          <motion.button
            onClick={onOrder}
            whileTap={{ scale: 0.97 }}
            className="w-full flex items-center justify-center gap-2 rounded-xl text-white font-bold py-3.5 text-sm hover:opacity-90 transition-opacity mb-4"
            style={{ backgroundColor: themeColor }}
          >
            <ShoppingCart size={17} />
            {orderLabel} — {formatPrice(total)}
          </motion.button>

          <div className="flex items-center justify-center gap-4">
            {["✓ Secure Order", "✓ Fast Delivery", "✓ Easy Returns"].map((t) => (
              <span key={t} className="text-[11px] text-gray-400">{t}</span>
            ))}
          </div>
        </>
      ) : (
        <button
          onClick={onNotify}
          className="w-full flex items-center justify-center gap-2 rounded-xl font-semibold py-3.5 text-sm border-2 border-gray-200 text-gray-700 hover:bg-gray-50 transition-colors"
        >
          <Bell size={16} />
          Notify When Available
        </button>
      )}
    </div>
  );
}

// ── Skeleton ──────────────────────────────────────────────────────────────────

function ProductSkeleton() {
  return (
    <div className="min-h-screen bg-gray-50 animate-pulse">
      <div className="h-14 bg-white border-b" />
      <div className="md:flex md:max-w-screen-lg md:mx-auto">
        <div className="md:w-1/2 aspect-square bg-gray-200" />
        <div className="md:w-1/2 bg-white px-5 pt-5 space-y-4">
          <div className="flex gap-2">
            <div className="h-6 bg-gray-200 rounded-full w-16" />
            <div className="h-6 bg-gray-200 rounded-full w-20" />
          </div>
          <div className="h-7 bg-gray-200 rounded w-4/5" />
          <div className="h-5 bg-gray-200 rounded w-1/3" />
          <div className="h-9 bg-gray-200 rounded w-2/5" />
          <div className="h-px bg-gray-200 w-full" />
          <div className="space-y-2">
            <div className="h-4 bg-gray-200 rounded w-full" />
            <div className="h-4 bg-gray-200 rounded w-4/5" />
            <div className="h-4 bg-gray-200 rounded w-3/4" />
          </div>
          <div className="rounded-xl overflow-hidden border border-gray-100 space-y-px">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-11 bg-gray-200" />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function ProductPage() {
  const { slug, sku } = useParams<{ slug: string; sku: string }>();
  const [data, setData] = useState<ProductDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [qty, setQty] = useState(1);
  const [showNotify, setShowNotify] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [liked, setLiked] = useState(false);
  const [skuCopied, setSkuCopied] = useState(false);
  const [selectedColor, setSelectedColor] = useState<string | null>(null);
  const [selectedSize, setSelectedSize] = useState<string | null>(null);

  useEffect(() => {
    if (!slug || !sku) return;
    publicApi
      .getProduct(slug, sku)
      .then((d) => {
        setData(d);
        setLoading(false);
        document.title = `${toTitleCase(d.product.name)} — ${formatPrice(d.product.price)} — ${d.business.name}`;
        try {
          const faves: string[] = JSON.parse(localStorage.getItem(`faves_${slug}`) || "[]");
          setLiked(faves.includes(String(d.product.id)));
        } catch {}

        // Pre-select the first in-stock colour/size combo, if any.
        const { variants, available_colors, available_sizes } = d.product;
        const firstInStock = variants.find((v) => v.stock > 0);
        if (available_colors.length > 0) setSelectedColor(firstInStock?.color ?? available_colors[0]);
        if (available_sizes.length > 0) setSelectedSize(firstInStock?.size ?? available_sizes[0]);
      })
      .catch(() => { setError(true); setLoading(false); });
  }, [slug, sku]);

  if (loading) return <ProductSkeleton />;

  if (error || !data) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center bg-gray-50 gap-4 px-6 text-center">
        <p className="text-6xl">😕</p>
        <h1 className="text-xl font-bold text-gray-800">Product not found</h1>
        <p className="text-sm text-gray-400">This product may no longer be available.</p>
        <Link
          to={`/shop/${slug}`}
          className="mt-2 inline-flex items-center gap-1.5 text-sm font-semibold px-5 py-2.5 rounded-full text-white"
          style={{ backgroundColor: "#6366f1" }}
        >
          <ArrowLeft size={15} />
          Back to catalogue
        </Link>
      </div>
    );
  }

  const { product, business } = data;
  const theme = business.theme_color;
  const name = toTitleCase(product.name);

  const hasColors = product.available_colors.length > 0;
  const hasSizes = product.available_sizes.length > 0;
  const selectedVariant = product.has_variants
    ? product.variants.find(
        (v) => (!hasColors || v.color === selectedColor) && (!hasSizes || v.size === selectedSize)
      )
    : undefined;
  const effectiveStock = product.has_variants ? selectedVariant?.stock ?? 0 : product.stock ?? 0;
  const effectiveAvailable = product.has_variants ? effectiveStock > 0 : product.is_available;
  const effectiveSku = selectedVariant?.sku || product.sku;
  const variantLabel = [selectedColor, selectedSize].filter(Boolean).join(" ");
  const orderLabel = variantLabel ? `Order ${variantLabel} on WhatsApp` : "Order on WhatsApp";

  const maxQty = effectiveStock > 0 ? effectiveStock : product.stock ?? 99;
  const total = product.price * qty;

  function toggleLike() {
    const key = `faves_${slug}`;
    const faves: string[] = JSON.parse(localStorage.getItem(key) || "[]");
    const id = String(product.id);
    const next = liked ? faves.filter((f) => f !== id) : [...faves, id];
    localStorage.setItem(key, JSON.stringify(next));
    setLiked(!liked);
  }

  function copySku() {
    if (!product.sku) return;
    navigator.clipboard.writeText(product.sku);
    setSkuCopied(true);
    setTimeout(() => setSkuCopied(false), 2000);
  }

  function openWhatsApp() {
    const lines = ["Hi! I want to order:", `Product: ${product.name}`];
    if (selectedColor) lines.push(`Color: ${selectedColor}`);
    if (selectedSize) lines.push(`Size: ${selectedSize}`);
    lines.push(`SKU: ${effectiveSku || "N/A"}`);
    lines.push(`Qty: ${qty}`);
    lines.push(`Total: ${formatPrice(total)}`);
    const msg = encodeURIComponent(lines.join("\n"));
    window.open(`https://wa.me/${business.whatsapp_number}?text=${msg}`, "_blank");
  }

  async function share() {
    if (navigator.share) {
      await navigator.share({ title: product.name, url: window.location.href });
    } else {
      navigator.clipboard.writeText(window.location.href);
      setToast("Link copied!");
      setTimeout(() => setToast(null), 2500);
    }
  }

  // ── Image area ───────────────────────────────────────────────────────────────

  const ImageArea = () => (
    <div className="relative w-full aspect-square md:aspect-[4/5] overflow-hidden bg-gray-50">
      {product.image_url ? (
        <img
          src={product.image_url}
          alt={name}
          className="w-full h-full object-cover"
          style={{ touchAction: "pinch-zoom" }}
        />
      ) : (
        // Branded placeholder card — no empty space
        <div
          className="w-full h-full flex flex-col items-center justify-center relative"
          style={{
            background: `linear-gradient(145deg, ${theme}28 0%, ${theme}10 60%, #f9fafb 100%)`,
          }}
        >
          {/* Decorative circles */}
          <div
            className="absolute -top-16 -right-16 w-64 h-64 rounded-full opacity-10"
            style={{ backgroundColor: theme }}
          />
          <div
            className="absolute -bottom-10 -left-10 w-48 h-48 rounded-full opacity-10"
            style={{ backgroundColor: theme }}
          />

          <span className="relative text-[96px] leading-none mb-4 select-none">
            {categoryEmoji(product.category)}
          </span>
          <p
            className="relative text-lg font-bold text-center px-8 leading-snug"
            style={{ color: theme }}
          >
            {name}
          </p>
          {product.category && (
            <p className="relative text-xs mt-2 font-medium" style={{ color: theme + "99" }}>
              {product.category}
            </p>
          )}

          {/* Business watermark */}
          <p className="absolute bottom-4 left-4 text-xs font-medium" style={{ color: theme + "66" }}>
            {business.name}
          </p>
        </div>
      )}
    </div>
  );

  // ── Product info content ──────────────────────────────────────────────────────

  const ProductInfo = () => (
    <motion.div
      initial={{ opacity: 0, y: 30 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, delay: 0.1 }}
      className="bg-white rounded-t-[28px] md:rounded-none -mt-6 md:mt-0 relative z-10 px-5 pt-5 pb-6 md:px-6 md:pt-6"
    >
      {/* Row 1: category + stock */}
      <div className="flex items-center gap-2 mb-3 flex-wrap">
        {product.category && (
          <span
            className="text-[11px] font-semibold px-3 py-1 rounded-full"
            style={{ backgroundColor: theme + "18", color: theme }}
          >
            {product.category}
          </span>
        )}
        <StockPill product={product} available={effectiveAvailable} stock={effectiveStock} />
      </div>

      {/* Row 2: Product name */}
      <h1 className="text-[22px] font-bold text-gray-900 leading-snug mb-3">{name}</h1>

      {/* Row 3: SKU */}
      {product.sku && (
        <button
          onClick={copySku}
          className="flex items-center gap-1.5 mb-3 group"
        >
          <span className="text-xs text-gray-400">Product Code:</span>
          <span className="font-mono text-[11px] text-gray-500 bg-gray-100 px-2 py-0.5 rounded">
            {product.sku}
          </span>
          {skuCopied
            ? <Check size={12} className="text-green-500" />
            : <Copy size={12} className="text-gray-300 group-hover:text-gray-500 transition-colors" />}
          <span className="text-[11px] text-gray-400">
            {skuCopied ? "Copied!" : "Copy"}
          </span>
        </button>
      )}

      {/* Row 4: Price */}
      <p className="text-[28px] font-bold mb-4" style={{ color: theme }}>
        {formatPrice(product.price)}
      </p>

      {/* Variant selector */}
      {product.has_variants && (
        <VariantSelector
          product={product}
          themeColor={theme}
          selectedColor={selectedColor}
          selectedSize={selectedSize}
          onColorChange={(c) => { setSelectedColor(c); setQty(1); }}
          onSizeChange={(s) => { setSelectedSize(s); setQty(1); }}
        />
      )}

      <div className="h-px bg-gray-100 mb-4" />

      {/* Row 5: Description */}
      {product.description && (
        <div className="mb-4">
          <h2 className="text-sm font-bold text-gray-800 mb-2">About this product</h2>
          <p className="text-sm text-gray-500 leading-relaxed">{product.description}</p>
        </div>
      )}

      {/* Row 6: Details table */}
      <div className="mb-6">
        <h2 className="text-sm font-bold text-gray-800 mb-3">Product Details</h2>
        <div className="rounded-xl overflow-hidden border border-gray-200">
          {[
            ["Product Code", product.sku || "—"],
            ["Category", product.category || "—"],
            ["Availability", product.is_available ? "✓ In Stock" : "Out of Stock"],
          ].map(([label, value], i) => (
            <div
              key={label}
              className={`flex items-center px-4 py-3 ${i % 2 === 0 ? "bg-white" : "bg-gray-50"}`}
            >
              <span className="text-[13px] text-gray-400 w-32 shrink-0">{label}</span>
              <span className="text-[13px] text-gray-800 font-medium">{value}</span>
            </div>
          ))}
        </div>
      </div>

      {/* Row 7: Related products */}
      <div className="mb-6">
        <RelatedProducts
          slug={slug!}
          currentProductId={product.id}
          category={product.category}
          themeColor={theme}
        />
      </div>

      {/* Desktop inline order card */}
      <div className="hidden md:block">
        <DesktopOrderCard
          qty={qty}
          total={total}
          themeColor={theme}
          maxQty={maxQty}
          available={effectiveAvailable}
          orderLabel={orderLabel}
          onQtyChange={setQty}
          onOrder={openWhatsApp}
          onNotify={() => setShowNotify(true)}
        />
      </div>
    </motion.div>
  );

  return (
    <div
      className="min-h-screen bg-gray-50"
      style={{ fontFamily: "'Inter', sans-serif" }}
    >
      {/* ── TOP NAV ─────────────────────────────────────────────────────────── */}
      <div className="sticky top-0 z-30 bg-white border-b border-gray-100 shadow-sm h-14 flex items-center px-4">
        <div className="w-full max-w-screen-lg mx-auto flex items-center justify-between">
          <Link
            to={`/shop/${slug}`}
            className="flex items-center gap-1.5 text-sm font-semibold shrink-0"
            style={{ color: theme }}
          >
            <ArrowLeft size={18} />
          </Link>

          {/* Business name — center, truncated */}
          <p
            className="text-[15px] font-bold text-gray-900 text-center truncate"
            style={{ maxWidth: 180 }}
          >
            {business.name}
          </p>

          <div className="flex items-center gap-1.5 shrink-0">
            <motion.button
              onClick={toggleLike}
              whileTap={{ scale: 1.35 }}
              transition={{ type: "spring", stiffness: 400, damping: 12 }}
              className="w-9 h-9 flex items-center justify-center rounded-full bg-gray-100"
            >
              <Heart
                size={16}
                className={liked ? "text-red-500 fill-red-500" : "text-gray-500"}
              />
            </motion.button>
            <button
              onClick={share}
              className="w-9 h-9 flex items-center justify-center rounded-full bg-gray-100"
            >
              <Share2 size={16} className="text-gray-500" />
            </button>
          </div>
        </div>
      </div>

      {/* ── MOBILE: single column ──────────────────────────────────────────── */}
      <div className="md:hidden">
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.3 }}
        >
          <ImageArea />
        </motion.div>
        {/* Extra padding-bottom so content clears sticky order bar (≈ 140px) */}
        <div className="pb-36">
          <ProductInfo />
        </div>
      </div>

      {/* ── DESKTOP: two column ───────────────────────────────────────────── */}
      <div className="hidden md:flex max-w-screen-lg mx-auto gap-0 items-start">
        {/* Left — sticky image */}
        <div className="w-1/2 sticky top-14 self-start">
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.3 }}
          >
            <ImageArea />
          </motion.div>
        </div>

        {/* Right — scrollable info */}
        <div className="w-1/2 overflow-y-auto">
          <ProductInfo />
        </div>
      </div>

      {/* ── MOBILE STICKY BOTTOM ORDER BAR ───────────────────────────────── */}
      <div className="md:hidden fixed bottom-0 left-0 right-0 bg-white border-t border-gray-200 shadow-[0_-4px_24px_rgba(0,0,0,0.08)] px-4 pt-3 pb-5 z-50">
        {effectiveAvailable ? (
          <>
            <div className="flex items-center justify-between mb-3">
              <QtySelector qty={qty} max={maxQty} onChange={setQty} />
              <div className="text-right">
                <p className="text-[11px] text-gray-400 mb-0.5">Total</p>
                <p className="text-xl font-bold" style={{ color: theme }}>
                  {formatPrice(total)}
                </p>
              </div>
            </div>

            <motion.button
              onClick={openWhatsApp}
              whileTap={{ scale: 0.97 }}
              className="w-full h-[50px] flex items-center justify-center gap-2 rounded-xl text-white font-bold text-[15px] hover:opacity-92 transition-opacity"
              style={{ backgroundColor: theme }}
              animate={{ boxShadow: ["0 0 0 0 " + theme + "44", "0 0 0 8px " + theme + "00"] }}
              transition={{ duration: 1.5, delay: 1, repeat: 1 }}
            >
              <ShoppingCart size={18} />
              {orderLabel} — {formatPrice(total)}
            </motion.button>
          </>
        ) : (
          <button
            onClick={() => setShowNotify(true)}
            className="w-full h-[50px] flex items-center justify-center gap-2 rounded-xl font-bold text-[15px] border-2 border-gray-200 text-gray-700 hover:bg-gray-50 transition-colors"
          >
            <Bell size={18} />
            Notify When Available
          </button>
        )}
      </div>

      {/* ── NOTIFY MODAL ─────────────────────────────────────────────────── */}
      <AnimatePresence>
        {showNotify && (
          <NotifyModal
            product={product}
            slug={slug!}
            themeColor={theme}
            onClose={() => setShowNotify(false)}
          />
        )}
      </AnimatePresence>

      {/* ── TOAST ────────────────────────────────────────────────────────── */}
      <AnimatePresence>
        {toast && (
          <motion.div
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 16 }}
            className="fixed bottom-28 left-1/2 -translate-x-1/2 bg-gray-900 text-white text-sm px-4 py-2 rounded-full shadow-lg z-50 whitespace-nowrap md:bottom-6"
          >
            {toast}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
