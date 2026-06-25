import { useEffect, useState, type RefObject } from "react";

interface ConnectorLinesProps {
  containerRef: RefObject<HTMLDivElement | null>;
  agentRef: RefObject<HTMLDivElement | null>;
  leftRef: RefObject<HTMLDivElement | null>;
  rightRefs: RefObject<HTMLDivElement>[];
}

interface Dot {
  key: string;
  x: number;
  y: number;
}

interface LinesState {
  paths: { key: string; d: string }[];
  dots: Dot[];
}

function rectOf(el: HTMLElement, containerRect: DOMRect) {
  const r = el.getBoundingClientRect();
  return {
    top: r.top - containerRect.top,
    left: r.left - containerRect.left,
    right: r.right - containerRect.left,
    bottom: r.bottom - containerRect.top,
    width: r.width,
    height: r.height,
  };
}

export default function ConnectorLines({ containerRef, agentRef, leftRef, rightRefs }: ConnectorLinesProps) {
  const [lines, setLines] = useState<LinesState>({ paths: [], dots: [] });

  useEffect(() => {
    function recompute() {
      const container = containerRef.current;
      const agent = agentRef.current;
      if (!container || !agent) return;
      const containerRect = container.getBoundingClientRect();
      const agentRect = rectOf(agent, containerRect);
      const paths: { key: string; d: string }[] = [];
      const dots: Dot[] = [];

      const left = leftRef.current;
      if (left) {
        const leftRect = rectOf(left, containerRect);
        const x1 = leftRect.right;
        const y1 = leftRect.top + leftRect.height / 2;
        const x2 = agentRect.left;
        const y2 = agentRect.top + agentRect.height / 2;
        paths.push({ key: "left", d: `M ${x1} ${y1} H ${x2}` });
        dots.push({ key: "left-start", x: x1, y: y1 }, { key: "left-end", x: x2, y: y2 });
      }

      const rightEls = rightRefs.map((ref) => ref.current).filter((el): el is HTMLDivElement => !!el);
      if (rightEls.length > 0) {
        const x0 = agentRect.right;
        const y0 = agentRect.top + agentRect.height / 2;
        const targets = rightEls.map((el) => {
          const r = rectOf(el, containerRect);
          return { x: r.left, y: r.top + r.height / 2 };
        });
        const trunkX = x0 + Math.min(48, Math.max(24, (Math.min(...targets.map((t) => t.x)) - x0) / 2));

        dots.push({ key: "right-start", x: x0, y: y0 });
        paths.push({ key: "right-trunk-in", d: `M ${x0} ${y0} H ${trunkX}` });

        if (targets.length > 1) {
          const minY = Math.min(...targets.map((t) => t.y));
          const maxY = Math.max(...targets.map((t) => t.y));
          paths.push({ key: "right-trunk-bus", d: `M ${trunkX} ${minY} V ${maxY}` });
        }

        targets.forEach((t, i) => {
          paths.push({ key: `right-branch-${i}`, d: `M ${trunkX} ${t.y} H ${t.x}` });
          dots.push({ key: `right-end-${i}`, x: t.x, y: t.y });
        });
      }

      setLines({ paths, dots });
    }

    recompute();

    const ro = new ResizeObserver(recompute);
    if (containerRef.current) ro.observe(containerRef.current);
    if (agentRef.current) ro.observe(agentRef.current);
    if (leftRef.current) ro.observe(leftRef.current);
    rightRefs.forEach((ref) => ref.current && ro.observe(ref.current));

    window.addEventListener("resize", recompute);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", recompute);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <svg className="hidden md:block absolute inset-0 w-full h-full pointer-events-none" style={{ zIndex: 0 }}>
      {lines.paths.map((p) => (
        <path key={p.key} d={p.d} fill="none" stroke="#cbd5e1" strokeWidth={1.5} strokeLinecap="round" />
      ))}
      {lines.dots.map((d) => (
        <circle key={d.key} cx={d.x} cy={d.y} r={3} fill="#94a3b8" />
      ))}
    </svg>
  );
}
