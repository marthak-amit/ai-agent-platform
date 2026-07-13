import { ViteReactSSG } from "vite-react-ssg";
import App from "./App";
import Home from "./pages/Home";
import Pricing from "./pages/Pricing";
import Features from "./pages/Features";
import Industries from "./pages/Industries";
import FAQ from "./pages/FAQ";
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
        { path: "pricing", element: <Pricing /> },
        { path: "features", element: <Features /> },
        { path: "industries", element: <Industries /> },
        { path: "faq", element: <FAQ /> },
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
