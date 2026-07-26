import type { ReactNode } from "react";
import { motion } from "framer-motion";

interface FeatureCardProps {
  icon?: ReactNode;
  title: string;
  description: string;
  /** Heading level for `title`, so it nests correctly under the group heading in each page's outline. Defaults to h3. */
  headingLevel?: "h3" | "h4";
}

export default function FeatureCard({ icon, title, description, headingLevel = "h3" }: FeatureCardProps) {
  const Heading = headingLevel;
  return (
    <motion.div
      className="rounded-2xl bg-white p-6 shadow-sm shadow-gray-900/5 ring-1 ring-gray-900/5"
      whileHover={{ y: -4, boxShadow: "0 16px 32px -12px rgba(15, 139, 76, 0.18)" }}
      transition={{ duration: 0.15, ease: "easeOut" }}
    >
      {icon && (
        <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-lg bg-brand-primary/10 text-brand-primaryDark">
          {icon}
        </div>
      )}
      <Heading className="text-base font-semibold text-gray-900">{title}</Heading>
      <p className="mt-2 text-sm text-gray-600">{description}</p>
    </motion.div>
  );
}
