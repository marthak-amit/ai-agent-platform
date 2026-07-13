import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { motion } from "framer-motion";

interface CTAButtonProps {
  to: string;
  children: ReactNode;
  variant?: "primary" | "secondary";
  external?: boolean;
  className?: string;
}

const MotionLink = motion(Link);
const MotionAnchor = motion.a;

export default function CTAButton({
  to,
  children,
  variant = "primary",
  external = false,
  className = "",
}: CTAButtonProps) {
  const base =
    "inline-flex items-center justify-center rounded-full px-6 py-3 text-sm font-semibold transition-all focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2";
  const styles =
    variant === "primary"
      ? "bg-brand-primaryDark text-white shadow-md shadow-brand-primary/20 hover:shadow-lg hover:shadow-brand-primary/30 hover:bg-brand-primary/90 focus-visible:ring-brand-primary"
      : "bg-white text-brand-secondary ring-1 ring-inset ring-gray-300 hover:bg-gray-50 hover:ring-gray-400 focus-visible:ring-brand-primary";

  const tapAnimation = { whileTap: { scale: 0.97 } };

  if (external) {
    return (
      <MotionAnchor href={to} className={`${base} ${styles} ${className}`} {...tapAnimation}>
        {children}
      </MotionAnchor>
    );
  }

  return (
    <MotionLink to={to} className={`${base} ${styles} ${className}`} {...tapAnimation}>
      {children}
    </MotionLink>
  );
}
