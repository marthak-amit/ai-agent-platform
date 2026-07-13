import SEO from "../components/SEO";
import Reveal from "../components/motion/Reveal";

export default function Terms() {
  return (
    <>
      <SEO
        title="Terms & Conditions — SellerTalk24"
        description="Terms of service for SellerTalk24, an AI-assisted auto-reply and order automation platform for WhatsApp and Instagram."
        path="/terms"
      />
      <section className="mx-auto max-w-3xl px-4 py-16 sm:px-6 lg:px-8">
        <Reveal as="h1" className="text-4xl font-bold text-gray-900">
          Terms &amp; Conditions
        </Reveal>
        <p className="mt-2 text-sm text-gray-500">Last updated: [TODO: fill effective date]</p>

        <div className="prose prose-gray mt-10 max-w-none space-y-8 text-gray-700">
          <div>
            <h2 className="text-xl font-semibold text-gray-900">1. Service description</h2>
            <p className="mt-3">
              SellerTalk24 (operated by [TODO: legal entity name]) provides AI-assisted auto-reply and
              order-automation software for merchants on WhatsApp and Instagram, using Meta's Business API
              and the merchant's own connected business account. SellerTalk24 automates conversation replies
              and order collection based on the merchant's catalogue and configuration. SellerTalk24 does
              not guarantee sales, conversions, or revenue outcomes — it is a messaging automation tool, and
              results depend on the merchant's catalogue, pricing, and customer demand.
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">2. Account and onboarding</h2>
            <p className="mt-3">
              WhatsApp onboarding is currently assisted: our team configures the Meta System User token and
              connects the merchant's WhatsApp Business Account on their behalf, rather than through instant
              self-serve connect. Instagram connection may be self-serve subject to Meta's own app review and
              permission grants. Merchants are responsible for maintaining accurate business information and
              for complying with Meta's own platform policies for WhatsApp and Instagram.
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">3. Subscription &amp; billing</h2>
            <p className="mt-3">
              Plans are billed on a recurring basis (monthly or yearly, as selected) at the price shown on
              our{" "}
              <a href="/pricing" className="text-brand-primaryDark underline">
                Pricing page
              </a>{" "}
              at the time of purchase. [TODO: fill payment provider/billing mechanics — e.g. Razorpay
              subscription, auto-renewal, invoicing cadence.] Plan changes or upgrades are currently
              activated manually by our team during onboarding. We charge no markup or hidden fee on top of
              Meta's own messaging costs; any Meta conversation-based charges are separate and billed by
              Meta or passed through at cost, with 0% markup from SellerTalk24.
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">4. Acceptable use</h2>
            <p className="mt-3">
              Merchants may not use SellerTalk24 to send unsolicited bulk messages, spam, or content that
              violates Meta's WhatsApp Business Messaging Policy or Instagram Platform Policy, or to sell
              prohibited, counterfeit, or illegal goods. We may suspend or terminate accounts that violate
              this section or that cause Meta to flag, restrict, or ban the connected business account.
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">5. Limitation of liability</h2>
            <p className="mt-3">
              SellerTalk24 is provided on an "as is" basis. We do not guarantee uninterrupted availability
              of WhatsApp or Instagram messaging, which depends on Meta's platforms and infrastructure
              outside our control. To the maximum extent permitted by law, our liability for any claim
              arising from use of the service is limited to the subscription fees paid by the merchant in
              the [TODO: fill period, e.g. preceding 3 months]. We are not liable for indirect, incidental,
              or consequential damages, including lost sales or lost profits.
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">6. Termination</h2>
            <p className="mt-3">
              Merchants may cancel their subscription at any time; see our{" "}
              <a href="/refund-policy" className="text-brand-primaryDark underline">
                Refund &amp; Cancellation Policy
              </a>{" "}
              for details. We may suspend or terminate an account for non-payment, breach of these terms, or
              violation of Meta's platform policies, with notice where practicable.
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">7. Governing law</h2>
            <p className="mt-3">
              [TODO: fill governing law/jurisdiction, e.g. laws of India, courts of [city].]
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">8. Contact</h2>
            <p className="mt-3">
              Questions about these terms can be sent via our{" "}
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
