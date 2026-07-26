import { Link } from "react-router-dom";
import SEO from "../components/SEO";
import CTAButton from "../components/CTAButton";
import FeatureCard from "../components/FeatureCard";
import PageCrossLinks from "../components/PageCrossLinks";
import Reveal from "../components/motion/Reveal";
import { StaggerGroup, StaggerItem } from "../components/motion/StaggerGroup";
import { featureGroups } from "../content/site";

export default function Features() {
  return (
    <>
      <SEO
        title="Features — AI Sales Agent for WhatsApp & Instagram Selling | SellerTalk24"
        description="See everything SellerTalk24's AI sales agent does: catalogue browsing, variant selection, in-chat order collection, dispatch tracking, and WhatsApp broadcast campaigns for fashion retailers in India."
        path="/features"
      />

      <section className="mx-auto max-w-4xl px-4 pb-4 pt-16 text-center sm:px-6 lg:px-8">
        <Reveal as="h1" className="text-4xl font-bold tracking-tight text-gray-900 sm:text-5xl">
          Everything SellerTalk24 does
        </Reveal>
        <Reveal as="p" className="mx-auto mt-4 max-w-2xl text-lg text-gray-600" delay={0.05}>
          SellerTalk24 is an AI sales agent that runs inside WhatsApp and Instagram, built
          specifically for how fashion retail actually sells in India — from the first message to
          a dispatched order. Every feature below is included as part of your{" "}
          <Link to="/pricing" className="font-semibold text-brand-primaryDark underline">
            plan
          </Link>
          , with no separate add-ons to configure.
        </Reveal>
      </section>

      <section className="mx-auto max-w-7xl px-4 py-12 sm:px-6 lg:px-8">
        <div className="space-y-14">
          {featureGroups.map((group) => (
            <div key={group.title}>
              <h2 className="text-2xl font-bold text-gray-900">{group.title}</h2>
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
      </section>

      <section className="mx-auto max-w-3xl px-4 pb-16 text-center sm:px-6 lg:px-8">
        <p className="text-gray-600">
          Every plan runs on WhatsApp, Instagram, or both — see{" "}
          <Link to="/pricing" className="font-semibold text-brand-primaryDark underline">
            pricing
          </Link>{" "}
          for channel availability, or check whether SellerTalk24 fits your{" "}
          <Link to="/industries" className="font-semibold text-brand-primaryDark underline">
            industry
          </Link>
          .
        </p>
        <CTAButton to="/demo" className="mt-8">
          Book a Demo
        </CTAButton>
      </section>

      <PageCrossLinks current="features" />
    </>
  );
}
