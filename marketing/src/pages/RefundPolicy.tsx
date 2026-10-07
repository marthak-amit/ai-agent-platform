import SEO from "../components/SEO";
import Reveal from "../components/motion/Reveal";

export default function RefundPolicy() {
  return (
    <>
      <SEO
        title="Refund & Cancellation Policy — SellerTalk24"
        description="Refund and cancellation terms for SellerTalk24 subscription plans."
        path="/refund-policy"
      />
      <section className="mx-auto max-w-3xl px-4 py-16 sm:px-6 lg:px-8">
        <Reveal as="h1" className="text-4xl font-bold text-gray-900">
          Refund &amp; Cancellation Policy
        </Reveal>
        <p className="mt-2 text-sm text-gray-500">Last updated: [TODO: fill effective date]</p>

        <div className="prose prose-gray mt-10 max-w-none space-y-8 text-gray-700">
          <div>
            <h2 className="text-xl font-semibold text-gray-900">1. Subscription fees are non-refundable</h2>
            <p className="mt-3">
              SellerTalk24 plans are prepaid for a 30-day period and the fee is non-refundable once
              charged, including for any unused portion of the period. [TODO: legal review of this
              wording.]
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">2. Cancellation</h2>
            <p className="mt-3">
              Plans do not renew automatically, so there is nothing to cancel to avoid a future charge —
              your plan simply ends when the paid period ends unless you renew it. You retain access to
              the service through the end of the period already paid for. For any other cancellation
              request, contact us via our{" "}
              <a href="/contact" className="text-brand-primaryDark underline">
                Contact page
              </a>
              .
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">3. Exceptions</h2>
            <p className="mt-3">
              [TODO: fill any exceptions to the no-refund rule, e.g. duplicate charges, billing errors, or
              service outages, if applicable.]
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">4. Contact</h2>
            <p className="mt-3">
              For billing questions, reach us via our{" "}
              <a href="/contact" className="text-brand-primaryDark underline">
                Contact page
              </a>
              .
            </p>
          </div>
        </div>
      </section>
    </>
  );
}
