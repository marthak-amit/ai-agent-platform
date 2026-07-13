import { useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import SEO from "../components/SEO";
import CTAButton from "../components/CTAButton";
import ChatMockup from "../components/ChatMockup";
import FeatureCard from "../components/FeatureCard";
import PricingCard, { type PricingPlan } from "../components/PricingCard";
import BillingToggle, { type BillingCycle } from "../components/BillingToggle";
import Reveal from "../components/motion/Reveal";
import { StaggerGroup, StaggerItem } from "../components/motion/StaggerGroup";
import { DASHBOARD_SIGNUP_URL } from "../config";

const steps = [
  {
    title: "Customer messages",
    description: "A shopper messages your WhatsApp or Instagram with a question or a product they saw.",
  },
  {
    title: "AI engages & shows products",
    description: "SellerTalk24 replies instantly, understands what they're looking for, and shares matching products from your catalogue.",
  },
  {
    title: "Customer picks size/color & confirms",
    description: "The AI walks them through variants — size, color, material — and confirms what they want to order.",
  },
  {
    title: "Order placed, paid & tracked",
    description: "Order details are captured, payment is collected (UPI/COD/bank transfer), and dispatch status is sent automatically.",
  },
];

const capabilities = [
  { title: "Chat-driven product browsing", description: "Customers explore your catalogue directly inside WhatsApp or Instagram chat." },
  { title: "Variant selection", description: "Color, size, and material picked conversationally — no forms, no app to install." },
  { title: "Full in-chat order collection", description: "Address, quantity, and order details collected without leaving the chat." },
  { title: "COD / UPI / bank transfer", description: "Accept the payment methods your customers already trust." },
  { title: "Order status & dispatch alerts", description: "Customers get automatic updates as their order moves and ships." },
  { title: "Returning customer recognition", description: "SellerTalk24 remembers past conversations so regulars don't repeat themselves." },
  { title: "Human takeover when needed", description: "Hand off to a real person any time the conversation needs a human touch." },
  { title: "Daily owner briefing email", description: "A daily summary of orders, leads, and conversations in your inbox." },
  { title: "Follow-up & re-engagement", description: "Automatic follow-ups bring browsing customers back to complete their order." },
  { title: "WhatsApp broadcast campaigns", description: "Reach your customer list with new arrivals and offers." },
];

const monthlyPlans: PricingPlan[] = [
  {
    name: "Starter",
    price: "₹1,499",
    cadence: "/mo",
    limit: "~800 conversations/mo",
    channels: "Instagram",
    badges: ["IG"],
    features: ["Order flow (cart → address → confirm)"],
  },
  {
    name: "Growth",
    price: "₹3,499",
    cadence: "/mo",
    limit: "~2,000 conversations/mo",
    channels: "WhatsApp + Instagram",
    badges: ["WA", "IG"],
    features: ["Order flow (cart → address → confirm)", "Broadcast / marketing templates"],
    highlighted: true,
  },
  {
    name: "Pro",
    price: "₹6,999",
    cadence: "/mo",
    limit: "~6,000 conversations/mo",
    channels: "WhatsApp + Instagram + Website widget",
    badges: ["WA", "IG"],
    features: ["Order flow (cart → address → confirm)", "Broadcast / marketing templates", "Priority support"],
  },
];

const yearlyPlans: PricingPlan[] = monthlyPlans.map((plan) => ({
  ...plan,
  price: "Contact us",
  cadence: undefined,
}));

export default function Home() {
  const [cycle, setCycle] = useState<BillingCycle>("monthly");
  const plans = cycle === "monthly" ? monthlyPlans : yearlyPlans;
  const reduceMotion = useReducedMotion();

  return (
    <>
      <SEO
        title="SellerTalk24 — Turn WhatsApp & Instagram Chats Into Orders for Fashion Brands"
        description="SellerTalk24 is an AI sales agent for Indian fashion retailers that browses, sells, and collects orders directly inside WhatsApp and Instagram chats."
        path="/"
      />

      {/* Hero */}
      <section className="relative overflow-hidden bg-gradient-to-b from-brand-primary/5 to-white">
        {!reduceMotion && (
          <motion.div
            aria-hidden="true"
            className="pointer-events-none absolute -top-32 left-1/2 h-[32rem] w-[32rem] -translate-x-1/2 rounded-full bg-brand-primary/20 blur-3xl"
            animate={{ scale: [1, 1.08, 1], rotate: [0, 8, 0] }}
            transition={{ duration: 18, repeat: Infinity, ease: "easeInOut" }}
            style={{ willChange: "transform" }}
          />
        )}
        <div className="relative mx-auto grid max-w-7xl items-center gap-12 px-4 py-16 sm:px-6 lg:grid-cols-2 lg:px-8 lg:py-24">
          <motion.div
            initial={reduceMotion ? undefined : "hidden"}
            animate={reduceMotion ? undefined : "visible"}
            variants={{
              hidden: {},
              visible: { transition: { staggerChildren: 0.12 } },
            }}
          >
            <motion.span
              className="inline-flex items-center gap-2 rounded-full bg-brand-primary/10 px-3 py-1 text-xs font-semibold text-brand-primaryDark ring-1 ring-inset ring-brand-primary/20"
              variants={{ hidden: { opacity: 0, y: 20 }, visible: { opacity: 1, y: 0 } }}
              transition={{ duration: 0.5, ease: "easeOut" }}
            >
              AI sales agent for fashion retailers
            </motion.span>
            <motion.h1
              className="mt-4 text-4xl font-extrabold tracking-tight text-gray-900 sm:text-5xl"
              variants={{ hidden: { opacity: 0, y: 20 }, visible: { opacity: 1, y: 0 } }}
              transition={{ duration: 0.5, ease: "easeOut" }}
            >
              Turn WhatsApp &amp; Instagram chats into{" "}
              <span className="bg-gradient-to-r from-brand-primary to-brand-primaryDark bg-clip-text text-transparent">
                Orders
              </span>
            </motion.h1>
            <motion.p
              className="mt-4 text-lg text-gray-600"
              variants={{ hidden: { opacity: 0, y: 20 }, visible: { opacity: 1, y: 0 } }}
              transition={{ duration: 0.5, ease: "easeOut" }}
            >
              SellerTalk24 is an AI sales agent built for Indian fashion retailers. It browses your
              catalogue, helps customers pick size and color, and collects the full order — right
              inside the chat your customers already use.
            </motion.p>
            <motion.div
              className="mt-8 flex flex-wrap gap-4"
              variants={{ hidden: { opacity: 0, y: 20 }, visible: { opacity: 1, y: 0 } }}
              transition={{ duration: 0.5, ease: "easeOut" }}
            >
              <CTAButton to="/demo">Book a Demo</CTAButton>
              <CTAButton to={DASHBOARD_SIGNUP_URL} external variant="secondary">
                Create your account
              </CTAButton>
            </motion.div>
            <motion.div
              className="mt-10 flex flex-wrap items-center gap-6 text-sm text-gray-500"
              variants={{ hidden: { opacity: 0, y: 20 }, visible: { opacity: 1, y: 0 } }}
              transition={{ duration: 0.5, ease: "easeOut" }}
            >
              <span>Built on Meta's WhatsApp &amp; Instagram messaging platform</span>
              <span aria-hidden="true">·</span>
              <span>Built in India, for India</span>
            </motion.div>
          </motion.div>
          <div className="flex justify-center">
            <ChatMockup />
          </div>
        </div>
      </section>

      {/* How it works */}
      <section className="mx-auto max-w-7xl px-4 py-20 sm:px-6 lg:px-8">
        <Reveal as="div" className="text-center">
          <span className="text-xs font-semibold uppercase tracking-widest text-brand-primaryDark">Process</span>
          <h2 className="mt-2 text-3xl font-bold tracking-tight text-gray-900">How it works</h2>
        </Reveal>
        <div className="relative mt-12">
          {!reduceMotion && (
            <motion.div
              aria-hidden="true"
              className="absolute left-[12.5%] right-[12.5%] top-5 hidden h-0.5 origin-left bg-brand-primary/40 md:block"
              initial={{ scaleX: 0 }}
              whileInView={{ scaleX: 1 }}
              viewport={{ once: true, margin: "-80px" }}
              transition={{ duration: 0.9, ease: "easeOut", delay: 0.2 }}
            />
          )}
          <StaggerGroup className="grid gap-8 sm:grid-cols-2 lg:grid-cols-4">
            {steps.map((step, i) => (
              <StaggerItem key={step.title} className="relative text-center">
                <div className="relative z-10 mx-auto flex h-10 w-10 items-center justify-center rounded-full bg-brand-primaryDark text-sm font-bold text-white shadow-sm shadow-brand-primary/30">
                  {i + 1}
                </div>
                <h3 className="mt-4 text-base font-semibold text-gray-900">{step.title}</h3>
                <p className="mt-2 text-sm text-gray-600">{step.description}</p>
              </StaggerItem>
            ))}
          </StaggerGroup>
        </div>
      </section>

      {/* Capabilities */}
      <section className="bg-gray-50 py-20">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <Reveal as="div" className="text-center">
            <span className="text-xs font-semibold uppercase tracking-widest text-brand-primaryDark">Capabilities</span>
            <h2 className="mt-2 text-3xl font-bold tracking-tight text-gray-900">What SellerTalk24 does</h2>
          </Reveal>
          <StaggerGroup className="mt-12 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
            {capabilities.map((cap) => (
              <StaggerItem key={cap.title}>
                <FeatureCard title={cap.title} description={cap.description} />
              </StaggerItem>
            ))}
          </StaggerGroup>
        </div>
      </section>

      {/* Channels */}
      <section className="mx-auto max-w-7xl px-4 py-20 sm:px-6 lg:px-8">
        <Reveal as="div" className="text-center">
          <span className="text-xs font-semibold uppercase tracking-widest text-brand-primaryDark">Channels</span>
          <h2 className="mt-2 text-3xl font-bold tracking-tight text-gray-900">Where SellerTalk24 works</h2>
        </Reveal>
        <StaggerGroup className="mt-12 grid gap-6 sm:grid-cols-3">
          <StaggerItem>
            <motion.div
              className="rounded-3xl bg-brand-primary/5 p-6 text-center ring-1 ring-inset ring-brand-primary/20"
              whileHover={{ y: -4, boxShadow: "0 16px 32px -12px rgba(37, 211, 102, 0.25)" }}
              transition={{ duration: 0.15, ease: "easeOut" }}
            >
              <motion.p
                className="text-lg font-semibold text-gray-900"
                whileHover={reduceMotion ? undefined : { scale: 1.08, rotate: 2 }}
                transition={{ duration: 0.15 }}
              >
                WhatsApp
              </motion.p>
              <p className="mt-1 text-sm text-gray-600">Live today, on every plan</p>
            </motion.div>
          </StaggerItem>
          <StaggerItem>
            <motion.div
              className="rounded-3xl bg-gradient-to-br from-brand-accent/5 to-amber-50 p-6 text-center ring-1 ring-inset ring-brand-accent/30"
              whileHover={{ y: -4, boxShadow: "0 16px 32px -12px rgba(242, 83, 107, 0.2)" }}
              transition={{ duration: 0.15, ease: "easeOut" }}
            >
              <motion.p
                className="text-lg font-semibold text-gray-900"
                whileHover={reduceMotion ? undefined : { scale: 1.08, rotate: 2 }}
                transition={{ duration: 0.15 }}
              >
                Instagram
              </motion.p>
              <p className="mt-1 text-sm text-gray-600">Live today, on Growth &amp; Pro</p>
            </motion.div>
          </StaggerItem>
          <StaggerItem>
            <motion.div
              className="rounded-3xl bg-white p-6 text-center ring-1 ring-inset ring-gray-900/5"
              whileHover={{ y: -4, boxShadow: "0 16px 32px -12px rgba(15, 139, 76, 0.18)" }}
              transition={{ duration: 0.15, ease: "easeOut" }}
            >
              <motion.p
                className="text-lg font-semibold text-gray-900"
                whileHover={reduceMotion ? undefined : { scale: 1.08, rotate: 2 }}
                transition={{ duration: 0.15 }}
              >
                Website widget
              </motion.p>
              <p className="mt-1 text-sm text-gray-600">Available on Pro</p>
            </motion.div>
          </StaggerItem>
        </StaggerGroup>
      </section>

      {/* Industry focus */}
      <section className="bg-gradient-to-br from-brand-primary/5 via-white to-brand-primary/5 py-20">
        <Reveal as="div" className="mx-auto max-w-7xl px-4 text-center sm:px-6 lg:px-8">
          <h2 className="text-3xl font-bold tracking-tight text-gray-900">Built for Indian fashion retail</h2>
          <p className="mx-auto mt-4 max-w-2xl text-gray-600">
            Sarees, kurtis, lehengas, and everything in between — SellerTalk24 understands fashion
            catalogues with variants like color, size, and fabric, and is built for India-first
            payments like UPI and COD.
          </p>
        </Reveal>
      </section>

      {/* Pricing */}
      <section className="mx-auto max-w-7xl px-4 py-20 sm:px-6 lg:px-8">
        <Reveal as="div" className="text-center">
          <span className="text-xs font-semibold uppercase tracking-widest text-brand-primaryDark">Pricing</span>
          <h2 className="mt-2 text-3xl font-bold tracking-tight text-gray-900">Simple, transparent pricing</h2>
        </Reveal>
        <Reveal as="p" className="mx-auto mt-3 max-w-xl text-center text-sm text-gray-600" delay={0.05}>
          Plans are activated by our team during onboarding.
        </Reveal>
        <Reveal as="div" className="mt-6" delay={0.1}>
          <BillingToggle value={cycle} onChange={setCycle} />
        </Reveal>
        <StaggerGroup className="mt-12 grid gap-6 sm:grid-cols-3">
          {plans.map((plan) => (
            <StaggerItem key={plan.name}>
              <PricingCard plan={plan} />
            </StaggerItem>
          ))}
        </StaggerGroup>
        <p className="mt-6 text-center text-sm">
          Need higher volume? <a href="/demo" className="font-semibold text-brand-primaryDark">Talk to us about Enterprise.</a>
        </p>
      </section>

      {/* Final CTA */}
      <section className="relative overflow-hidden bg-gradient-to-br from-gray-900 via-gray-900 to-brand-secondary py-20">
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top,_rgba(37,211,102,0.25),_transparent_60%)]"
        />
        <Reveal as="div" className="relative mx-auto max-w-4xl px-4 text-center sm:px-6 lg:px-8">
          <h2 className="text-3xl font-bold tracking-tight text-white">See SellerTalk24 on your own catalogue</h2>
          <p className="mt-4 text-gray-300">
            Book a short demo and we'll show you exactly how it works with your products.
          </p>
          <div className="mt-8 flex flex-wrap justify-center gap-4">
            <CTAButton to="/demo">Book a Demo</CTAButton>
            <CTAButton to="/pricing" variant="secondary">
              Create your account
            </CTAButton>
          </div>
        </Reveal>
      </section>
    </>
  );
}
