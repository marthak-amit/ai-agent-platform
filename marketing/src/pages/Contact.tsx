import SEO from "../components/SEO";
import Reveal from "../components/motion/Reveal";

export default function Contact() {
  return (
    <>
      <SEO
        title="Contact — SellerTalk24"
        description="Get in touch with the SellerTalk24 team for support, sales, or billing questions."
        path="/contact"
      />
      <section className="mx-auto max-w-3xl px-4 py-16 sm:px-6 lg:px-8">
        <Reveal as="h1" className="text-4xl font-bold text-gray-900">
          Contact us
        </Reveal>

        <div className="prose prose-gray mt-10 max-w-none space-y-8 text-gray-700">
          <div>
            <h2 className="text-xl font-semibold text-gray-900">Support</h2>
            <p className="mt-3">
              Email: [TODO: fill support email]
              <br />
              Typical response time: [TODO: fill response time, e.g. within 1 business day]
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">Business address</h2>
            <p className="mt-3">
              [TODO: legal entity name]
              <br />
              [TODO: registered business address]
            </p>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-gray-900">Sales &amp; demos</h2>
            <p className="mt-3">
              Want to see SellerTalk24 in action?{" "}
              <a href="/demo" className="text-brand-primaryDark underline">
                Book a demo
              </a>{" "}
              and our team will walk you through setup.
            </p>
          </div>
        </div>
      </section>
    </>
  );
}
