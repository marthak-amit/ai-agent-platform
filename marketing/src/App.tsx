import { useEffect } from "react";
import { Outlet, useLocation } from "react-router-dom";
import Nav from "./components/Nav";
import Footer from "./components/Footer";

/** Scrolls the window to the top (or to a #hash target) whenever the route changes. */
function ScrollToTop() {
  const { pathname, hash, key } = useLocation();

  useEffect(() => {
    if (hash) {
      const target = document.getElementById(hash.slice(1));
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "start" });
        return;
      }
    }
    window.scrollTo(0, 0);
    // `key` changes on every navigation, even re-clicking a link to the same
    // hash, so this still scrolls when the URL string itself doesn't change.
  }, [pathname, hash, key]);

  return null;
}

export default function App() {
  return (
    <div className="flex min-h-screen flex-col font-sans text-gray-900">
      <ScrollToTop />
      <Nav />
      <main className="flex-1">
        <Outlet />
      </main>
      <Footer />
    </div>
  );
}
