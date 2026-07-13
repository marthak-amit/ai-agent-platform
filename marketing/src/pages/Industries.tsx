import SEO from "../components/SEO";
import CTAButton from "../components/CTAButton";

export default function Industries() {
  return (
    <>
      <SEO
        title="Industries — SellerTalk24"
        description="SellerTalk24 is built first for fashion & apparel retailers in India, with more categories on the way."
        path="/industries"
      />
      <section className="mx-auto max-w-7xl px-4 py-16 sm:px-6 lg:px-8">
        <div className="text-center">
          <h1 className="text-4xl font-bold text-gray-900">Built for fashion retail, first</h1>
          <p className="mx-auto mt-4 max-w-xl text-gray-600">
            SellerTalk24 is purpose-built for how Indian fashion brands sell — variant-heavy
            catalogues, WhatsApp-first customers, and UPI/COD payments.
          </p>
        </div>

        <div className="mt-12 rounded-3xl bg-gradient-to-br from-brand-primary/5 to-brand-primary/5 p-8 ring-2 ring-brand-primary">
          <h2 className="text-2xl font-bold text-gray-900">Fashion &amp; Apparel</h2>
          <p className="mt-3 text-gray-700">
            Sarees, kurtis, lehengas, suits, and more. SellerTalk24 understands color, size, and
            fabric variants and helps customers pick exactly what they want — over chat, the way
            they already shop.
          </p>
          <CTAButton to="/demo" className="mt-6">
            Book a Demo
          </CTAButton>
        </div>

        <div className="mt-8 grid gap-6 sm:grid-cols-2">
          <div className="relative rounded-3xl bg-white p-8 opacity-70 ring-1 ring-gray-900/5">
            <span className="absolute right-4 top-4 rounded-full bg-gray-100 px-3 py-1 text-xs font-semibold text-gray-600">
              Coming soon
            </span>
            <h3 className="text-xl font-semibold text-gray-900">Jewelry</h3>
            <p className="mt-2 text-sm text-gray-600">
              Not yet supported — on our roadmap.
            </p>
          </div>
          <div className="relative rounded-3xl bg-white p-8 opacity-70 ring-1 ring-gray-900/5">
            <span className="absolute right-4 top-4 rounded-full bg-gray-100 px-3 py-1 text-xs font-semibold text-gray-600">
              Coming soon
            </span>
            <h3 className="text-xl font-semibold text-gray-900">Accessories</h3>
            <p className="mt-2 text-sm text-gray-600">
              Not yet supported — on our roadmap.
            </p>
          </div>
        </div>
      </section>
    </>
  );
}
