import { Link } from "react-router-dom";

type PageKey = "features" | "industries" | "pricing" | "faq";

const pages: Record<PageKey, { to: string; label: string; description: string }> = {
  features: {
    to: "/features",
    label: "Features",
    description: "Everything SellerTalk24 does — selling, operations, growth, and control.",
  },
  industries: {
    to: "/industries",
    label: "Industries",
    description: "Built first for fashion & apparel retailers in India.",
  },
  pricing: {
    to: "/pricing",
    label: "Pricing",
    description: "Simple, transparent monthly plans for every stage of growth.",
  },
  faq: {
    to: "/faq",
    label: "FAQ",
    description: "Answers about channels, setup, languages, payments, and security.",
  },
};

interface PageCrossLinksProps {
  current: PageKey;
  heading?: string;
}

/** Renders links to the other three content pages, for internal linking in body copy. */
export default function PageCrossLinks({ current, heading = "Explore more" }: PageCrossLinksProps) {
  const others = (Object.keys(pages) as PageKey[]).filter((key) => key !== current);

  return (
    <section className="mx-auto max-w-7xl px-4 pb-20 sm:px-6 lg:px-8">
      <h2 className="text-center text-sm font-semibold uppercase tracking-widest text-brand-primaryDark">
        {heading}
      </h2>
      <div className="mt-6 grid gap-6 sm:grid-cols-3">
        {others.map((key) => {
          const page = pages[key];
          return (
            <Link
              key={key}
              to={page.to}
              className="rounded-2xl bg-white p-6 shadow-sm shadow-gray-900/5 ring-1 ring-gray-900/5 transition-all hover:-translate-y-1 hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-primary"
            >
              <h3 className="text-base font-semibold text-gray-900">{page.label}</h3>
              <p className="mt-2 text-sm text-gray-600">{page.description}</p>
            </Link>
          );
        })}
      </div>
    </section>
  );
}
