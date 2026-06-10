const BASE_URL = (import.meta.env.VITE_API_URL as string) || "http://localhost:8000";

export interface BusinessInfo {
  name: string;
  tagline: string | null;
  logo_url: string | null;
  banner_url: string | null;
  whatsapp_number: string | null;
  instagram_id: string | null;
  theme_color: string;
  slug: string;
}

export interface ProductVariant {
  id: number;
  color: string | null;
  size: string | null;
  material: string | null;
  sku: string | null;
  price: number | null;
  stock: number;
  is_active: boolean;
  image_url: string | null;
}

export interface PublicProduct {
  id: number;
  name: string;
  sku: string | null;
  category: string | null;
  price: number;
  description: string | null;
  image_url: string | null;
  stock: number | null;
  is_active: boolean;
  is_available: boolean;
  low_stock_alert: number;
  has_variants: boolean;
  variants: ProductVariant[];
  available_colors: string[];
  available_sizes: string[];
}

export interface CatalogueResponse {
  business: BusinessInfo;
  products: PublicProduct[];
  categories: string[];
}

export interface ProductDetailResponse {
  product: PublicProduct;
  business: { name: string; whatsapp_number: string | null; theme_color: string };
  whatsapp_message: string;
}

export const publicApi = {
  getCatalogue: (slug: string): Promise<CatalogueResponse> =>
    fetch(`${BASE_URL}/shop/${slug}`).then((r) => {
      if (!r.ok) throw new Error("Catalogue not found");
      return r.json();
    }),

  getProduct: (slug: string, sku: string): Promise<ProductDetailResponse> =>
    fetch(`${BASE_URL}/shop/${slug}/product/${sku}`).then((r) => {
      if (!r.ok) throw new Error("Product not found");
      return r.json();
    }),

  searchProducts: (
    slug: string,
    q: string,
    category?: string
  ): Promise<{ products: PublicProduct[]; categories: string[]; total: number }> =>
    fetch(
      `${BASE_URL}/shop/${slug}/search?q=${encodeURIComponent(q)}${
        category ? `&category=${encodeURIComponent(category)}` : ""
      }`
    ).then((r) => r.json()),

  notifyRestock: (slug: string, sku: string, phone: string): Promise<{ message: string }> =>
    fetch(`${BASE_URL}/shop/${slug}/notify-restock`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sku, phone }),
    }).then((r) => r.json()),

  downloadPdf: async (slug: string): Promise<void> => {
    const r = await fetch(`${BASE_URL}/shop/${slug}/pdf`);
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${slug}-catalogue.pdf`;
    a.click();
    URL.revokeObjectURL(url);
  },
};
