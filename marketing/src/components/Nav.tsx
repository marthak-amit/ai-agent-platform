import { useEffect, useState } from "react";
import { Link, NavLink } from "react-router-dom";
import { motion } from "framer-motion";
import { DASHBOARD_LOGIN_URL, DASHBOARD_SIGNUP_URL } from "../config";
import CTAButton from "./CTAButton";
import Logo from "./Logo";

const links = [
  { to: "/features", label: "Features" },
  { to: "/industries", label: "Industries" },
  { to: "/pricing", label: "Pricing" },
  { to: "/faq", label: "FAQ" },
];

export default function Nav() {
  const [open, setOpen] = useState(false);
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 40);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <motion.header
      className={`sticky top-0 z-50 border-b ${
        scrolled
          ? "border-gray-200 bg-white/80 shadow-sm backdrop-blur-md"
          : "border-transparent bg-white/90 backdrop-blur"
      }`}
      animate={{ paddingTop: scrolled ? 2 : 0, paddingBottom: scrolled ? 2 : 0 }}
      transition={{ duration: 0.2, ease: "easeOut" }}
    >
      <nav className="mx-auto flex max-w-7xl items-center justify-between px-4 py-3 sm:px-6 lg:px-8">
        <Link to="/" className="flex items-center gap-2 text-lg font-bold tracking-tight text-gray-900">
          <Logo className="h-11 w-auto" />
        </Link>

        <div className="hidden items-center gap-8 md:flex">
          {links.map((link) => (
            <NavLink
              key={link.to}
              to={link.to}
              className={({ isActive }) =>
                `text-sm font-medium transition-colors hover:text-brand-primaryDark ${
                  isActive ? "text-brand-primaryDark" : "text-gray-600"
                }`
              }
            >
              {link.label}
            </NavLink>
          ))}
        </div>

        <div className="hidden items-center gap-3 md:flex">
          <a
            href={DASHBOARD_LOGIN_URL}
            className="text-sm font-medium text-gray-700 hover:text-brand-primaryDark"
          >
            Log in
          </a>
          <CTAButton to={DASHBOARD_SIGNUP_URL} external variant="secondary">
            Create your account
          </CTAButton>
          <CTAButton to="/demo">Book a Demo</CTAButton>
        </div>

        <button
          type="button"
          className="inline-flex items-center justify-center rounded-md p-2 text-gray-700 md:hidden"
          aria-expanded={open}
          aria-label="Toggle navigation menu"
          onClick={() => setOpen((v) => !v)}
        >
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            {open ? (
              <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            ) : (
              <path d="M4 7h16M4 12h16M4 17h16" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            )}
          </svg>
        </button>
      </nav>

      {open && (
        <div className="border-t border-gray-200 bg-white px-4 py-4 md:hidden">
          <div className="flex flex-col gap-4">
            {links.map((link) => (
              <NavLink
                key={link.to}
                to={link.to}
                onClick={() => setOpen(false)}
                className="text-base font-medium text-gray-800"
              >
                {link.label}
              </NavLink>
            ))}
            <a href={DASHBOARD_LOGIN_URL} className="text-base font-medium text-gray-800">
              Log in
            </a>
            <CTAButton to={DASHBOARD_SIGNUP_URL} external variant="secondary" className="w-full">
              Create your account
            </CTAButton>
            <CTAButton to="/demo" className="w-full">
              Book a Demo
            </CTAButton>
          </div>
        </div>
      )}
    </motion.header>
  );
}
