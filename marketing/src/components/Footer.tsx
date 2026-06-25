import { Link } from "react-router-dom";
import { DASHBOARD_LOGIN_URL } from "../config";

export default function Footer() {
  return (
    <footer className="border-t border-gray-200 bg-gray-50">
      <div className="mx-auto max-w-7xl px-4 py-12 sm:px-6 lg:px-8">
        <div className="grid grid-cols-2 gap-8 sm:grid-cols-4">
          <div className="col-span-2 sm:col-span-1">
            <Link to="/" className="flex items-center gap-2 text-lg font-bold tracking-tight text-gray-900">
              <span
                className="inline-block h-7 w-7 rounded-lg bg-gradient-to-br from-indigo-600 to-violet-600"
                aria-hidden="true"
              />
              AgentlyAI
            </Link>
            <p className="mt-3 max-w-xs text-sm text-gray-600">
              AI sales automation for fashion retailers on WhatsApp &amp; Instagram. Built in India,
              for India.
            </p>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900">Product</h3>
            <ul className="mt-3 space-y-2 text-sm text-gray-600">
              <li><Link to="/features" className="hover:text-indigo-600">Features</Link></li>
              <li><Link to="/pricing" className="hover:text-indigo-600">Pricing</Link></li>
              <li><Link to="/industries" className="hover:text-indigo-600">Industries</Link></li>
            </ul>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900">Company</h3>
            <ul className="mt-3 space-y-2 text-sm text-gray-600">
              <li><Link to="/faq" className="hover:text-indigo-600">FAQ</Link></li>
              <li><Link to="/demo" className="hover:text-indigo-600">Book a Demo</Link></li>
            </ul>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900">Account</h3>
            <ul className="mt-3 space-y-2 text-sm text-gray-600">
              <li>
                <a href={DASHBOARD_LOGIN_URL} className="hover:text-indigo-600">
                  Log in
                </a>
              </li>
            </ul>
          </div>
        </div>

        <div className="mt-10 border-t border-gray-200 pt-6 text-sm text-gray-500">
          © {new Date().getFullYear()} AgentlyAI. All rights reserved.
        </div>
      </div>
    </footer>
  );
}
