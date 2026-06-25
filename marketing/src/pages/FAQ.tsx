import { useState } from "react";
import SEO from "../components/SEO";
import FAQItem from "../components/FAQItem";
import Reveal from "../components/motion/Reveal";
import { StaggerGroup, StaggerItem } from "../components/motion/StaggerGroup";

const faqs = [
  {
    question: "What is AgentlyAI?",
    answer:
      "AgentlyAI is an AI sales agent for fashion retailers in India. It runs inside WhatsApp and Instagram, helping customers browse your catalogue, pick variants like size and color, and place orders — all in chat.",
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
    answer: "AgentlyAI can converse in English, Hindi, and Hinglish.",
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
    question: "Who is AgentlyAI for?",
    answer:
      "Fashion and apparel retailers in India selling over WhatsApp and Instagram — from independent boutiques to multi-store fashion brands.",
  },
];

export default function FAQ() {
  const [openQuestion, setOpenQuestion] = useState<string | null>(null);

  return (
    <>
      <SEO
        title="FAQ — AgentlyAI"
        description="Answers to common questions about AgentlyAI: channels, setup, languages, payments, security, and plans."
        path="/faq"
      />
      <section className="mx-auto max-w-3xl px-4 py-16 sm:px-6 lg:px-8">
        <Reveal as="h1" className="text-center text-4xl font-bold text-gray-900">
          Frequently asked questions
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
      </section>
    </>
  );
}
