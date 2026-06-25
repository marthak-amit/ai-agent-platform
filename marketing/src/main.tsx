import { ViteReactSSG } from "vite-react-ssg";
import App from "./App";
import Home from "./pages/Home";
import Pricing from "./pages/Pricing";
import Features from "./pages/Features";
import Industries from "./pages/Industries";
import FAQ from "./pages/FAQ";
import BookDemo from "./pages/BookDemo";
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
        { path: "*", element: <NotFound /> },
      ],
    },
  ],
});
