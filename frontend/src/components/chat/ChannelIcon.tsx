/** Channel glyphs in brand colours: WhatsApp green #25D366, Instagram coral #F2536B. */
export const CHANNEL_COLOR: Record<string, string> = {
  whatsapp: "#25D366",
  instagram: "#F2536B",
  website: "#1A2E44",
};

export default function ChannelIcon({ channel, size = 16 }: { channel: string | null | undefined; size?: number }) {
  const color = CHANNEL_COLOR[channel ?? ""] ?? "#6B7280";
  const label = channel === "whatsapp" ? "WhatsApp" : channel === "instagram" ? "Instagram" : channel ?? "chat";
  if (channel === "instagram") {
    return (
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" role="img" aria-label={label}>
        <rect x="3" y="3" width="18" height="18" rx="5" />
        <circle cx="12" cy="12" r="4" />
        <circle cx="17.5" cy="6.5" r="0.9" fill={color} stroke="none" />
      </svg>
    );
  }
  if (channel === "website") {
    return (
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" role="img" aria-label={label}>
        <circle cx="12" cy="12" r="9" />
        <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" />
      </svg>
    );
  }
  // WhatsApp: speech bubble with handset
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" role="img" aria-label={label}>
      <path d="M3 21l1.6-4.8A8.5 8.5 0 1 1 8 19.5z" />
      <path d="M9 9.5c0 3 2.5 5.5 5.5 5.5l1-1.4-1.9-1-.8.8a3.2 3.2 0 0 1-1.7-1.7l.8-.8-1-1.9z" fill={color} stroke="none" />
    </svg>
  );
}
