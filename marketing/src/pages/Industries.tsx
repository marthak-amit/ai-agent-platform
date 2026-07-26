import { Link } from "react-router-dom";
import SEO from "../components/SEO";
import CTAButton from "../components/CTAButton";
import PageCrossLinks from "../components/PageCrossLinks";
import Reveal from "../components/motion/Reveal";
import { StaggerGroup, StaggerItem } from "../components/motion/StaggerGroup";

export default function Industries() {
  return (
    <>
      <SEO
        title="Industries — AI Sales Agent for Fashion Retail in India | SellerTalk24"
        description="SellerTalk24 is purpose-built for fashion & apparel retailers in India — variant-heavy catalogues, WhatsApp-first customers, and UPI/COD payments. Jewelry and accessories are on the roadmap."
        path="/industries"
      />

      <section className="mx-auto max-w-4xl px-4 pb-4 pt-16 text-center sm:px-6 lg:px-8">
        <Reveal as="h1" className="text-4xl font-bold tracking-tight text-gray-900 sm:text-5xl">
          Built for fashion retail, first
        </Reveal>
        <Reveal as="p" className="mx-auto mt-4 max-w-2xl text-lg text-gray-600" delay={0.05}>
          SellerTalk24 is purpose-built for how Indian fashion brands sell — variant-heavy
          catalogues, WhatsApp-first customers, and UPI/COD payments. See the full{" "}
          <Link to="/features" className="font-semibold text-brand-primaryDark underline">
            feature set
          </Link>{" "}
          this is built on.
        </Reveal>
      </section>

      <section className="mx-auto max-w-7xl px-4 py-12 sm:px-6 lg:px-8">
        <Reveal as="div" className="rounded-3xl bg-white p-8 ring-2 ring-brand-primary sm:p-10">
          <span className="inline-flex items-center rounded-full bg-brand-primary/10 px-3 py-1 text-xs font-semibold text-brand-primaryDark ring-1 ring-inset ring-brand-primary/20">
            Live today
          </span>
          <h2 className="mt-4 text-2xl font-bold text-gray-900 sm:text-3xl">Fashion &amp; Apparel</h2>
          <p className="mt-3 max-w-2xl text-gray-700">
            Sarees, kurtis, lehengas, suits, and more. SellerTalk24 understands color, size, and
            fabric variants and helps customers pick exactly what they want — over chat, the way
            they already shop. Independent boutiques and multi-store fashion brands both run on
            the same catalogue-driven conversation flow.
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
      </section>

      <section className="mx-auto max-w-3xl px-4 pb-16 text-center sm:px-6 lg:px-8">
        <p className="text-gray-600">
          Check{" "}
          <Link to="/pricing" className="font-semibold text-brand-primaryDark underline">
            pricing
          </Link>{" "}
          for plans by channel, or read the{" "}
          <Link to="/faq" className="font-semibold text-brand-primaryDark underline">
            FAQ
          </Link>{" "}
          for setup and language details.
        </p>
      </section>

      <PageCrossLinks current="industries" />
    </>
  );
}
