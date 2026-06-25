import { motion, useReducedMotion } from "framer-motion";

interface Bubble {
  from: "customer" | "agent";
  text: string;
}

const conversation: Bubble[] = [
  { from: "customer", text: "Hi! Do you have banarasi sarees in red?" },
  { from: "agent", text: "Yes! Here are 3 banarasi sarees in red 🔴 — want me to show sizes & prices?" },
  { from: "customer", text: "Show me the maroon one" },
  { from: "agent", text: "Maroon Banarasi Silk Saree — ₹4,200. Available in Free Size. Shall I add it to your order?" },
  { from: "customer", text: "Yes, confirm with UPI" },
  { from: "agent", text: "Order placed ✅ Pay ₹4,200 via UPI: scan the QR or pay to agentlyai@upi. Dispatch in 3-5 days 🚚" },
];

export default function ChatMockup() {
  const reduceMotion = useReducedMotion();

  return (
    <motion.div
      className="w-full max-w-sm rounded-3xl border border-gray-200/80 bg-[#e9edef] p-3 shadow-2xl shadow-indigo-900/10"
      role="img"
      aria-label="Sample WhatsApp conversation showing a customer browsing a saree, picking a color, confirming the order, and paying via UPI"
      initial={reduceMotion ? undefined : { opacity: 0, y: 16, scale: 0.98 }}
      whileInView={reduceMotion ? undefined : { opacity: 1, y: 0, scale: 1 }}
      viewport={{ once: true }}
      transition={{ duration: 0.5, ease: "easeOut" }}
    >
      <div className="mb-3 flex items-center gap-2 rounded-2xl bg-[#075e54] px-3 py-2 text-white">
        <span className="inline-block h-8 w-8 shrink-0 rounded-full bg-white/20" aria-hidden="true" />
        <div>
          <p className="text-sm font-semibold">Meera Fashions</p>
          <p className="text-xs text-white/80">AI assistant · online</p>
        </div>
      </div>
      <div className="flex flex-col gap-2">
        {conversation.map((bubble, i) => (
          <motion.div
            key={i}
            initial={reduceMotion ? undefined : { opacity: 0, y: 8 }}
            whileInView={reduceMotion ? undefined : { opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ duration: 0.35, ease: "easeOut", delay: reduceMotion ? 0 : i * 0.06 }}
            className={`max-w-[85%] rounded-xl px-3 py-2 text-sm shadow-sm ${
              bubble.from === "customer"
                ? "self-start bg-white text-gray-800"
                : "self-end bg-[#d9fdd3] text-gray-800"
            }`}
          >
            {bubble.text}
          </motion.div>
        ))}
      </div>
    </motion.div>
  );
}
