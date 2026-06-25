import { useEffect, useRef } from "react";

const WIDGET_SCRIPT_SRC = "https://assets.calendly.com/assets/external/widget.js";

declare global {
  interface Window {
    Calendly?: {
      initInlineWidget: (options: { url: string; parentElement: HTMLElement }) => void;
    };
  }
}

function loadWidgetScript(): Promise<void> {
  const existing = document.querySelector<HTMLScriptElement>(`script[src="${WIDGET_SCRIPT_SRC}"]`);
  if (existing) {
    if (window.Calendly) return Promise.resolve();
    return new Promise((resolve) => existing.addEventListener("load", () => resolve()));
  }
  return new Promise((resolve) => {
    const script = document.createElement("script");
    script.src = WIDGET_SCRIPT_SRC;
    script.async = true;
    script.onload = () => resolve();
    document.body.appendChild(script);
  });
}

interface CalendlyEmbedProps {
  url: string;
  className?: string;
}

// Calendly's official inline embed needs `window`/`document`, so it can only
// run client-side. Mounting via useEffect (not module scope) keeps it out of
// vite-react-ssg's server render entirely — the container renders empty on
// the prerendered HTML and Calendly fills it in after hydration.
export default function CalendlyEmbed({ url, className = "" }: CalendlyEmbedProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    loadWidgetScript().then(() => {
      if (cancelled || !containerRef.current || !window.Calendly) return;
      window.Calendly.initInlineWidget({ url, parentElement: containerRef.current });
    });
    return () => {
      cancelled = true;
    };
  }, [url]);

  return (
    // Calendly's iframe sizes itself to height:100% of this container, so it
    // needs an explicit `height` (not `min-height`) — with only min-height,
    // the box collapses to the iframe's pre-load size and the min-height
    // reservation shows up as blank space instead of stretching the iframe.
    <div ref={containerRef} className={className} style={{ minWidth: "320px", height: "700px" }} />
  );
}
