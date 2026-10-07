import SEO from "../components/SEO";
import Reveal from "../components/motion/Reveal";

export default function ShippingPolicy() {
  return (
    <>
      <SEO
        title="Shipping & Delivery Policy — SellerTalk24"
        description="SellerTalk24 is a digital software service: nothing is shipped, and your plan is activated online as soon as payment is confirmed."
        path="/shipping-policy"
      />
      <section className="mx-auto max-w-3xl px-4 py-16 sm:px-6 lg:px-8">
        <Reveal as="h1" className="text-4xl font-bold text-gray-900">
          Shipping &amp; Delivery Policy
        </Reveal>
        <p className="mt-2 text-sm text-gray-500">Last updated: [TODO: fill effective date]</p>

        <div className="prose prose-gray mt-10 max-w-none space-y-8 text-gray-700">
          <div>
            <h2 className="text-xl font-semibold text-gray-900">1. Digital service — no physical shipping</h2>
            <p className="mt-3">
              SellerTalk24 is a digital software service (an AI sales agent for WhatsApp and Instagram).
              We do not sell, ship, or deliver any physical goods, so there are no shipping charges,
              courier partners, or delivery timelines.
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">2. How your plan is delivered</h2>
            <p className="mt-3">
              Your plan is delivered electronically. Once your payment is confirmed, the plan is activated
              on your SellerTalk24 dashboard and you can use it straight away. A payment receipt is
              available from your dashboard under Billing. [TODO: confirm whether a GST invoice is also
              emailed, and from which address.]
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">3. If your plan doesn't activate</h2>
            <p className="mt-3">
              Activation normally takes seconds, but can occasionally take a few minutes while the payment
              is confirmed by our payment partner. If money was deducted and your plan is still not active
              after [TODO: fill time, e.g. 30 minutes], please do not pay again — contact us via our{" "}
              <a href="/contact" className="text-brand-primaryDark underline">
                Contact page
              </a>{" "}
              with your payment ID and we will resolve it. See also our{" "}
              <a href="/refund-policy" className="text-brand-primaryDark underline">
                Refund &amp; Cancellation Policy
              </a>
              .
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">4. Contact</h2>
            <p className="mt-3">
              Questions about delivery of your plan? Reach us via our{" "}
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
