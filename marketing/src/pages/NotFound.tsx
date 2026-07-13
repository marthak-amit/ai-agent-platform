import SEO from "../components/SEO";
import CTAButton from "../components/CTAButton";

export default function NotFound() {
  return (
    <>
      <SEO title="Page not found — SellerTalk24" description="This page does not exist." path="/404" />
      <section className="mx-auto flex max-w-7xl flex-col items-center px-4 py-24 text-center sm:px-6 lg:px-8">
        <h1 className="text-3xl font-bold text-gray-900">Page not found</h1>
        <p className="mt-3 text-gray-600">The page you're looking for doesn't exist.</p>
        <CTAButton to="/" className="mt-8">
          Back to home
        </CTAButton>
      </section>
    </>
  );
}
