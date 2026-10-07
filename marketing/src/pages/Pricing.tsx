import { Link } from "react-router-dom";
import SEO, { SITE_URL } from "../components/SEO";
import CTAButton from "../components/CTAButton";
import PricingCard from "../components/PricingCard";
import PageCrossLinks from "../components/PageCrossLinks";
import Reveal from "../components/motion/Reveal";
import { StaggerGroup, StaggerItem } from "../components/motion/StaggerGroup";
import { plans } from "../content/site";
import { GST_LINE, PLAN_SPECS, PRICES_INCLUDE_GST, PRICE_SUMMARY } from "../content/plans";

const productJsonLd = {
  "@context": "https://schema.org",
  "@type": "Product",
  name: "SellerTalk24",
  description:
    "AI sales agent for WhatsApp, Instagram, and website chat — built for fashion retailers in India.",
  brand: { "@type": "Brand", name: "SellerTalk24" },
  // Enterprise is "Contact us" (no price), so it is intentionally not an Offer.
  offers: PLAN_SPECS.map((plan) => ({
    "@type": "Offer",
    name: `${plan.name} — ${plan.conversationsPerMonth} conversations/month`,
    sku: plan.code,
    price: String(plan.priceInr),
    priceCurrency: "INR",
    priceSpecification: {
      "@type": "PriceSpecification",
      price: String(plan.priceInr),
      priceCurrency: "INR",
      valueAddedTaxIncluded: PRICES_INCLUDE_GST,
    },
    url: `${SITE_URL}/pricing`,
    availability: "https://schema.org/InStock",
  })),
};

export default function Pricing() {
  return (
    <>
      <SEO
        title="Pricing — WhatsApp & Instagram AI Sales Agent Plans | SellerTalk24"
        description={`Simple monthly pricing for SellerTalk24's WhatsApp & Instagram AI sales agent: ${PRICE_SUMMARY} per month. ${GST_LINE}. Built for fashion retailers in India.`}
        path="/pricing"
        jsonLd={productJsonLd}
      />

      <section className="mx-auto max-w-4xl px-4 pb-4 pt-16 text-center sm:px-6 lg:px-8">
        <Reveal as="h1" className="text-4xl font-bold tracking-tight text-gray-900 sm:text-5xl">
          Simple, transparent pricing
        </Reveal>
        <Reveal as="p" className="mx-auto mt-4 max-w-2xl text-lg text-gray-600" delay={0.05}>
          Every plan includes the full{" "}
          <Link to="/features" className="font-semibold text-brand-primaryDark underline">
            order flow
          </Link>{" "}
          — cart, address, and confirmation — over the channels you need. Pick a plan, pay securely
          online, and your AI sales agent is live as soon as the payment goes through.
        </Reveal>
      </section>

      <section className="mx-auto max-w-7xl px-4 py-12 sm:px-6 lg:px-8">
        <h2 className="text-center text-2xl font-bold text-gray-900">Choose your plan</h2>
        <p className="mt-2 text-center text-sm text-gray-500">{GST_LINE}</p>
        <StaggerGroup className="mt-8 grid gap-6 sm:grid-cols-3">
          {plans.map((plan) => (
            <StaggerItem key={plan.name}>
              <PricingCard plan={plan} />
            </StaggerItem>
          ))}
        </StaggerGroup>

        <div className="mt-6 rounded-2xl border border-gray-200 bg-gray-50 p-8 text-center">
          <h2 className="text-xl font-semibold text-gray-900">Enterprise</h2>
          <p className="mt-1 text-2xl font-bold text-gray-900">Contact us</p>
          <p className="mt-2 text-gray-600">Custom message volume, dedicated onboarding, and priority support.</p>
          <CTAButton to="/demo" className="mt-6">
            Let's talk
          </CTAButton>
        </div>

        <p className="mt-6 text-center text-sm text-gray-500">
          Plans are prepaid for 30 days and renewed manually — no auto-debit. Payments by UPI, cards,
          netbanking and wallets via Razorpay. A conversation is one customer within a 24-hour window,
          with unlimited messages. See our{" "}
          <Link to="/refund-policy" className="font-semibold text-brand-primaryDark underline">
            refund policy
          </Link>{" "}
          and{" "}
          <Link to="/faq" className="font-semibold text-brand-primaryDark underline">
            FAQ
          </Link>
          .
        </p>
      </section>

      <section className="mx-auto max-w-3xl px-4 pb-16 text-center sm:px-6 lg:px-8">
        <p className="text-gray-600">
          Not sure which plan fits? See if SellerTalk24 fits your{" "}
          <Link to="/industries" className="font-semibold text-brand-primaryDark underline">
            industry
          </Link>{" "}
          or check the{" "}
          <Link to="/faq" className="font-semibold text-brand-primaryDark underline">
            FAQ
          </Link>{" "}
          for billing and setup questions.
        </p>
      </section>

      <PageCrossLinks current="pricing" />
    </>
  );
}
