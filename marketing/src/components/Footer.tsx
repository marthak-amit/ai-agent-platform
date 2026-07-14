import { Link } from "react-router-dom";
import { DASHBOARD_LOGIN_URL } from "../config";
import Logo from "./Logo";

export default function Footer() {
  return (
    <footer className="border-t border-gray-200 bg-gray-50">
      <div className="mx-auto max-w-7xl px-4 py-12 sm:px-6 lg:px-8">
        <div className="grid grid-cols-2 gap-8 sm:grid-cols-4">
          <div className="col-span-2 sm:col-span-1">
            <Link to="/" className="flex items-center gap-2 text-lg font-bold tracking-tight text-gray-900">
              <Logo className="h-9 w-auto" />
            </Link>
            <p className="mt-3 max-w-xs text-sm text-gray-600">
              AI sales automation for fashion retailers on WhatsApp &amp; Instagram. Built in India,
              for India.
            </p>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900">Product</h3>
            <ul className="mt-3 space-y-2 text-sm text-gray-600">
              <li><Link to="/#features" className="hover:text-brand-primaryDark">Features</Link></li>
              <li><Link to="/#pricing" className="hover:text-brand-primaryDark">Pricing</Link></li>
              <li><Link to="/#industries" className="hover:text-brand-primaryDark">Industries</Link></li>
            </ul>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900">Company</h3>
            <ul className="mt-3 space-y-2 text-sm text-gray-600">
              <li><Link to="/#faq" className="hover:text-brand-primaryDark">FAQ</Link></li>
              <li><Link to="/demo" className="hover:text-brand-primaryDark">Book a Demo</Link></li>
              <li><Link to="/contact" className="hover:text-brand-primaryDark">Contact</Link></li>
            </ul>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900">Legal</h3>
            <ul className="mt-3 space-y-2 text-sm text-gray-600">
              <li><Link to="/terms" className="hover:text-brand-primaryDark">Terms &amp; Conditions</Link></li>
              <li><Link to="/privacy" className="hover:text-brand-primaryDark">Privacy Policy</Link></li>
              <li><Link to="/refund-policy" className="hover:text-brand-primaryDark">Refund &amp; Cancellation</Link></li>
            </ul>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900">Account</h3>
            <ul className="mt-3 space-y-2 text-sm text-gray-600">
              <li>
                <a href={DASHBOARD_LOGIN_URL} className="hover:text-brand-primaryDark">
                  Log in
                </a>
              </li>
            </ul>
          </div>
        </div>

        <div className="mt-10 border-t border-gray-200 pt-6 text-sm text-gray-500">
          © {new Date().getFullYear()} SellerTalk24. All rights reserved.
        </div>
      </div>
    </footer>
  );
}
