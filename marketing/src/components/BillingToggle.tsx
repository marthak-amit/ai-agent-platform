import { motion } from "framer-motion";

export type BillingCycle = "monthly" | "yearly";

interface BillingToggleProps {
  value: BillingCycle;
  onChange: (value: BillingCycle) => void;
}

export default function BillingToggle({ value, onChange }: BillingToggleProps) {
  const isYearly = value === "yearly";

  return (
    <div className="flex items-center justify-center gap-3">
      <span className={`text-sm font-medium ${!isYearly ? "text-gray-900" : "text-gray-500"}`}>
        Monthly
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={isYearly}
        aria-label="Toggle billing cycle"
        onClick={() => onChange(isYearly ? "monthly" : "yearly")}
        className="relative h-7 w-12 rounded-full bg-gray-200 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-primary"
      >
        <motion.span
          className="absolute top-0.5 left-0.5 h-6 w-6 rounded-full bg-brand-primary shadow"
          animate={{ x: isYearly ? 20 : 0 }}
          transition={{ type: "spring", stiffness: 400, damping: 30 }}
        />
      </button>
      <span className={`text-sm font-medium ${isYearly ? "text-gray-900" : "text-gray-500"}`}>
        Yearly
      </span>
    </div>
  );
}
