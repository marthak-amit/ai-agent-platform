import { forwardRef, type ReactNode } from "react";
import { Settings } from "lucide-react";

export type ChannelStatus = "connected" | "not_connected" | "loading";

interface ChannelCardProps {
  id: string;
  name: string;
  icon: ReactNode;
  status: ChannelStatus;
  onConnect: () => void;
  onManage: () => void;
}

const ChannelCard = forwardRef<HTMLDivElement, ChannelCardProps>(
  ({ name, icon, status, onConnect, onManage }, ref) => {
    if (status === "loading") {
      return (
        <div ref={ref} className="w-72 bg-white rounded-2xl border border-slate-200 shadow-sm p-4 animate-pulse">
          <div className="flex items-center gap-3 mb-4">
            <div className="w-10 h-10 rounded-xl bg-gray-200 shrink-0" />
            <div className="flex-1 space-y-2">
              <div className="h-3 w-2/3 bg-gray-200 rounded" />
              <div className="h-2.5 w-1/3 bg-gray-100 rounded" />
            </div>
          </div>
          <div className="h-9 bg-gray-100 rounded-lg" />
        </div>
      );
    }

    const connected = status === "connected";

    return (
      <div ref={ref} className="w-72 bg-white rounded-2xl border border-slate-200 shadow-sm p-4">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-10 h-10 rounded-xl flex items-center justify-center shrink-0 overflow-hidden">{icon}</div>
          <div className="min-w-0">
            <p className="text-sm font-semibold text-gray-900 leading-tight">{name}</p>
            <span className="flex items-center gap-1.5 text-xs mt-0.5">
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${connected ? "bg-green-500" : "bg-amber-500"}`} />
              <span className={connected ? "text-green-600" : "text-amber-600"}>
                {connected ? "Connected" : "Not connected"}
              </span>
            </span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={connected ? onManage : onConnect}
            className={`flex-1 py-2 rounded-lg text-xs font-semibold transition-colors ${
              connected
                ? "border border-indigo-200 text-indigo-700 hover:bg-indigo-50"
                : "bg-indigo-600 text-white hover:bg-indigo-700"
            }`}
          >
            {connected ? "Manage" : "Connect"}
          </button>
          <button
            onClick={onManage}
            aria-label={`${name} settings`}
            className="w-9 h-9 shrink-0 flex items-center justify-center rounded-lg border border-gray-200 text-gray-400 hover:bg-gray-50 hover:text-gray-600 transition-colors"
          >
            <Settings size={14} />
          </button>
        </div>
      </div>
    );
  }
);
ChannelCard.displayName = "ChannelCard";

export default ChannelCard;
