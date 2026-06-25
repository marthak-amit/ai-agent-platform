import { createRef, useRef, type ReactNode, type RefObject } from "react";
import AgentNode from "./AgentNode";
import AddToolCard from "./AddToolCard";
import ChannelCard, { type ChannelStatus } from "./ChannelCard";
import ConnectorLines from "./ConnectorLines";

export interface ChannelCardConfig {
  id: string;
  name: string;
  icon: ReactNode;
  status: ChannelStatus;
  onConnect: () => void;
  onManage: () => void;
}

interface AgentCanvasProps {
  agentName: string;
  channels: ChannelCardConfig[];
}

export default function AgentCanvas({ agentName, channels }: AgentCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const agentRef = useRef<HTMLDivElement>(null);
  const addToolRef = useRef<HTMLDivElement>(null);
  const channelRefsHolder = useRef<RefObject<HTMLDivElement>[]>([]);
  while (channelRefsHolder.current.length < channels.length) {
    channelRefsHolder.current.push(createRef<HTMLDivElement>());
  }
  const channelRefs = channelRefsHolder.current.slice(0, channels.length);

  return (
    <div ref={containerRef} className="relative bg-[#fafafa] rounded-2xl border border-gray-100 px-6 py-12 md:px-14 lg:px-20">
      <ConnectorLines
        containerRef={containerRef}
        agentRef={agentRef}
        leftRef={addToolRef}
        rightRefs={channelRefs}
      />

      <div className="relative z-10 flex flex-col md:flex-row md:items-center md:justify-between gap-10 md:gap-12 lg:gap-20">
        <div className="flex justify-center md:justify-start shrink-0">
          <AddToolCard ref={addToolRef} />
        </div>

        <div className="flex justify-center shrink-0">
          <AgentNode ref={agentRef} name={agentName} />
        </div>

        <div className="flex flex-col items-center md:items-end gap-4 shrink-0">
          {channels.map((c, i) => (
            <ChannelCard
              key={c.id}
              ref={channelRefs[i]}
              id={c.id}
              name={c.name}
              icon={c.icon}
              status={c.status}
              onConnect={c.onConnect}
              onManage={c.onManage}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
