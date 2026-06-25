import { forwardRef, useEffect, useRef, useState } from "react";
import { Bot, MoreVertical } from "lucide-react";

interface AgentNodeProps {
  name: string;
}

const AgentNode = forwardRef<HTMLDivElement, AgentNodeProps>(({ name }, ref) => {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onClickOutside(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    }
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, []);

  return (
    <div
      ref={ref}
      className="relative w-56 bg-white rounded-2xl border border-slate-200 shadow-sm px-6 py-6 flex flex-col items-center text-center z-10"
    >
      <div ref={menuRef} className="absolute top-2 right-2">
        <button
          onClick={() => setMenuOpen((v) => !v)}
          className="w-7 h-7 flex items-center justify-center rounded-lg text-gray-400 hover:bg-gray-100 hover:text-gray-600 transition-colors"
        >
          <MoreVertical size={16} />
        </button>
        {menuOpen && (
          <div className="absolute right-0 mt-1 w-36 bg-white rounded-xl border border-gray-100 shadow-lg py-1 text-left">
            <button className="w-full text-left px-3.5 py-2 text-sm text-gray-600 hover:bg-gray-50 transition-colors">
              Rename
            </button>
            <button className="w-full text-left px-3.5 py-2 text-sm text-gray-600 hover:bg-gray-50 transition-colors">
              Settings
            </button>
          </div>
        )}
      </div>

      <div className="w-12 h-12 rounded-2xl bg-indigo-600 flex items-center justify-center shadow-sm mb-3">
        <Bot size={24} className="text-white" />
      </div>
      <h2 className="font-bold text-gray-900 text-sm leading-snug break-words">{name}</h2>
      <span className="mt-1.5 text-[11px] font-medium text-indigo-500 bg-indigo-50 px-2 py-0.5 rounded-full">
        AI Agent
      </span>
    </div>
  );
});
AgentNode.displayName = "AgentNode";

export default AgentNode;
