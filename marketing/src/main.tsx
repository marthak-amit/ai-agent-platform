import { ViteReactSSG } from "vite-react-ssg";
import App from "./App";
import Home from "./pages/Home";
import SectionRedirect from "./components/SectionRedirect";
import BookDemo from "./pages/BookDemo";
import Terms from "./pages/Terms";
import Privacy from "./pages/Privacy";
import RefundPolicy from "./pages/RefundPolicy";
import Contact from "./pages/Contact";
import NotFound from "./pages/NotFound";
import "./index.css";

export const createRoot = ViteReactSSG({
  routes: [
    {
      path: "/",
      element: <App />,
      children: [
        { index: true, element: <Home /> },
        {
          path: "pricing",
          element: (
            <SectionRedirect
              section="pricing"
              title="Pricing — SellerTalk24"
              description="Simple, transparent pricing for SellerTalk24's WhatsApp & Instagram AI sales agent for fashion retailers in India."
              path="/pricing"
            />
          ),
        },
        {
          path: "features",
          element: (
            <SectionRedirect
              section="features"
              title="Features — SellerTalk24"
              description="Everything SellerTalk24's AI sales agent does for fashion retailers: selling, operations, growth, and control."
              path="/features"
            />
          ),
        },
        {
          path: "industries",
          element: (
            <SectionRedirect
              section="industries"
              title="Industries — SellerTalk24"
              description="SellerTalk24 is built first for fashion & apparel retailers in India, with more categories on the way."
              path="/industries"
            />
          ),
        },
        {
          path: "faq",
          element: (
            <SectionRedirect
              section="faq"
              title="FAQ — SellerTalk24"
              description="Answers to common questions about SellerTalk24: channels, setup, languages, payments, security, and plans."
              path="/faq"
            />
          ),
        },
        { path: "demo", element: <BookDemo /> },
        { path: "terms", element: <Terms /> },
        { path: "privacy", element: <Privacy /> },
        { path: "refund-policy", element: <RefundPolicy /> },
        { path: "contact", element: <Contact /> },
        { path: "*", element: <NotFound /> },
      ],
    },
  ],
});
