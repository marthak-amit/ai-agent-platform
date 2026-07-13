import SEO from "../components/SEO";
import Reveal from "../components/motion/Reveal";

export default function Privacy() {
  return (
    <>
      <SEO
        title="Privacy Policy — SellerTalk24"
        description="How SellerTalk24 collects, uses, and protects data from WhatsApp and Instagram conversations via the Meta Business API."
        path="/privacy"
      />
      <section className="mx-auto max-w-3xl px-4 py-16 sm:px-6 lg:px-8">
        <Reveal as="h1" className="text-4xl font-bold text-gray-900">
          Privacy Policy
        </Reveal>
        <p className="mt-2 text-sm text-gray-500">Last updated: [TODO: fill effective date]</p>

        <div className="prose prose-gray mt-10 max-w-none space-y-8 text-gray-700">
          <div>
            <h2 className="text-xl font-semibold text-gray-900">1. Who we are</h2>
            <p className="mt-3">
              SellerTalk24 ("we", "us", "our") is operated by [TODO: legal entity name], registered at
              [TODO: registered business address]. SellerTalk24 provides AI-assisted auto-reply and order
              automation for merchants on WhatsApp and Instagram, built on Meta's Business API platform.
              This policy explains what data we collect from merchants and their customers, why, and how
              it's handled.
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">2. Data we collect via WhatsApp and Instagram</h2>
            <p className="mt-3">
              We access WhatsApp and Instagram messaging through Meta's official Business API (Meta Cloud
              API / Instagram Messaging API). When a merchant connects their WhatsApp or Instagram business
              account to SellerTalk24, and when their customers message that account, we process:
            </p>
            <ul className="mt-3 list-disc space-y-2 pl-6">
              <li>The merchant's business phone number / Instagram handle and Meta-issued access tokens.</li>
              <li>Customer phone numbers (WhatsApp) or Instagram-scoped user IDs (Instagram).</li>
              <li>Chat/message content — text, images, and order-related messages exchanged in a conversation.</li>
              <li>Order data collected during checkout — items selected, size/variant, delivery address, and
                payment method chosen (COD, UPI, bank transfer).</li>
              <li>Basic conversation metadata — timestamps, message status, and lead/intent tags used to
                route conversations.</li>
            </ul>
            <p className="mt-3">
              We do not collect data from WhatsApp or Instagram beyond what is needed to run the automated
              reply and ordering flow the merchant has configured.
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">3. How we use this data</h2>
            <p className="mt-3">
              Data is used solely to operate the service the merchant has signed up for: generating AI
              replies, tracking conversation state, processing orders, and giving merchants visibility into
              their conversations and leads through the SellerTalk24 dashboard. We do not use customer chat
              data to train third-party AI models, and we do not sell customer data.
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">4. Data retention</h2>
            <p className="mt-3">
              [TODO: fill retention period for chat logs and order data — e.g. retained for the duration of
              the merchant's subscription plus N days/months after cancellation, then deleted or
              anonymized.]
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">5. Third-party sharing</h2>
            <p className="mt-3">
              We share data only with Meta Platforms, Inc., as required to deliver messages through the
              WhatsApp Business Platform and Instagram Messaging API. We do not sell customer or merchant
              data to advertisers or other third parties, and we do not share data across merchant accounts.
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">6. Your rights</h2>
            <p className="mt-3">
              Merchants and their customers can request access to, correction of, or deletion of their
              stored conversation and order data by contacting us at [TODO: support email — see Contact
              page]. We will respond to verified requests within [TODO: fill response timeframe].
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">7. Cookies</h2>
            <p className="mt-3">
              [TODO: confirm — if this marketing site or the dashboard sets analytics/session cookies,
              describe them here; if none beyond essential session cookies for login, state that.]
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">8. Contact</h2>
            <p className="mt-3">
              Questions about this policy can be sent to [TODO: support email] or via our{" "}
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
