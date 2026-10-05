import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./context/AuthContext";
import { RealtimeProvider } from "./context/RealtimeContext";
import { ToastProvider } from "./context/ToastContext";
import ProtectedRoute from "./components/ProtectedRoute";
import RequirePermission from "./components/RequirePermission";
import Login from "./pages/Login";
import Onboarding from "./pages/Onboarding";
import Dashboard from "./pages/Dashboard";
import Conversations from "./pages/Conversations";
import Leads from "./pages/Leads";
import Settings from "./pages/Settings";
import Catalogue from "./pages/Catalogue";
import Analytics from "./pages/Analytics";
// import Campaigns from "./pages/Campaigns"; // hidden — see HIDDEN_FEATURES.md
import Channels from "./pages/Channels";
import Orders from "./pages/Orders";
import Payments from "./pages/Payments";
import PaymentDetails from "./pages/PaymentDetails";
import Customers from "./pages/Customers";
import KnowledgeBase from "./pages/KnowledgeBase";
import Sandbox from "./pages/Sandbox";
import AcceptInvite from "./pages/AcceptInvite";
import CataloguePage from "./pages/public/CataloguePage";
import ProductPage from "./pages/public/ProductPage";

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
       <ToastProvider>
        <RealtimeProvider>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route
            path="/onboarding"
            element={
              <ProtectedRoute>
                <Onboarding />
              </ProtectedRoute>
            }
          />
          <Route
            path="/dashboard"
            element={
              <ProtectedRoute>
                <Dashboard />
              </ProtectedRoute>
            }
          />
          <Route
            path="/conversations"
            element={
              <ProtectedRoute>
                <RequirePermission permission="manual_reply">
                  <Conversations />
                </RequirePermission>
              </ProtectedRoute>
            }
          />
          <Route
            path="/leads"
            element={
              <ProtectedRoute>
                <RequirePermission ownerOnly>
                  <Leads />
                </RequirePermission>
              </ProtectedRoute>
            }
          />
          <Route
            path="/analytics"
            element={
              <ProtectedRoute>
                <RequirePermission permission="analytics_view">
                  <Analytics />
                </RequirePermission>
              </ProtectedRoute>
            }
          />
          <Route
            path="/channels"
            element={
              <ProtectedRoute>
                <RequirePermission ownerOnly>
                  <Channels />
                </RequirePermission>
              </ProtectedRoute>
            }
          />
          <Route
            path="/catalogue"
            element={
              <ProtectedRoute>
                <RequirePermission permission="catalog_edit">
                  <Catalogue />
                </RequirePermission>
              </ProtectedRoute>
            }
          />
          <Route
            path="/settings"
            element={
              <ProtectedRoute>
                <RequirePermission ownerOnly>
                  <Settings />
                </RequirePermission>
              </ProtectedRoute>
            }
          />
          {/* Campaigns hidden — see HIDDEN_FEATURES.md. /campaigns falls through to the "*" redirect. */}
          <Route
            path="/orders"
            element={
              <ProtectedRoute>
                <RequirePermission permission="order_view">
                  <Orders />
                </RequirePermission>
              </ProtectedRoute>
            }
          />
          <Route
            path="/payments"
            element={
              <ProtectedRoute>
                <RequirePermission permission="payment_verify">
                  <Payments />
                </RequirePermission>
              </ProtectedRoute>
            }
          />
          <Route
            path="/settings/payment"
            element={
              <ProtectedRoute>
                <RequirePermission ownerOnly>
                  <PaymentDetails />
                </RequirePermission>
              </ProtectedRoute>
            }
          />
          <Route
            path="/customers"
            element={
              <ProtectedRoute>
                <RequirePermission ownerOnly>
                  <Customers />
                </RequirePermission>
              </ProtectedRoute>
            }
          />
          <Route
            path="/knowledge"
            element={
              <ProtectedRoute>
                <RequirePermission ownerOnly>
                  <KnowledgeBase />
                </RequirePermission>
              </ProtectedRoute>
            }
          />
          <Route
            path="/sandbox"
            element={
              <ProtectedRoute>
                <RequirePermission ownerOnly>
                  <Sandbox />
                </RequirePermission>
              </ProtectedRoute>
            }
          />
          {/* Public catalogue routes — no auth */}
          <Route path="/shop/:slug" element={<CataloguePage />} />
          <Route path="/shop/:slug/product/:sku" element={<ProductPage />} />
          <Route path="/accept-invite" element={<AcceptInvite />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
        </RealtimeProvider>
       </ToastProvider>
      </BrowserRouter>
    </AuthProvider>
  );
}
