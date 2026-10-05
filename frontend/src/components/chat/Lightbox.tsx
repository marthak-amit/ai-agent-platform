import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, X, ZoomIn, ZoomOut } from "lucide-react";
import { useTranslation } from "react-i18next";

interface Props {
  /** One or more image URLs; arrows/←/→ move between them. */
  images: string[];
  startIndex?: number;
  onClose: () => void;
}

const MIN = 1;
const MAX = 5;

/** Fullscreen image viewer with wheel/button zoom, drag-to-pan and carousel navigation. */
export default function Lightbox({ images, startIndex = 0, onClose }: Props) {
  const { t } = useTranslation();
  const [index, setIndex] = useState(startIndex);
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null);

  const reset = useCallback(() => {
    setZoom(1);
    setOffset({ x: 0, y: 0 });
  }, []);

  const go = useCallback(
    (delta: number) => {
      setIndex((i) => (i + delta + images.length) % images.length);
      reset();
    },
    [images.length, reset],
  );

  const changeZoom = useCallback((next: number) => {
    const z = Math.min(MAX, Math.max(MIN, next));
    setZoom(z);
    if (z === 1) setOffset({ x: 0, y: 0 });
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowRight" && images.length > 1) go(1);
      else if (e.key === "ArrowLeft" && images.length > 1) go(-1);
      else if (e.key === "+" || e.key === "=") changeZoom(zoom + 0.5);
      else if (e.key === "-") changeZoom(zoom - 0.5);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, go, changeZoom, zoom, images.length]);

  return (
    <div
      className="fixed inset-0 z-[90] bg-black/90 flex items-center justify-center select-none"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={t("chat.image_viewer")}
    >
      <div className="absolute top-3 right-3 flex items-center gap-2 z-10" onClick={(e) => e.stopPropagation()}>
        <button onClick={() => changeZoom(zoom - 0.5)} className="p-2 rounded-full bg-white/10 text-white hover:bg-white/20" aria-label={t("chat.zoom_out")}>
          <ZoomOut size={18} />
        </button>
        <span className="text-white text-xs w-10 text-center tabular-nums">{Math.round(zoom * 100)}%</span>
        <button onClick={() => changeZoom(zoom + 0.5)} className="p-2 rounded-full bg-white/10 text-white hover:bg-white/20" aria-label={t("chat.zoom_in")}>
          <ZoomIn size={18} />
        </button>
        <button onClick={onClose} className="p-2 rounded-full bg-white/10 text-white hover:bg-white/20" aria-label={t("chat.close")}>
          <X size={18} />
        </button>
      </div>

      {images.length > 1 && (
        <>
          <button
            onClick={(e) => { e.stopPropagation(); go(-1); }}
            className="absolute left-3 top-1/2 -translate-y-1/2 p-2 rounded-full bg-white/10 text-white hover:bg-white/20 z-10"
            aria-label={t("chat.previous")}
          >
            <ChevronLeft size={22} />
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); go(1); }}
            className="absolute right-3 top-1/2 -translate-y-1/2 p-2 rounded-full bg-white/10 text-white hover:bg-white/20 z-10"
            aria-label={t("chat.next")}
          >
            <ChevronRight size={22} />
          </button>
          <div className="absolute bottom-4 left-1/2 -translate-x-1/2 text-white/80 text-xs">
            {index + 1} / {images.length}
          </div>
        </>
      )}

      <img
        src={images[index]}
        alt=""
        draggable={false}
        onClick={(e) => e.stopPropagation()}
        onDoubleClick={() => changeZoom(zoom > 1 ? 1 : 2.5)}
        onWheel={(e) => changeZoom(zoom + (e.deltaY < 0 ? 0.25 : -0.25))}
        onPointerDown={(e) => {
          if (zoom <= 1) return;
          (e.target as HTMLElement).setPointerCapture(e.pointerId);
          drag.current = { x: e.clientX, y: e.clientY, ox: offset.x, oy: offset.y };
        }}
        onPointerMove={(e) => {
          if (!drag.current) return;
          setOffset({ x: drag.current.ox + e.clientX - drag.current.x, y: drag.current.oy + e.clientY - drag.current.y });
        }}
        onPointerUp={() => { drag.current = null; }}
        style={{
          transform: `translate(${offset.x}px, ${offset.y}px) scale(${zoom})`,
          cursor: zoom > 1 ? "grab" : "zoom-in",
          transition: drag.current ? "none" : "transform 120ms ease-out",
        }}
        className="max-h-[90vh] max-w-[92vw] object-contain touch-none"
      />
    </div>
  );
}
