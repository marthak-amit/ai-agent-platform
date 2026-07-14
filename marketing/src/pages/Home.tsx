import { useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import SEO from "../components/SEO";
import CTAButton from "../components/CTAButton";
import ChatMockup from "../components/ChatMockup";
import FeatureCard from "../components/FeatureCard";
import FAQItem from "../components/FAQItem";
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

const featureGroups = [
  {
    title: "Selling",
    items: [
      { title: "Catalogue browsing in chat", description: "Customers explore your products without leaving WhatsApp or Instagram." },
      { title: "Variant selection", description: "Color, size, and material are handled conversationally, just like talking to a shop assistant." },
      { title: "Full in-chat order collection", description: "Quantity, address, and order details captured directly in the conversation." },
      { title: "Flexible payments", description: "COD, UPI, and bank transfer — the payment methods Indian shoppers already use." },
    ],
  },
  {
    title: "Operations",
    items: [
      { title: "Order status tracking", description: "Customers can check where their order is, without you lifting a finger." },
      { title: "Dispatch notifications", description: "Automatic updates the moment an order ships." },
      { title: "Daily owner briefing", description: "A daily email summarizing new orders, leads, and conversations." },
    ],
  },
  {
    title: "Growth",
    items: [
      { title: "Follow-ups & re-engagement", description: "Customers who browsed but didn't order get a gentle nudge back." },
      { title: "WhatsApp broadcast campaigns", description: "Announce new arrivals or offers to your customer list." },
      { title: "Multi-channel reach", description: "One catalogue, sold consistently across WhatsApp and Instagram." },
    ],
  },
  {
    title: "Control",
    items: [
      { title: "Human takeover", description: "Step into any conversation when a customer needs a real person." },
      { title: "Abuse protection", description: "Built-in safeguards keep conversations on-topic and protect against misuse, without you needing to configure anything." },
    ],
  },
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

const faqs = [
  {
    question: "What is SellerTalk24?",
    answer:
      "SellerTalk24 is an AI sales agent for fashion retailers in India. It runs inside WhatsApp and Instagram, helping customers browse your catalogue, pick variants like size and color, and place orders — all in chat.",
  },
  {
    question: "Which channels does it support?",
    answer:
      "WhatsApp and Instagram are both live today. A website chat widget is also available on the Pro plan for stores that want chat on their own site.",
  },
  {
    question: "How does setup work?",
    answer:
      "WhatsApp onboarding is assisted — our team sets up WhatsApp for you by configuring a Meta System User token on your behalf, rather than an instant self-serve connect button. We'll guide you through it during onboarding.",
  },
  {
    question: "What languages does it support?",
    answer: "SellerTalk24 can converse in English, Hindi, and Hinglish.",
  },
  {
    question: "What payment methods are supported?",
    answer: "Cash on delivery (COD), UPI, and bank transfer — set up per your preferences.",
  },
  {
    question: "Is my data secure?",
    answer:
      "Conversations and order data are stored securely and scoped to your account only. We don't share your catalogue or customer data across merchants.",
  },
  {
    question: "How do plans get activated?",
    answer:
      "Plan upgrades are currently activated manually by our team during onboarding rather than self-serve billing — book a demo and we'll get you set up on the right plan.",
  },
  {
    question: "Who is SellerTalk24 for?",
    answer:
      "Fashion and apparel retailers in India selling over WhatsApp and Instagram — from independent boutiques to multi-store fashion brands.",
  },
];

export default function Home() {
  const [cycle, setCycle] = useState<BillingCycle>("monthly");
  const plans = cycle === "monthly" ? monthlyPlans : yearlyPlans;
  const [openQuestion, setOpenQuestion] = useState<string | null>(null);
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

      {/* Features */}
      <section id="features" className="scroll-mt-24 bg-gray-50 py-20">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <Reveal as="div" className="text-center">
            <span className="text-xs font-semibold uppercase tracking-widest text-brand-primaryDark">Features</span>
            <h2 className="mt-2 text-3xl font-bold tracking-tight text-gray-900">Everything SellerTalk24 does</h2>
            <p className="mx-auto mt-4 max-w-xl text-gray-600">
              Built specifically for how fashion retail actually sells in India.
            </p>
          </Reveal>

          <div className="mt-12 space-y-12">
            {featureGroups.map((group) => (
              <div key={group.title}>
                <h3 className="text-xl font-semibold text-gray-900">{group.title}</h3>
                <StaggerGroup className="mt-6 grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
                  {group.items.map((item) => (
                    <StaggerItem key={item.title}>
                      <FeatureCard title={item.title} description={item.description} />
                    </StaggerItem>
                  ))}
                </StaggerGroup>
              </div>
            ))}
          </div>
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

      {/* Industries */}
      <section id="industries" className="scroll-mt-24 bg-gradient-to-br from-brand-primary/5 via-white to-brand-primary/5 py-20">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <Reveal as="div" className="text-center">
            <span className="text-xs font-semibold uppercase tracking-widest text-brand-primaryDark">Industries</span>
            <h2 className="mt-2 text-3xl font-bold tracking-tight text-gray-900">Built for fashion retail, first</h2>
            <p className="mx-auto mt-4 max-w-2xl text-gray-600">
              SellerTalk24 is purpose-built for how Indian fashion brands sell — variant-heavy
              catalogues, WhatsApp-first customers, and UPI/COD payments.
            </p>
          </Reveal>

          <Reveal as="div" className="mt-12 rounded-3xl bg-white p-8 ring-2 ring-brand-primary" delay={0.05}>
            <h3 className="text-2xl font-bold text-gray-900">Fashion &amp; Apparel</h3>
            <p className="mt-3 text-gray-700">
              Sarees, kurtis, lehengas, suits, and more. SellerTalk24 understands color, size, and
              fabric variants and helps customers pick exactly what they want — over chat, the way
              they already shop.
            </p>
            <CTAButton to="/demo" className="mt-6">
              Book a Demo
            </CTAButton>
          </Reveal>

          <StaggerGroup className="mt-8 grid gap-6 sm:grid-cols-2">
            <StaggerItem>
              <div className="relative rounded-3xl bg-white p-8 opacity-70 ring-1 ring-gray-900/5">
                <span className="absolute right-4 top-4 rounded-full bg-gray-100 px-3 py-1 text-xs font-semibold text-gray-600">
                  Coming soon
                </span>
                <h3 className="text-xl font-semibold text-gray-900">Jewelry</h3>
                <p className="mt-2 text-sm text-gray-600">Not yet supported — on our roadmap.</p>
              </div>
            </StaggerItem>
            <StaggerItem>
              <div className="relative rounded-3xl bg-white p-8 opacity-70 ring-1 ring-gray-900/5">
                <span className="absolute right-4 top-4 rounded-full bg-gray-100 px-3 py-1 text-xs font-semibold text-gray-600">
                  Coming soon
                </span>
                <h3 className="text-xl font-semibold text-gray-900">Accessories</h3>
                <p className="mt-2 text-sm text-gray-600">Not yet supported — on our roadmap.</p>
              </div>
            </StaggerItem>
          </StaggerGroup>
        </div>
      </section>

      {/* Pricing */}
      <section id="pricing" className="scroll-mt-24 mx-auto max-w-7xl px-4 py-20 sm:px-6 lg:px-8">
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

        <div className="mt-10 rounded-2xl border border-gray-200 bg-gray-50 p-8 text-center">
          <h3 className="text-xl font-semibold text-gray-900">Enterprise</h3>
          <p className="mt-2 text-gray-600">Custom message volume, dedicated onboarding, and priority support.</p>
          <CTAButton to="/demo" className="mt-6">
            Let's talk
          </CTAButton>
        </div>

        <p className="mt-8 text-center text-sm text-gray-500">
          Billing is currently set up manually by our team — plan upgrades are not yet self-serve.
        </p>
      </section>

      {/* FAQ */}
      <section id="faq" className="scroll-mt-24 bg-gray-50 py-20">
        <div className="mx-auto max-w-3xl px-4 sm:px-6 lg:px-8">
          <Reveal as="div" className="text-center">
            <span className="text-xs font-semibold uppercase tracking-widest text-brand-primaryDark">FAQ</span>
            <h2 className="mt-2 text-3xl font-bold tracking-tight text-gray-900">Frequently asked questions</h2>
          </Reveal>
          <StaggerGroup className="mt-10">
            {faqs.map((faq) => (
              <StaggerItem key={faq.question}>
                <FAQItem
                  question={faq.question}
                  answer={faq.answer}
                  open={openQuestion === faq.question}
                  onToggle={() =>
                    setOpenQuestion((current) => (current === faq.question ? null : faq.question))
                  }
                />
              </StaggerItem>
            ))}
          </StaggerGroup>
        </div>
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
            <CTAButton to="/#pricing" variant="secondary">
              Create your account
            </CTAButton>
          </div>
        </Reveal>
      </section>
    </>
  );
}
