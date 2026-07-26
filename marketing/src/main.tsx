import { ViteReactSSG } from "vite-react-ssg";
import App from "./App";
import Home from "./pages/Home";
import Features from "./pages/Features";
import Industries from "./pages/Industries";
import Pricing from "./pages/Pricing";
import Faq from "./pages/Faq";
import "./index.css";

// Booking (Calendly widget) and legal boilerplate are visited far less often
// than the core marketing pages, and aren't part of the cross-linked
// Features/Industries/Pricing/FAQ journey — code-split them out of the main
// bundle via vite-react-ssg's route-level `lazy`, which (unlike React.lazy)
// is awaited during SSG so prerendered HTML is still complete.
export const createRoot = ViteReactSSG({
  routes: [
    {
      path: "/",
      element: <App />,
      children: [
        { index: true, element: <Home /> },
        { path: "pricing", element: <Pricing /> },
        { path: "features", element: <Features /> },
        { path: "industries", element: <Industries /> },
        { path: "faq", element: <Faq /> },
        {
          path: "demo",
          lazy: async () => ({ Component: (await import("./pages/BookDemo")).default }),
        },
        {
          path: "terms",
          lazy: async () => ({ Component: (await import("./pages/Terms")).default }),
        },
        {
          path: "privacy",
          lazy: async () => ({ Component: (await import("./pages/Privacy")).default }),
        },
        {
          path: "refund-policy",
          lazy: async () => ({ Component: (await import("./pages/RefundPolicy")).default }),
        },
        {
          path: "contact",
          lazy: async () => ({ Component: (await import("./pages/Contact")).default }),
        },
        {
          path: "*",
          lazy: async () => ({ Component: (await import("./pages/NotFound")).default }),
        },
      ],
    },
  ],
});
