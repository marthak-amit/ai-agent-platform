import type { ReactNode } from "react";
import { AnimatePresence, motion } from "framer-motion";
import CTAButton from "./CTAButton";

export interface PricingPlan {
  name: string;
  price: string;
  cadence?: string;
  limit: string;
  channels: string;
  highlighted?: boolean;
}

interface PricingCardProps {
  plan: PricingPlan;
  children?: ReactNode;
}

export default function PricingCard({ plan, children }: PricingCardProps) {
  return (
    <motion.div
      className={`relative flex flex-col rounded-3xl p-6 shadow-sm ${
        plan.highlighted
          ? "bg-white ring-2 ring-indigo-600"
          : "bg-white ring-1 ring-gray-900/5"
      }`}
      whileHover={{ y: -4, boxShadow: "0 16px 32px -12px rgba(79, 70, 229, 0.18)" }}
      transition={{ duration: 0.15, ease: "easeOut" }}
    >
      {plan.highlighted && (
        <motion.span
          className="pointer-events-none absolute inset-0 rounded-2xl"
          style={{ boxShadow: "0 0 0 2px rgba(79, 70, 229, 0.4)" }}
          animate={{
            boxShadow: [
              "0 0 0 2px rgba(79, 70, 229, 0.25)",
              "0 0 16px 2px rgba(79, 70, 229, 0.35)",
              "0 0 0 2px rgba(79, 70, 229, 0.25)",
            ],
          }}
          transition={{ duration: 2.5, repeat: Infinity, ease: "easeInOut" }}
        />
      )}
      {plan.highlighted && (
        <span className="mb-3 inline-block w-fit rounded-full bg-indigo-100 px-3 py-1 text-xs font-semibold text-indigo-700">
          Most popular
        </span>
      )}
      <h3 className="text-lg font-semibold text-gray-900">{plan.name}</h3>
      <p className="mt-2 flex items-baseline gap-1 overflow-hidden">
        <AnimatePresence mode="wait" initial={false}>
          <motion.span
            key={plan.price}
            className="text-3xl font-bold text-gray-900"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.2, ease: "easeOut" }}
          >
            {plan.price}
          </motion.span>
        </AnimatePresence>
        {plan.cadence && <span className="text-sm text-gray-500">{plan.cadence}</span>}
      </p>
      <ul className="mt-4 space-y-2 text-sm text-gray-600">
        <li>{plan.limit}</li>
        <li>{plan.channels}</li>
      </ul>
      {children}
      <CTAButton to="/demo" className="mt-6 w-full">
        Book a Demo
      </CTAButton>
    </motion.div>
  );
}
