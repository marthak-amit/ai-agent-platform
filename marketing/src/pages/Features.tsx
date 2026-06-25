import SEO from "../components/SEO";
import FeatureCard from "../components/FeatureCard";

const groups = [
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

export default function Features() {
  return (
    <>
      <SEO
        title="Features — AgentlyAI"
        description="Everything AgentlyAI's AI sales agent does for fashion retailers: selling, operations, growth, and control."
        path="/features"
      />
      <section className="mx-auto max-w-7xl px-4 py-16 sm:px-6 lg:px-8">
        <div className="text-center">
          <h1 className="text-4xl font-bold text-gray-900">Everything AgentlyAI does</h1>
          <p className="mx-auto mt-4 max-w-xl text-gray-600">
            Built specifically for how fashion retail actually sells in India.
          </p>
        </div>

        <div className="mt-12 space-y-12">
          {groups.map((group) => (
            <div key={group.title}>
              <h2 className="text-2xl font-semibold text-gray-900">{group.title}</h2>
              <div className="mt-6 grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
                {group.items.map((item) => (
                  <FeatureCard key={item.title} title={item.title} description={item.description} />
                ))}
              </div>
            </div>
          ))}
        </div>
      </section>
    </>
  );
}
