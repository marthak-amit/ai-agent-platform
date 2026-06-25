import { motion, AnimatePresence } from "framer-motion";

interface FAQItemProps {
  question: string;
  answer: string;
  open: boolean;
  onToggle: () => void;
}

export default function FAQItem({ question, answer, open, onToggle }: FAQItemProps) {
  return (
    <div className="border-b border-gray-200 py-4">
      <button
        type="button"
        className="flex w-full items-center justify-between gap-4 text-left"
        aria-expanded={open}
        onClick={onToggle}
      >
        <span className="text-base font-medium text-gray-900">{question}</span>
        <motion.span
          className="shrink-0 text-xl text-gray-500"
          aria-hidden="true"
          animate={{ rotate: open ? 45 : 0 }}
          transition={{ duration: 0.2, ease: "easeOut" }}
        >
          +
        </motion.span>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.25, ease: "easeOut" }}
            className="overflow-hidden"
          >
            <p className="mt-3 text-sm text-gray-600">{answer}</p>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
