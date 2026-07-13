import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import {
  Search, Heart, ShoppingCart, Bell, Share2,
  MessageCircle, ChevronDown, X, LayoutGrid, List,
  SlidersHorizontal, ArrowUp, Filter, BadgeCheck, Truck, PackageCheck,
} from "lucide-react";
import { publicApi, type BusinessInfo, type PublicProduct } from "../../api/publicApi";
import { ColorDots, SizePills } from "../../utils/variants";

// ── Helpers ───────────────────────────────────────────────────────────────────

function toTitleCase(str: string): string {
  return str.replace(/\b\w/g, (c) => c.toUpperCase());
}

const CATEGORY_EMOJI: Record<string, string> = {
  saree: "🥻", lehenga: "👗", kurti: "👕",
  dress: "👗", suit: "👔", shirt: "👕",
  dupatta: "🧣", shoes: "👟", shoe: "👟",
  bag: "👜", bags: "👜", jewellery: "💍", jewelry: "💍",
  other: "📦",
};

function categoryEmoji(category?: string | null): string {
  return CATEGORY_EMOJI[category?.toLowerCase() ?? ""] ?? "📦";
}

function formatPrice(n: number): string {
  return "₹" + n.toLocaleString("en-IN");
}

// Lighten/darken helper for gradient accents built off the brand color.
function shade(hex: string, percent: number): string {
  const n = parseInt(hex.replace("#", ""), 16);
  const r = Math.min(255, Math.max(0, ((n >> 16) & 255) + percent));
  const g = Math.min(255, Math.max(0, ((n >> 8) & 255) + percent));
  const b = Math.min(255, Math.max(0, (n & 255) + percent));
  return `#${((1 << 24) + (r << 16) + (g << 8) + b).toString(16).slice(1)}`;
}

// ── Skeleton ──────────────────────────────────────────────────────────────────

function SkeletonCard() {
  const shimmer = "relative overflow-hidden bg-gray-200 before:absolute before:inset-0 before:-translate-x-full before:animate-[shimmer_1.6s_infinite] before:bg-gradient-to-r before:from-transparent before:via-white/60 before:to-transparent";
  return (
    <div>
      <div className={`${shimmer} rounded-3xl w-full mb-3`} style={{ aspectRatio: "3/4" }} />
      <div className={`${shimmer} h-3 rounded-full w-1/3 mb-2`} />
      <div className={`${shimmer} h-4 rounded-full w-3/4 mb-2`} />
      <div className={`${shimmer} h-5 rounded-full w-1/2 mb-3`} />
      <div className={`${shimmer} h-11 rounded-2xl w-full`} />
    </div>
  );
}

// ── Product Card ──────────────────────────────────────────────────────────────

function ProductCard({
  product, business, slug, themeColor, onNotify, view,
}: {
  product: PublicProduct;
  business: BusinessInfo;
  slug: string;
  themeColor: string;
  onNotify: (p: PublicProduct) => void;
  view: "grid" | "list";
}) {
  const [liked, setLiked] = useState(() => {
    try {
      const faves: string[] = JSON.parse(localStorage.getItem(`faves_${slug}`) || "[]");
      return faves.includes(String(product.id));
    } catch { return false; }
  });
  const [imgError, setImgError] = useState(false);

  function toggleLike(e: React.MouseEvent) {
    e.preventDefault(); e.stopPropagation();
    const key = `faves_${slug}`;
    const faves: string[] = JSON.parse(localStorage.getItem(key) || "[]");
    const id = String(product.id);
    const next = liked ? faves.filter((f) => f !== id) : [...faves, id];
    localStorage.setItem(key, JSON.stringify(next));
    setLiked(!liked);
  }

  function openWhatsApp(e: React.MouseEvent) {
    e.preventDefault(); e.stopPropagation();
    const msg = encodeURIComponent(
      `Hi! 👋 I want to order from your catalogue:\n\n🛍️ *${product.name}*\n📦 SKU: ${product.sku || "N/A"}\n🔢 Quantity: 1\n💰 Total: ${formatPrice(product.price)}\n\nPlease confirm my order!`
    );
    window.open(`https://wa.me/${business.whatsapp_number}?text=${msg}`, "_blank");
  }

  const outOfStock = !product.is_available;
  const lowStock = !outOfStock && product.stock !== null && product.stock <= product.low_stock_alert;
  const hasImage = !!product.image_url && !imgError;
  const name = toTitleCase(product.name);

  const placeholder = (
    <div
      className="w-full h-full flex flex-col items-center justify-center gap-1.5"
      style={{ background: `linear-gradient(135deg, ${themeColor}14 0%, ${themeColor}26 100%)` }}
    >
      <span className="text-3xl drop-shadow-sm">{categoryEmoji(product.category)}</span>
      <span className="text-[10px] font-medium tracking-wide uppercase" style={{ color: themeColor }}>
        {product.category || "Product"}
      </span>
    </div>
  );

  if (view === "list") {
    return (
      <motion.div
        layout
        className="bg-white rounded-3xl border border-gray-100 flex gap-3 p-3 hover:shadow-lg hover:border-gray-200 transition-all duration-200"
        whileHover={{ y: -1 }}
      >
        <Link
          to={`/shop/${slug}/product/${product.sku || product.id}`}
          className="shrink-0 w-24 h-24 rounded-2xl overflow-hidden bg-gray-50 flex items-center justify-center relative"
        >
          {hasImage ? (
            <img src={product.image_url!} alt={name} className="w-full h-full object-cover" onError={() => setImgError(true)} />
          ) : (
            placeholder
          )}
          {outOfStock && (
            <div className="absolute inset-0 bg-black/45 flex items-center justify-center rounded-2xl">
              <span className="text-white text-[9px] font-bold text-center px-1 tracking-wide">SOLD OUT</span>
            </div>
          )}
        </Link>

        <div className="flex flex-col flex-1 min-w-0">
          {product.sku && (
            <span className="font-mono text-[10px] text-gray-400 bg-gray-50 border border-gray-100 rounded-md px-1.5 py-0.5 self-start mb-1">{product.sku}</span>
          )}
          <Link
            to={`/shop/${slug}/product/${product.sku || product.id}`}
            className="text-sm font-semibold text-gray-900 leading-snug line-clamp-2 hover:opacity-80 mb-1"
          >
            {name}
          </Link>
          <p className="text-base font-bold mb-0.5" style={{ color: themeColor }}>{formatPrice(product.price)}</p>
          <p className="text-[11px] text-gray-400 mb-1 flex items-center gap-1"><Truck size={11} /> {product.delivery_time}</p>
          <div className="flex items-center gap-1.5 mb-1.5">
            <div className={`w-1.5 h-1.5 rounded-full shrink-0 ${outOfStock ? "bg-red-500" : lowStock ? "bg-amber-500" : "bg-emerald-500"}`} />
            <span className="text-[11px] text-gray-500">
              {outOfStock ? "Out of stock" : lowStock ? `Only ${product.stock} left` : "In stock"}
            </span>
          </div>
          {product.has_variants && (
            <div className="flex flex-col gap-1">
              <ColorDots colors={product.available_colors} size={14} />
              <SizePills variants={product.variants} sizes={product.available_sizes} />
            </div>
          )}
        </div>

        <div className="shrink-0 flex flex-col justify-center">
          {product.is_available ? (
            <button
              onClick={openWhatsApp}
              className="flex items-center gap-1 px-3.5 py-2.5 rounded-2xl text-white text-xs font-semibold whitespace-nowrap shadow-sm hover:opacity-90 active:scale-95 transition-all"
              style={{ backgroundColor: themeColor }}
            >
              <ShoppingCart size={13} />
              Order
            </button>
          ) : (
            <button
              onClick={(e) => { e.preventDefault(); onNotify(product); }}
              className="flex items-center gap-1 px-3.5 py-2.5 rounded-2xl text-gray-600 text-xs font-semibold border border-gray-200 whitespace-nowrap hover:bg-gray-50"
            >
              <Bell size={13} />
              Notify
            </button>
          )}
        </div>
      </motion.div>
    );
  }

  return (
    <motion.div
      layout
      className="group bg-white rounded-2xl border border-gray-100 overflow-hidden flex flex-col hover:border-gray-200"
      whileHover={{ y: -3, boxShadow: "0 12px 28px rgba(0,0,0,0.09)" }}
      transition={{ duration: 0.2 }}
    >
      <Link
        to={`/shop/${slug}/product/${product.sku || product.id}`}
        className="block relative overflow-hidden bg-gray-50"
        style={{ aspectRatio: "1/1" }}
      >
        {hasImage ? (
          <img
            src={product.image_url!}
            alt={name}
            className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500"
            loading="lazy"
            onError={() => setImgError(true)}
          />
        ) : (
          placeholder
        )}

        {outOfStock && (
          <div className="absolute inset-0 bg-gray-900/45 backdrop-blur-[1px] flex items-end p-2">
            <span className="bg-white text-gray-900 text-[9px] font-bold px-2 py-0.5 rounded-full w-full text-center tracking-wide shadow-sm">
              OUT OF STOCK
            </span>
          </div>
        )}

        {product.category && (
          <span className="absolute top-2 left-2 bg-white/95 backdrop-blur-sm text-gray-700 text-[9px] font-semibold px-2 py-0.5 rounded-full shadow-sm capitalize">
            {product.category}
          </span>
        )}

        {lowStock && (
          <motion.span
            animate={product.stock! <= 3 ? { opacity: [1, 0.55, 1] } : {}}
            transition={{ repeat: Infinity, duration: 1.2 }}
            className="absolute bottom-2 left-2 bg-amber-500 text-white text-[9px] font-bold px-2 py-0.5 rounded-full shadow-sm"
          >
            Only {product.stock} left!
          </motion.span>
        )}

        <motion.button
          onClick={toggleLike}
          whileTap={{ scale: 1.4 }}
          transition={{ type: "spring", stiffness: 400, damping: 10 }}
          className="absolute top-2 right-2 w-7 h-7 bg-white/95 backdrop-blur-sm rounded-full flex items-center justify-center shadow-md"
        >
          <Heart size={12} className={liked ? "text-red-500 fill-red-500" : "text-gray-400"} />
        </motion.button>
      </Link>

      <div className="p-2.5 flex flex-col flex-1">
        {product.sku && (
          <span className="font-mono text-[9px] text-gray-400 bg-gray-50 border border-gray-100 rounded-md px-1.5 py-0.5 self-start mb-1">{product.sku}</span>
        )}
        <Link
          to={`/shop/${slug}/product/${product.sku || product.id}`}
          className="text-[13px] font-semibold text-gray-900 leading-snug line-clamp-2 hover:opacity-80 mb-1"
        >
          {name}
        </Link>
        <p className="text-[15px] font-bold mb-0.5 tracking-tight" style={{ color: themeColor }}>{formatPrice(product.price)}</p>

        <p className="text-[10px] text-gray-400 mb-1 flex items-center gap-1"><Truck size={10} /> {product.delivery_time}</p>

        <div className="flex items-center gap-1.5 mb-2">
          <div className={`w-1.5 h-1.5 rounded-full shrink-0 ${outOfStock ? "bg-red-500" : lowStock ? "bg-amber-500" : "bg-emerald-500"}`} />
          <span className="text-[10px] text-gray-500">
            {outOfStock ? "Out of stock" : lowStock ? `Only ${product.stock} left` : "In stock"}
          </span>
        </div>

        {product.has_variants && (
          <div className="flex flex-col gap-1 mb-2">
            <ColorDots colors={product.available_colors} size={12} />
            <SizePills variants={product.variants} sizes={product.available_sizes} />
          </div>
        )}

        <div className="mt-auto">
          {product.is_available ? (
            <motion.button
              onClick={openWhatsApp}
              whileTap={{ scale: 0.97 }}
              className="w-full flex items-center justify-center gap-1.5 py-2 rounded-xl text-white text-xs font-semibold shadow-sm hover:opacity-90 transition-opacity"
              style={{ backgroundColor: themeColor }}
            >
              <ShoppingCart size={12} />
              Order on WhatsApp
            </motion.button>
          ) : (
            <button
              onClick={() => onNotify(product)}
              className="w-full flex items-center justify-center gap-1.5 py-2 rounded-xl text-gray-600 text-xs font-semibold border border-gray-200 hover:bg-gray-50 transition-colors"
            >
              <Bell size={12} />
              Notify When Available
            </button>
          )}
        </div>
      </div>
    </motion.div>
  );
}

// ── Filter Panel ──────────────────────────────────────────────────────────────

function FilterPanel({
  products, priceMin, priceMax, setPriceMin, setPriceMax,
  inStockOnly, setInStockOnly, showOutOfStock, setShowOutOfStock,
  themeColor, onClear, onClose,
}: {
  products: PublicProduct[];
  priceMin: number; priceMax: number;
  setPriceMin: (v: number) => void; setPriceMax: (v: number) => void;
  inStockOnly: boolean; setInStockOnly: (v: boolean) => void;
  showOutOfStock: boolean; setShowOutOfStock: (v: boolean) => void;
  themeColor: string;
  onClear: () => void; onClose: () => void;
}) {
  const absMin = Math.min(...products.map((p) => p.price));
  const absMax = Math.max(...products.map((p) => p.price));

  return (
    <motion.div
      initial={{ opacity: 0, y: -6, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -6, scale: 0.98 }}
      transition={{ duration: 0.15 }}
      className="absolute right-0 top-full mt-2 w-72 bg-white rounded-2xl shadow-xl border border-gray-100 z-50 p-4"
    >
      <div className="flex items-center justify-between mb-4">
        <h4 className="font-semibold text-gray-900 text-sm">Filters</h4>
        <button onClick={onClose} className="w-6 h-6 rounded-full hover:bg-gray-100 flex items-center justify-center">
          <X size={14} className="text-gray-400" />
        </button>
      </div>

      <div className="mb-4">
        <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide mb-3">Price Range</p>
        <div className="flex items-center gap-2">
          <div className="flex-1">
            <label className="text-[11px] text-gray-400 mb-1 block">Min ₹</label>
            <input
              type="number" value={priceMin} min={absMin} max={priceMax - 1}
              onChange={(e) => setPriceMin(Number(e.target.value))}
              className="w-full border border-gray-200 rounded-xl px-2.5 py-1.5 text-sm focus:outline-none focus:ring-2"
              style={{ "--tw-ring-color": themeColor } as React.CSSProperties}
            />
          </div>
          <span className="text-gray-300 mt-4">—</span>
          <div className="flex-1">
            <label className="text-[11px] text-gray-400 mb-1 block">Max ₹</label>
            <input
              type="number" value={priceMax} min={priceMin + 1} max={absMax}
              onChange={(e) => setPriceMax(Number(e.target.value))}
              className="w-full border border-gray-200 rounded-xl px-2.5 py-1.5 text-sm focus:outline-none focus:ring-2"
              style={{ "--tw-ring-color": themeColor } as React.CSSProperties}
            />
          </div>
        </div>
      </div>

      <div className="mb-4">
        <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide mb-3">Availability</p>
        <label className="flex items-center gap-2 cursor-pointer mb-2.5">
          <input type="checkbox" checked={inStockOnly} onChange={(e) => setInStockOnly(e.target.checked)} className="rounded accent-brand-primary" />
          <span className="text-sm text-gray-700">In stock only</span>
        </label>
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="checkbox" checked={showOutOfStock} onChange={(e) => setShowOutOfStock(e.target.checked)} className="rounded accent-brand-primary" />
          <span className="text-sm text-gray-700">Show out of stock</span>
        </label>
      </div>

      <div className="flex items-center gap-2 pt-3 border-t border-gray-100">
        <button onClick={onClear} className="flex-1 py-2 text-sm text-gray-500 hover:text-gray-700">Clear all</button>
        <button
          onClick={onClose}
          className="flex-1 py-2 rounded-xl text-white text-sm font-semibold shadow-sm"
          style={{ backgroundColor: themeColor }}
        >
          Apply
        </button>
      </div>
    </motion.div>
  );
}

// ── Notify Modal ──────────────────────────────────────────────────────────────

function NotifyModal({ product, slug, themeColor, onClose }: {
  product: PublicProduct; slug: string; themeColor: string; onClose: () => void;
}) {
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
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/50 backdrop-blur-sm" onClick={onClose}>
      <motion.div
        initial={{ y: "100%", opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        exit={{ y: "100%", opacity: 0 }}
        transition={{ type: "spring", damping: 30, stiffness: 300 }}
        onClick={(e) => e.stopPropagation()}
        className="bg-white w-full max-w-lg rounded-t-[28px] sm:rounded-[28px] p-6 pb-10 sm:pb-8 shadow-2xl"
      >
        <div className="w-10 h-1 bg-gray-300 rounded-full mx-auto mb-5 sm:hidden" />

        {done ? (
          <div className="text-center py-4">
            <div className="w-16 h-16 rounded-2xl flex items-center justify-center mx-auto mb-4" style={{ backgroundColor: themeColor + "18" }}>
              <Bell size={28} style={{ color: themeColor }} />
            </div>
            <h3 className="text-lg font-bold text-gray-900 mb-1">You're on the list!</h3>
            <p className="text-sm text-gray-500 mb-5">We'll WhatsApp you when <strong>{toTitleCase(product.name)}</strong> is back.</p>
            <button onClick={onClose} className="text-sm font-semibold" style={{ color: themeColor }}>Done</button>
          </div>
        ) : (
          <>
            <div className="flex items-center justify-between mb-1">
              <h3 className="text-lg font-bold text-gray-900">Notify me when available</h3>
              <button onClick={onClose} className="w-8 h-8 flex items-center justify-center rounded-full bg-gray-100 hover:bg-gray-200 transition-colors">
                <X size={16} className="text-gray-500" />
              </button>
            </div>
            <p className="text-sm text-gray-500 mb-5">
              We'll WhatsApp you when <strong>{toTitleCase(product.name)}</strong> is back in stock.
            </p>
            <form onSubmit={submit} className="flex flex-col gap-3">
              <div
                className="flex items-center border border-gray-200 rounded-2xl overflow-hidden focus-within:ring-2"
                style={{ "--tw-ring-color": themeColor } as React.CSSProperties}
              >
                <span className="px-3 py-3 text-gray-500 text-sm bg-gray-50 border-r border-gray-200">+91</span>
                <input
                  type="tel" value={phone} onChange={(e) => setPhone(e.target.value)}
                  placeholder="Enter your WhatsApp number"
                  required
                  className="flex-1 px-3 py-3 text-sm focus:outline-none"
                />
              </div>
              <motion.button
                type="submit" disabled={loading}
                whileTap={{ scale: 0.98 }}
                className="py-3.5 rounded-2xl text-white font-semibold text-sm disabled:opacity-60 flex items-center justify-center gap-2 shadow-sm"
                style={{ backgroundColor: themeColor }}
              >
                <Bell size={16} />
                {loading ? "Saving…" : "Notify Me"}
              </motion.button>
              <button type="button" onClick={onClose} className="text-sm text-gray-400 text-center">Cancel</button>
            </form>
          </>
        )}
      </motion.div>
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function CataloguePage() {
  const { slug } = useParams<{ slug: string }>();
  const [data, setData] = useState<{ business: BusinessInfo; products: PublicProduct[]; categories: string[] } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const [search, setSearch] = useState("");
  const [activeCategory, setActiveCategory] = useState<string | null>(null);
  const [sort, setSort] = useState<"default" | "price_asc" | "price_desc" | "name_az" | "in_stock">("default");
  const [view, setView] = useState<"grid" | "list">("grid");
  const [showFilter, setShowFilter] = useState(false);
  const [showSortMenu, setShowSortMenu] = useState(false);

  const [priceMin, setPriceMin] = useState(0);
  const [priceMax, setPriceMax] = useState(999999);
  const [inStockOnly, setInStockOnly] = useState(false);
  const [showOutOfStock, setShowOutOfStock] = useState(true);

  const [notifyProduct, setNotifyProduct] = useState<PublicProduct | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [scrollY, setScrollY] = useState(0);

  useEffect(() => {
    const onScroll = () => setScrollY(window.scrollY);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    if (!slug) return;
    publicApi
      .getCatalogue(slug)
      .then((d) => {
        setData(d);
        setLoading(false);
        if (d.products.length > 0) {
          const prices = d.products.map((p) => p.price);
          setPriceMin(Math.min(...prices));
          setPriceMax(Math.max(...prices));
        }
      })
      .catch(() => { setError(true); setLoading(false); });
  }, [slug]);

  useEffect(() => {
    if (data) {
      document.title = `${data.business.name} — ${data.business.tagline || "Online Catalogue"}`;
    }
  }, [data]);

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(null), 2500);
  }

  if (loading) {
    return (
      <div className="min-h-screen bg-gray-50">
        <div className="h-[280px] bg-gray-200 animate-pulse" />
        <div className="max-w-screen-xl mx-auto px-4 py-6">
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-5">
            {Array.from({ length: 8 }).map((_, i) => <SkeletonCard key={i} />)}
          </div>
        </div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <div className="text-center">
          <p className="text-6xl mb-4">🔍</p>
          <h1 className="text-xl font-bold text-gray-700 mb-2">Catalogue not found</h1>
          <p className="text-gray-400 text-sm">This link may be expired or incorrect.</p>
        </div>
      </div>
    );
  }

  const { business, categories } = data;
  const theme = business.theme_color || "#6366F1";
  const themeDark = shade(theme, -35);

  const categoryCounts = data.products.reduce<Record<string, number>>((acc, p) => {
    if (p.category) acc[p.category] = (acc[p.category] ?? 0) + 1;
    return acc;
  }, {});

  let products = data.products.filter((p) => {
    const q = search.toLowerCase();
    if (q && !`${p.name} ${p.sku || ""} ${p.description || ""} ${p.category || ""}`.toLowerCase().includes(q)) return false;
    if (activeCategory && p.category !== activeCategory) return false;
    if (p.price < priceMin || p.price > priceMax) return false;
    if (inStockOnly && !p.is_available) return false;
    if (!showOutOfStock && !p.is_available) return false;
    return true;
  });

  if (sort === "price_asc") products = [...products].sort((a, b) => a.price - b.price);
  else if (sort === "price_desc") products = [...products].sort((a, b) => b.price - a.price);
  else if (sort === "name_az") products = [...products].sort((a, b) => a.name.localeCompare(b.name));
  else if (sort === "in_stock") products = [...products].sort((a, b) => (b.is_available ? 1 : 0) - (a.is_available ? 1 : 0));

  function clearFilters() {
    const prices = data!.products.map((p) => p.price);
    setPriceMin(Math.min(...prices));
    setPriceMax(Math.max(...prices));
    setInStockOnly(false);
    setShowOutOfStock(true);
  }

  const hasActiveFilters = inStockOnly || !showOutOfStock;
  const isFiltered = products.length < data.products.length;

  const initials = business.name.split(" ").map((w) => w[0]).join("").slice(0, 2).toUpperCase();

  const sortLabels: Record<typeof sort, string> = {
    default: "Newest first",
    price_asc: "Price: Low → High",
    price_desc: "Price: High → Low",
    name_az: "Name: A → Z",
    in_stock: "In Stock first",
  };

  return (
    <div className="min-h-screen bg-[#FAFAFA]" style={{ fontFamily: "'Inter', sans-serif" }}>

      {/* ── HERO ──────────────────────────────────────────────────────────── */}
      <div
        className="relative w-full overflow-hidden"
        style={{
          minHeight: 280,
          background: business.banner_url
            ? `linear-gradient(rgba(10,10,10,0.55), rgba(10,10,10,0.72)), url(${business.banner_url}) center/cover no-repeat`
            : `radial-gradient(circle at 15% 15%, ${theme} 0%, ${themeDark} 75%)`,
        }}
      >
        {/* decorative mesh accent */}
        {!business.banner_url && (
          <div
            className="absolute inset-0 opacity-40 mix-blend-overlay"
            style={{
              backgroundImage: `radial-gradient(circle at 85% 20%, white 0%, transparent 35%), radial-gradient(circle at 20% 90%, white 0%, transparent 30%)`,
            }}
          />
        )}

        <div className="relative max-w-screen-xl mx-auto px-4 sm:px-6 pt-8 pb-6 flex flex-col">
          <div className="flex items-start gap-4 mb-5">
            <div className="w-16 h-16 sm:w-20 sm:h-20 rounded-2xl border-4 border-white/90 shadow-xl flex items-center justify-center bg-white overflow-hidden shrink-0">
              {business.logo_url ? (
                <img src={business.logo_url} alt="logo" className="w-full h-full object-cover" />
              ) : (
                <span className="text-xl sm:text-2xl font-bold" style={{ color: theme }}>{initials}</span>
              )}
            </div>
            <div className="min-w-0 pt-1">
              <h1 className="text-white text-2xl sm:text-3xl font-bold tracking-tight drop-shadow-sm mb-1 truncate">{business.name}</h1>
              {business.tagline && (
                <p className="text-white/85 text-sm sm:text-base">{business.tagline}</p>
              )}
            </div>
          </div>

          <div className="flex flex-wrap gap-2 mb-6">
            <span className="inline-flex items-center gap-1.5 bg-white/15 backdrop-blur-md border border-white/20 text-white text-xs font-medium px-3 py-1.5 rounded-full">
              <PackageCheck size={13} /> {data.products.length} Products
            </span>
            <span className="inline-flex items-center gap-1.5 bg-white/15 backdrop-blur-md border border-white/20 text-white text-xs font-medium px-3 py-1.5 rounded-full">
              <BadgeCheck size={13} /> Verified Seller
            </span>
            <span className="inline-flex items-center gap-1.5 bg-white/15 backdrop-blur-md border border-white/20 text-white text-xs font-medium px-3 py-1.5 rounded-full">
              <Truck size={13} />
              {business.delivery_days_min && business.delivery_days_max
                ? `Delivery in ${business.delivery_days_min}-${business.delivery_days_max} days`
                : "Fast Delivery"}
            </span>
          </div>

          <div className="flex gap-2.5 flex-wrap">
            {business.whatsapp_number && (
              <a
                href={`https://wa.me/${business.whatsapp_number}?text=${encodeURIComponent(`Hi ${business.name}! I visited your catalogue.`)}`}
                target="_blank" rel="noreferrer"
                className="inline-flex items-center gap-1.5 bg-white text-gray-900 text-xs font-semibold px-4 py-2.5 rounded-full shadow-lg hover:bg-gray-50 hover:scale-[1.02] transition-all"
              >
                <MessageCircle size={14} className="text-green-500" />
                Chat on WhatsApp
              </a>
            )}
            {business.instagram_id && (
              <a
                href={`https://instagram.com/${business.instagram_id}`}
                target="_blank" rel="noreferrer"
                className="inline-flex items-center gap-1.5 bg-white/10 backdrop-blur-md border border-white/30 text-white text-xs font-semibold px-4 py-2.5 rounded-full hover:bg-white/20 transition-colors"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-pink-200">
                  <rect x="2" y="2" width="20" height="20" rx="5" /><path d="M16 11.37A4 4 0 1 1 12.63 8 4 4 0 0 1 16 11.37z" /><line x1="17.5" y1="6.5" x2="17.51" y2="6.5" />
                </svg>
                Instagram
              </a>
            )}
          </div>
        </div>

        {/* soft curve into page */}
        <div className="absolute -bottom-1 left-0 right-0 h-6 bg-[#FAFAFA] rounded-t-[28px]" />
      </div>

      {/* ── STICKY SEARCH + FILTER ───────────────────────────────────────── */}
      <div
        className={`sticky top-0 z-30 bg-[#FAFAFA]/90 backdrop-blur-md border-b transition-shadow duration-200 ${
          scrollY > 40 ? "border-gray-200 shadow-sm" : "border-gray-100"
        }`}
      >
        <div className="max-w-screen-xl mx-auto px-4 sm:px-6 py-3 flex flex-col gap-2.5">
          <div className="relative">
            <Search size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search products, SKU code..."
              className="w-full bg-white border border-gray-200 rounded-2xl pl-11 pr-10 py-3 text-sm shadow-sm focus:outline-none focus:ring-2 focus:border-transparent transition-all"
              style={{ "--tw-ring-color": theme } as React.CSSProperties}
            />
            {search && (
              <button
                onClick={() => setSearch("")}
                className="absolute right-3.5 top-1/2 -translate-y-1/2 w-5 h-5 flex items-center justify-center bg-gray-200 rounded-full text-gray-600 hover:bg-gray-300 transition-colors"
              >
                <X size={12} />
              </button>
            )}
          </div>

          <div className="flex items-center gap-2">
            {categories.length > 0 && (
              <div className="flex-1 flex gap-2 overflow-x-auto pb-0.5 scrollbar-hide min-w-0">
                <button
                  onClick={() => setActiveCategory(null)}
                  className="shrink-0 px-3.5 py-1.5 rounded-full text-xs font-semibold border transition-all duration-200"
                  style={
                    activeCategory === null
                      ? { backgroundColor: theme, color: "white", borderColor: theme, boxShadow: `0 4px 12px ${theme}40` }
                      : { backgroundColor: "white", color: "#666", borderColor: "#e5e7eb" }
                  }
                >
                  All ({data.products.length})
                </button>
                {categories.map((cat) => (
                  <button
                    key={cat}
                    onClick={() => setActiveCategory(activeCategory === cat ? null : cat)}
                    className="shrink-0 inline-flex items-center gap-1 px-3.5 py-1.5 rounded-full text-xs font-semibold border capitalize transition-all duration-200"
                    style={
                      activeCategory === cat
                        ? { backgroundColor: theme, color: "white", borderColor: theme, boxShadow: `0 4px 12px ${theme}40` }
                        : { backgroundColor: "white", color: "#666", borderColor: "#e5e7eb" }
                    }
                  >
                    <span>{categoryEmoji(cat)}</span>
                    {cat} ({categoryCounts[cat] ?? 0})
                  </button>
                ))}
              </div>
            )}

            <div className="flex gap-1.5 shrink-0">
              <div className="relative">
                <button
                  onClick={() => { setShowSortMenu(!showSortMenu); setShowFilter(false); }}
                  className="flex items-center gap-1 text-xs font-semibold border border-gray-200 rounded-xl px-3 py-2 text-gray-600 bg-white hover:bg-gray-50 shadow-sm transition-colors"
                >
                  <SlidersHorizontal size={12} />
                  <span className="hidden sm:inline">Sort</span>
                  <ChevronDown size={10} />
                </button>
                <AnimatePresence>
                  {showSortMenu && (
                    <motion.div
                      initial={{ opacity: 0, y: -6, scale: 0.98 }}
                      animate={{ opacity: 1, y: 0, scale: 1 }}
                      exit={{ opacity: 0, y: -6, scale: 0.98 }}
                      transition={{ duration: 0.15 }}
                      className="absolute right-0 top-full mt-2 w-48 bg-white rounded-2xl shadow-xl border border-gray-100 z-50 py-1 overflow-hidden"
                    >
                      {(["default", "price_asc", "price_desc", "name_az", "in_stock"] as const).map((s) => (
                        <button
                          key={s}
                          onClick={() => { setSort(s); setShowSortMenu(false); }}
                          className="w-full text-left px-4 py-2.5 text-sm hover:bg-gray-50 flex items-center justify-between"
                        >
                          {sortLabels[s]}
                          {sort === s && <span style={{ color: theme }}>✓</span>}
                        </button>
                      ))}
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>

              <div className="relative">
                <button
                  onClick={() => { setShowFilter(!showFilter); setShowSortMenu(false); }}
                  className={`flex items-center gap-1 text-xs font-semibold border rounded-xl px-3 py-2 shadow-sm transition-colors ${
                    hasActiveFilters ? "text-white border-transparent" : "border-gray-200 text-gray-600 bg-white hover:bg-gray-50"
                  }`}
                  style={hasActiveFilters ? { backgroundColor: theme } : {}}
                >
                  <Filter size={12} />
                  <span className="hidden sm:inline">Filter</span>
                  {hasActiveFilters && <span className="w-1.5 h-1.5 bg-white rounded-full ml-0.5" />}
                </button>
                <AnimatePresence>
                  {showFilter && (
                    <FilterPanel
                      products={data.products}
                      priceMin={priceMin} priceMax={priceMax}
                      setPriceMin={setPriceMin} setPriceMax={setPriceMax}
                      inStockOnly={inStockOnly} setInStockOnly={setInStockOnly}
                      showOutOfStock={showOutOfStock} setShowOutOfStock={setShowOutOfStock}
                      themeColor={theme}
                      onClear={clearFilters}
                      onClose={() => setShowFilter(false)}
                    />
                  )}
                </AnimatePresence>
              </div>

              <div className="hidden sm:flex items-center gap-1 bg-white border border-gray-200 rounded-xl p-1 shadow-sm">
                <button
                  onClick={() => setView("grid")}
                  className="w-7 h-7 flex items-center justify-center rounded-lg transition-colors"
                  style={view === "grid" ? { backgroundColor: theme, color: "white" } : { color: "#9ca3af" }}
                >
                  <LayoutGrid size={14} />
                </button>
                <button
                  onClick={() => setView("list")}
                  className="w-7 h-7 flex items-center justify-center rounded-lg transition-colors"
                  style={view === "list" ? { backgroundColor: theme, color: "white" } : { color: "#9ca3af" }}
                >
                  <List size={14} />
                </button>
              </div>
            </div>
          </div>
        </div>

        {(showSortMenu || showFilter) && (
          <div className="fixed inset-0 z-40" onClick={() => { setShowSortMenu(false); setShowFilter(false); }} />
        )}
      </div>

      {/* ── RESULTS BAR ───────────────────────────────────────────────────── */}
      <div className="max-w-screen-xl mx-auto px-4 sm:px-6 pt-4 pb-2 flex items-center justify-between">
        <span className="text-xs font-medium text-gray-500">
          {search
            ? `Results for "${search}"`
            : isFiltered
            ? `Showing ${products.length} of ${data.products.length} products`
            : `${products.length} products`}
        </span>
        <div className="flex sm:hidden items-center gap-1">
          <button
            onClick={() => setView("grid")}
            className="w-7 h-7 flex items-center justify-center rounded-lg transition-colors"
            style={view === "grid" ? { backgroundColor: theme, color: "white" } : { color: "#9ca3af" }}
          >
            <LayoutGrid size={14} />
          </button>
          <button
            onClick={() => setView("list")}
            className="w-7 h-7 flex items-center justify-center rounded-lg transition-colors"
            style={view === "list" ? { backgroundColor: theme, color: "white" } : { color: "#9ca3af" }}
          >
            <List size={14} />
          </button>
        </div>
      </div>

      {/* ── PRODUCT GRID / LIST ───────────────────────────────────────────── */}
      <div className="max-w-screen-xl mx-auto px-4 sm:px-6 pb-24">
        {products.length === 0 ? (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="flex flex-col items-center justify-center py-24 text-center"
          >
            <div
              className="w-20 h-20 rounded-3xl flex items-center justify-center text-4xl mb-5"
              style={{ background: `linear-gradient(135deg, ${theme}14 0%, ${theme}26 100%)` }}
            >
              {search || activeCategory ? "🔍" : "📦"}
            </div>
            <p className="text-gray-700 font-semibold text-lg mb-1">
              {search || activeCategory ? "No products found" : "No products yet"}
            </p>
            <p className="text-gray-400 text-sm mb-5">
              {search ? "Try searching for something else" : activeCategory ? "Try a different category" : "Check back soon!"}
            </p>
            {(search || activeCategory) && (
              <button
                onClick={() => { setSearch(""); setActiveCategory(null); }}
                className="text-sm font-semibold px-5 py-2.5 rounded-full border-2 hover:opacity-90"
                style={{ color: theme, borderColor: theme }}
              >
                Clear filters
              </button>
            )}
          </motion.div>
        ) : view === "grid" ? (
          <motion.div
            className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3 sm:gap-4"
            initial="hidden"
            animate="show"
            variants={{ show: { transition: { staggerChildren: 0.05 } } }}
          >
            {products.map((product) => (
              <motion.div
                key={product.id}
                variants={{ hidden: { opacity: 0, y: 20 }, show: { opacity: 1, y: 0, transition: { duration: 0.3 } } }}
                layout
              >
                <ProductCard
                  product={product} business={business} slug={slug!}
                  themeColor={theme} onNotify={setNotifyProduct} view="grid"
                />
              </motion.div>
            ))}
          </motion.div>
        ) : (
          <motion.div
            className="flex flex-col gap-3"
            initial="hidden"
            animate="show"
            variants={{ show: { transition: { staggerChildren: 0.04 } } }}
          >
            {products.map((product) => (
              <motion.div
                key={product.id}
                variants={{ hidden: { opacity: 0, x: -20 }, show: { opacity: 1, x: 0, transition: { duration: 0.3 } } }}
                layout
              >
                <ProductCard
                  product={product} business={business} slug={slug!}
                  themeColor={theme} onNotify={setNotifyProduct} view="list"
                />
              </motion.div>
            ))}
          </motion.div>
        )}
      </div>

      {/* ── FOOTER ───────────────────────────────────────────────────────── */}
      <footer className="bg-white border-t border-gray-100 py-10">
        <div className="max-w-screen-xl mx-auto px-4 sm:px-6">
          <div className="flex flex-col sm:flex-row items-center justify-between gap-5 mb-6">
            <div className="flex items-center gap-3 text-center sm:text-left">
              <div className="w-11 h-11 rounded-xl flex items-center justify-center shrink-0 shadow-sm" style={{ backgroundColor: theme + "18" }}>
                <span className="text-sm font-bold" style={{ color: theme }}>{initials}</span>
              </div>
              <div>
                <p className="font-bold text-gray-900">{business.name}</p>
                {business.tagline && <p className="text-sm text-gray-400 mt-0.5">{business.tagline}</p>}
              </div>
            </div>
            <div className="flex items-center gap-3">
              {business.whatsapp_number && (
                <a href={`https://wa.me/${business.whatsapp_number}`} target="_blank" rel="noreferrer"
                  className="w-10 h-10 bg-[#25D366] rounded-full flex items-center justify-center shadow-sm hover:scale-105 transition-transform">
                  <MessageCircle size={17} className="text-white" />
                </a>
              )}
              {business.instagram_id && (
                <a href={`https://instagram.com/${business.instagram_id}`} target="_blank" rel="noreferrer"
                  className="w-10 h-10 bg-gradient-to-br from-pink-500 to-purple-600 rounded-full flex items-center justify-center shadow-sm hover:scale-105 transition-transform">
                  <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2">
                    <rect x="2" y="2" width="20" height="20" rx="5" /><path d="M16 11.37A4 4 0 1 1 12.63 8 4 4 0 0 1 16 11.37z" /><line x1="17.5" y1="6.5" x2="17.51" y2="6.5" />
                  </svg>
                </a>
              )}
            </div>
          </div>

          <div className="flex flex-wrap justify-center gap-3 py-5 border-y border-gray-100">
            <button
              onClick={() => { navigator.clipboard.writeText(window.location.href); showToast("Link copied!"); }}
              className="inline-flex items-center gap-1.5 text-sm font-medium text-gray-600 border border-gray-200 rounded-full px-4 py-2.5 hover:bg-gray-50 transition-colors"
            >
              <Share2 size={14} />
              Share Catalogue
            </button>
            <button
              onClick={() => publicApi.downloadPdf(slug!)}
              className="inline-flex items-center gap-1.5 text-sm font-medium text-gray-600 border border-gray-200 rounded-full px-4 py-2.5 hover:bg-gray-50 transition-colors"
            >
              📄 Download PDF
            </button>
          </div>

          <p className="text-center text-xs text-gray-400 mt-5">
            Powered by <strong className="text-gray-500">SellerTalk24</strong>
          </p>
        </div>
      </footer>

      {/* ── FLOATING WHATSAPP ────────────────────────────────────────────── */}
      {business.whatsapp_number && (
        <motion.a
          href={`https://wa.me/${business.whatsapp_number}?text=${encodeURIComponent(`Hi ${business.name}! I'm checking out your catalogue.`)}`}
          target="_blank" rel="noreferrer"
          initial={{ opacity: 0, scale: 0.8 }}
          animate={{ opacity: 1, scale: 1 }}
          whileHover={{ scale: 1.06 }}
          whileTap={{ scale: 0.94 }}
          className="fixed bottom-6 right-4 sm:right-6 w-14 h-14 rounded-full flex items-center justify-center shadow-xl z-40"
          style={{ backgroundColor: "#25D366" }}
          aria-label={`Chat with ${business.name} on WhatsApp`}
        >
          <motion.div
            animate={{ scale: [1, 1.15, 1] }}
            transition={{ repeat: Infinity, duration: 2.5, repeatDelay: 0.5 }}
          >
            <MessageCircle size={26} className="text-white" />
          </motion.div>
        </motion.a>
      )}

      {/* ── SCROLL TO TOP ────────────────────────────────────────────────── */}
      <AnimatePresence>
        {scrollY > 300 && (
          <motion.button
            initial={{ opacity: 0, scale: 0.8 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.8 }}
            onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}
            className="fixed bottom-24 right-4 sm:right-6 w-10 h-10 bg-white rounded-full flex items-center justify-center shadow-lg z-40 border border-gray-100"
          >
            <ArrowUp size={18} className="text-gray-600" />
          </motion.button>
        )}
      </AnimatePresence>

      {/* ── NOTIFY MODAL ─────────────────────────────────────────────────── */}
      <AnimatePresence>
        {notifyProduct && (
          <NotifyModal
            product={notifyProduct} slug={slug!} themeColor={theme}
            onClose={() => setNotifyProduct(null)}
          />
        )}
      </AnimatePresence>

      {/* ── TOAST ────────────────────────────────────────────────────────── */}
      <AnimatePresence>
        {toast && (
          <motion.div
            initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 20 }}
            className="fixed bottom-24 left-1/2 -translate-x-1/2 bg-gray-900 text-white text-sm px-4 py-2 rounded-full shadow-lg z-50 whitespace-nowrap"
          >
            {toast}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
