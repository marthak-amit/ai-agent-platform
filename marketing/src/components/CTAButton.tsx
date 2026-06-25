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
      ? "bg-gradient-to-r from-indigo-600 to-violet-600 text-white shadow-md shadow-indigo-600/20 hover:shadow-lg hover:shadow-indigo-600/30 hover:from-indigo-500 hover:to-violet-500 focus-visible:ring-indigo-500"
      : "bg-white text-gray-900 ring-1 ring-inset ring-gray-300 hover:bg-gray-50 hover:ring-gray-400 focus-visible:ring-indigo-500";

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
