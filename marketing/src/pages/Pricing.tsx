import { useState } from "react";
import SEO from "../components/SEO";
import PricingCard, { type PricingPlan } from "../components/PricingCard";
import CTAButton from "../components/CTAButton";
import BillingToggle, { type BillingCycle } from "../components/BillingToggle";
import Reveal from "../components/motion/Reveal";
import { StaggerGroup, StaggerItem } from "../components/motion/StaggerGroup";

const monthlyPlans: PricingPlan[] = [
  { name: "Starter", price: "₹999", cadence: "/mo", limit: "Up to 100 messages/day", channels: "WhatsApp" },
  { name: "Growth", price: "₹1,999", cadence: "/mo", limit: "Up to 300 messages/day", channels: "WhatsApp + Instagram", highlighted: true },
  { name: "Pro", price: "₹3,999", cadence: "/mo", limit: "Up to 700 messages/day", channels: "WhatsApp + Instagram + Website widget" },
];

const yearlyPlans: PricingPlan[] = monthlyPlans.map((plan) => ({
  ...plan,
  price: "Contact us",
  cadence: undefined,
}));

export default function Pricing() {
  const [cycle, setCycle] = useState<BillingCycle>("monthly");
  const plans = cycle === "monthly" ? monthlyPlans : yearlyPlans;

  return (
    <>
      <SEO
        title="Pricing — AgentlyAI"
        description="Simple, transparent pricing for AgentlyAI's WhatsApp & Instagram AI sales agent for fashion retailers in India."
        path="/pricing"
      />
      <section className="mx-auto max-w-7xl px-4 py-16 sm:px-6 lg:px-8">
        <Reveal as="div" className="text-center">
          <h1 className="text-4xl font-bold text-gray-900">Simple, transparent pricing</h1>
          <p className="mx-auto mt-4 max-w-xl text-gray-600">
            Monthly pricing, no annual lock-in discount to fake — what you see is what you pay.
            Plans are activated by our team during onboarding.
          </p>
        </Reveal>

        <Reveal as="div" className="mt-8" delay={0.1}>
          <BillingToggle value={cycle} onChange={setCycle} />
        </Reveal>

        <StaggerGroup className="mt-10 grid gap-6 sm:grid-cols-3">
          {plans.map((plan) => (
            <StaggerItem key={plan.name}>
              <PricingCard plan={plan} />
            </StaggerItem>
          ))}
        </StaggerGroup>

        <div className="mt-10 rounded-2xl border border-gray-200 bg-gray-50 p-8 text-center">
          <h2 className="text-xl font-semibold text-gray-900">Enterprise</h2>
          <p className="mt-2 text-gray-600">Custom message volume, dedicated onboarding, and priority support.</p>
          <CTAButton to="/demo" className="mt-6">
            Let's talk
          </CTAButton>
        </div>

        <p className="mt-8 text-center text-sm text-gray-500">
          Billing is currently set up manually by our team — plan upgrades are not yet self-serve.
        </p>
      </section>
    </>
  );
}
