import type { PricingPlan } from "../components/PricingCard";
import { GST_LINE_SHORT, PLAN_SPECS, formatInr, perConversation } from "./plans";

export const steps = [
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

export const featureGroups = [
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

const BASE_FEATURES = ["Order flow (cart → address → confirm)"];

export const plans: PricingPlan[] = PLAN_SPECS.map((spec) => ({
  code: spec.code,
  name: spec.name,
  price: formatInr(spec.priceInr),
  cadence: "/month",
  priceNote: GST_LINE_SHORT,
  limit: `${spec.conversationsPerMonth.toLocaleString("en-IN")} conversations/month`,
  perConversation: `≈ ${perConversation(spec)} per conversation`,
  channels: spec.instagram ? "WhatsApp + Instagram" : "WhatsApp",
  badges: spec.instagram ? ["WA", "IG"] : ["WA"],
  features: [
    ...BASE_FEATURES,
    ...(spec.name !== "Starter" ? ["Broadcast / marketing templates"] : []),
    ...(spec.name === "Pro" ? ["Priority support"] : []),
  ],
  highlighted: spec.popular,
}));

export interface Faq {
  question: string;
  answer: string;
  /** Optional in-site link rendered after the answer (the JSON-LD answer stays plain text). */
  link?: { to: string; label: string };
}

export const faqs: Faq[] = [
  {
    question: "What is SellerTalk24?",
    answer:
      "SellerTalk24 is an AI sales agent for fashion retailers in India. It runs inside WhatsApp and Instagram, helping customers browse your catalogue, pick variants like size and color, and place orders — all in chat.",
  },
  {
    question: "Which channels does it support?",
    answer:
      "WhatsApp is included in every plan, and Instagram is included in Growth and Pro. A website chat widget is also available — ask us during onboarding if you'd like chat on your own site.",
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
    question: "Which payment methods can my customers use?",
    answer: "Cash on delivery (COD), UPI, and bank transfer — set up per your preferences.",
  },
  {
    question: "Is my data secure?",
    answer:
      "Conversations and order data are stored securely and scoped to your account only. We don't share your catalogue or customer data across merchants.",
  },
  {
    question: "How does billing work?",
    answer:
      "Plans are prepaid for 30 days and you pay from your dashboard — the plan activates as soon as the payment goes through. We don't auto-debit your account: you renew manually, and we remind you before your plan expires. You can upgrade at any time and get credit for the unused part of your current plan; moving to a smaller plan takes effect after your current period ends.",
  },
  {
    question: "What is a conversation?",
    answer:
      "A conversation is one customer chatting with your AI agent on one channel within a 24-hour window. Inside that window the number of messages is unlimited — a shopper who sends 50 messages still counts as one conversation. If the same shopper comes back after the 24 hours are over, that starts a new conversation.",
  },
  {
    question: "What if I exceed my limit?",
    answer:
      "Your AI sales agent keeps working — we never switch it off mid-month because you went over. We'll show you alerts in your dashboard as you approach and pass your limit, and nudge you to upgrade so you stay within your plan next cycle.",
  },
  {
    question: "What payment methods can I use to pay for my plan?",
    answer:
      "You can pay with UPI, credit and debit cards, netbanking, and popular wallets. Payments are processed securely by Razorpay — we never see or store your card details.",
  },
  {
    question: "What is your refund policy?",
    answer:
      "Plans are prepaid for 30 days and are generally non-refundable once charged. The full terms, including cancellation, are in our Refund & Cancellation Policy.",
    link: { to: "/refund-policy", label: "Read the Refund & Cancellation Policy" },
  },
  {
    question: "Who is SellerTalk24 for?",
    answer:
      "Fashion and apparel retailers in India selling over WhatsApp and Instagram — from independent boutiques to multi-store fashion brands.",
  },
];
