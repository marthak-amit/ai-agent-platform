import { useState } from "react";
import { Link } from "react-router-dom";
import SEO from "../components/SEO";
import CTAButton from "../components/CTAButton";
import FAQItem from "../components/FAQItem";
import PageCrossLinks from "../components/PageCrossLinks";
import Reveal from "../components/motion/Reveal";
import { StaggerGroup, StaggerItem } from "../components/motion/StaggerGroup";
import { faqs } from "../content/site";

const faqJsonLd = {
  "@context": "https://schema.org",
  "@type": "FAQPage",
  mainEntity: faqs.map((faq) => ({
    "@type": "Question",
    name: faq.question,
    acceptedAnswer: {
      "@type": "Answer",
      text: faq.answer,
    },
  })),
};

export default function Faq() {
  const [openQuestion, setOpenQuestion] = useState<string | null>(null);

  return (
    <>
      <SEO
        title="FAQ — WhatsApp & Instagram AI Sales Agent Questions | SellerTalk24"
        description="Answers to common questions about SellerTalk24: supported channels, WhatsApp onboarding, languages, payment methods, data security, and how plans get activated."
        path="/faq"
        jsonLd={faqJsonLd}
      />

      <section className="mx-auto max-w-3xl px-4 pb-4 pt-16 text-center sm:px-6 lg:px-8">
        <Reveal as="h1" className="text-4xl font-bold tracking-tight text-gray-900 sm:text-5xl">
          Frequently asked questions
        </Reveal>
        <Reveal as="p" className="mx-auto mt-4 max-w-xl text-lg text-gray-600" delay={0.05}>
          Everything about channels, setup, languages, payments, and security. Looking for plan
          details instead? See{" "}
          <Link to="/pricing" className="font-semibold text-brand-primaryDark underline">
            pricing
          </Link>
          .
        </Reveal>
      </section>

      <section className="mx-auto max-w-3xl px-4 py-12 sm:px-6 lg:px-8">
        <StaggerGroup>
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
      </section>

      <section className="mx-auto max-w-3xl px-4 pb-16 text-center sm:px-6 lg:px-8">
        <p className="text-gray-600">
          Still have questions? See the full{" "}
          <Link to="/features" className="font-semibold text-brand-primaryDark underline">
            feature list
          </Link>
          , check{" "}
          <Link to="/industries" className="font-semibold text-brand-primaryDark underline">
            supported industries
          </Link>
          , or reach out directly.
        </p>
        <CTAButton to="/demo" className="mt-8">
          Book a Demo
        </CTAButton>
      </section>

      <PageCrossLinks current="faq" />
    </>
  );
}
