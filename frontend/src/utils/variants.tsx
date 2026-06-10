/** Minimal variant shape — works for both dashboard Product and public PublicProduct variants. */
export interface VariantLike {
  color: string | null;
  size: string | null;
  stock: number;
}

export const COLOR_HEX: Record<string, string> = {
  Red: "#EF4444",
  Blue: "#3B82F6",
  Green: "#10B981",
  Pink: "#EC4899",
  Navy: "#1E3A5F",
  Black: "#111827",
  Gold: "#F59E0B",
  Purple: "#8B5CF6",
  White: "#F9FAFB",
};

const SIZE_ORDER = ["XS", "S", "M", "L", "XL", "XXL", "XXXL"];

export function colorHex(color: string): string {
  return COLOR_HEX[color] ?? "#9CA3AF";
}

export function sortSizes(sizes: string[]): string[] {
  return [...sizes].sort((a, b) => {
    const ai = SIZE_ORDER.indexOf(a);
    const bi = SIZE_ORDER.indexOf(b);
    if (ai === -1 && bi === -1) return a.localeCompare(b);
    if (ai === -1) return 1;
    if (bi === -1) return -1;
    return ai - bi;
  });
}

/** Stock for a given size, summed across all colours (used when no colour selected yet). */
export function stockForSize(variants: VariantLike[], size: string): number {
  return variants.filter((v) => v.size === size).reduce((s, v) => s + v.stock, 0);
}

/** Small row of colour swatches shown on catalogue cards. */
export function ColorDots({ colors, size = 16 }: { colors: string[]; size?: number }) {
  if (colors.length === 0) return null;
  return (
    <div className="flex items-center gap-1.5">
      {colors.map((c) => (
        <span
          key={c}
          title={c}
          className="rounded-full border border-black/10 shrink-0"
          style={{ width: size, height: size, backgroundColor: colorHex(c) }}
        />
      ))}
    </div>
  );
}

/** Small row of size pills shown on catalogue cards; greyed/struck-through when out of stock. */
export function SizePills({ variants, sizes }: { variants: VariantLike[]; sizes: string[] }) {
  if (sizes.length === 0) return null;
  const ordered = sortSizes(sizes);
  return (
    <div className="flex items-center gap-1 flex-wrap">
      {ordered.map((s) => {
        const inStock = stockForSize(variants, s) > 0;
        return (
          <span
            key={s}
            className={`text-[10px] font-medium px-1.5 py-0.5 rounded border ${
              inStock
                ? "border-gray-200 text-gray-600 bg-white"
                : "border-gray-100 text-gray-300 bg-gray-50 line-through"
            }`}
          >
            {s}
          </span>
        );
      })}
    </div>
  );
}
