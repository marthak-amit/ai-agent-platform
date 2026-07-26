import type { PricingPlan } from "../components/PricingCard";

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

export const monthlyPlans: PricingPlan[] = [
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

export const yearlyPlans: PricingPlan[] = monthlyPlans.map((plan) => ({
  ...plan,
  price: "Contact us",
  cadence: undefined,
}));

export const faqs = [
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
