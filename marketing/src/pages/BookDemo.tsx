import { useState, type FormEvent } from "react";
import SEO from "../components/SEO";
import CalendlyEmbed from "../components/CalendlyEmbed";
import { CALENDLY_URL } from "../config";
import { submitDemoLead, buildMailtoFallback, type DemoLeadPayload } from "../lib/leads";

const trustPoints = ["30 minutes", "See it on your products", "No commitment"];

const initialState: DemoLeadPayload = {
  businessName: "",
  email: "",
  phone: "",
  whatsappNumber: "",
  monthlyOrderVolume: "",
  message: "",
  companyWebsite: "",
};

type Status = "idle" | "submitting" | "sent" | "error";
type FormError = { message: string; showMailtoFallback: boolean };

export default function BookDemo() {
  const [form, setForm] = useState<DemoLeadPayload>(initialState);
  const [status, setStatus] = useState<Status>("idle");
  const [formError, setFormError] = useState<FormError | null>(null);

  const update = (key: keyof DemoLeadPayload) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setStatus("submitting");
    setFormError(null);

    const result = await submitDemoLead(form);

    switch (result.kind) {
      case "ok":
        setStatus("sent");
        break;
      case "validation_error":
        setStatus("error");
        setFormError({ message: result.message, showMailtoFallback: false });
        break;
      case "rate_limited":
        setStatus("error");
        setFormError({ message: "Too many requests, try again shortly.", showMailtoFallback: false });
        break;
      case "network_error":
        setStatus("error");
        setFormError({
          message: "Something went wrong on our end. You can also email us directly:",
          showMailtoFallback: true,
        });
        break;
    }
  };

  return (
    <>
      <SEO
        title="Book a Demo — AgentlyAI"
        description="Book a demo to see AgentlyAI's AI sales agent working on your own fashion catalogue."
        path="/demo"
      />
      <section className="mx-auto max-w-3xl px-4 py-16 sm:px-6 lg:px-8">
        <h1 className="text-center text-4xl font-bold text-gray-900">Book a 30-minute demo</h1>
        <p className="mx-auto mt-3 max-w-xl text-center text-gray-600">
          See AgentlyAI working on your own catalogue — WhatsApp &amp; Instagram order automation
          for fashion retailers.
        </p>

        <ul className="mt-6 flex flex-wrap items-center justify-center gap-x-6 gap-y-2 text-sm font-medium text-indigo-700">
          {trustPoints.map((point) => (
            <li key={point} className="flex items-center gap-1.5">
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-indigo-600" aria-hidden="true" />
              {point}
            </li>
          ))}
        </ul>

        <div className="mt-10 overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-sm">
          <CalendlyEmbed url={CALENDLY_URL} className="w-full" />
        </div>
      </section>

      <section className="mx-auto max-w-xl px-4 pb-16 sm:px-6 lg:px-8">
        <div className="rounded-2xl border border-gray-200 bg-gray-50 p-6">
          <h2 className="text-center text-lg font-semibold text-gray-900">
            Prefer email? Send us your details
          </h2>
          <p className="mt-2 text-center text-sm text-gray-600">
            Can't find a time that works? Tell us a bit about your business and we'll reach out
            to schedule directly.
          </p>
        </div>

        {status === "sent" ? (
          <div className="mt-10 rounded-2xl border border-indigo-200 bg-indigo-50 p-8 text-center">
            <h2 className="text-xl font-semibold text-gray-900">Thanks, we'll be in touch</h2>
            <p className="mt-2 text-gray-600">Our team will reach out shortly to schedule your demo.</p>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="mt-10 space-y-5" noValidate>
            {formError && (
              <div
                role="alert"
                className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
              >
                {formError.message}
                {formError.showMailtoFallback && (
                  <>
                    {" "}
                    <a href={buildMailtoFallback(form)} className="font-semibold underline">
                      demo@agentlyai.in
                    </a>
                  </>
                )}
              </div>
            )}

            <div>
              <label htmlFor="businessName" className="block text-sm font-medium text-gray-700">
                Business name
              </label>
              <input
                id="businessName"
                required
                value={form.businessName}
                onChange={update("businessName")}
                disabled={status === "submitting"}
                className="mt-1 block w-full rounded-lg border border-gray-300 px-3 py-2 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:opacity-60"
              />
            </div>
            <div>
              <label htmlFor="email" className="block text-sm font-medium text-gray-700">
                Email
              </label>
              <input
                id="email"
                type="email"
                required
                value={form.email}
                onChange={update("email")}
                disabled={status === "submitting"}
                className="mt-1 block w-full rounded-lg border border-gray-300 px-3 py-2 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:opacity-60"
              />
            </div>
            <div>
              <label htmlFor="phone" className="block text-sm font-medium text-gray-700">
                Phone
              </label>
              <input
                id="phone"
                required
                value={form.phone}
                onChange={update("phone")}
                disabled={status === "submitting"}
                className="mt-1 block w-full rounded-lg border border-gray-300 px-3 py-2 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:opacity-60"
              />
            </div>
            <div>
              <label htmlFor="whatsappNumber" className="block text-sm font-medium text-gray-700">
                WhatsApp number
              </label>
              <input
                id="whatsappNumber"
                required
                value={form.whatsappNumber}
                onChange={update("whatsappNumber")}
                disabled={status === "submitting"}
                className="mt-1 block w-full rounded-lg border border-gray-300 px-3 py-2 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:opacity-60"
              />
            </div>
            <div>
              <label htmlFor="monthlyOrderVolume" className="block text-sm font-medium text-gray-700">
                Monthly order volume
              </label>
              <input
                id="monthlyOrderVolume"
                placeholder="e.g. 50-100 orders/month"
                value={form.monthlyOrderVolume}
                onChange={update("monthlyOrderVolume")}
                disabled={status === "submitting"}
                className="mt-1 block w-full rounded-lg border border-gray-300 px-3 py-2 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:opacity-60"
              />
            </div>
            <div>
              <label htmlFor="message" className="block text-sm font-medium text-gray-700">
                Message
              </label>
              <textarea
                id="message"
                rows={4}
                value={form.message}
                onChange={update("message")}
                disabled={status === "submitting"}
                className="mt-1 block w-full rounded-lg border border-gray-300 px-3 py-2 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:opacity-60"
              />
            </div>

            {/* Honeypot — off-screen, not display:none/visibility:hidden, so
                screen readers and keyboard users skip it but bots that fill
                every field will trip it. Must stay empty. */}
            <div className="absolute -left-[9999px] top-auto h-px w-px overflow-hidden" aria-hidden="true">
              <label htmlFor="companyWebsite">Leave this field blank</label>
              <input
                id="companyWebsite"
                name="companyWebsite"
                type="text"
                tabIndex={-1}
                autoComplete="off"
                value={form.companyWebsite}
                onChange={update("companyWebsite")}
              />
            </div>

            <button
              type="submit"
              disabled={status === "submitting"}
              className="w-full rounded-xl bg-indigo-600 px-6 py-3 text-sm font-semibold text-white shadow-sm transition hover:bg-indigo-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 focus-visible:ring-offset-2 disabled:opacity-60"
            >
              {status === "submitting" ? "Submitting..." : "Request Demo"}
            </button>
          </form>
        )}
      </section>
    </>
  );
}
