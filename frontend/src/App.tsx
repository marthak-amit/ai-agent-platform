import { BrowserRouter, Navigate, Outlet, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./context/AuthContext";
import { RealtimeProvider } from "./context/RealtimeContext";
import { ToastProvider } from "./context/ToastContext";
import { BillingProvider } from "./context/BillingContext";
import ProtectedRoute from "./components/ProtectedRoute";
import RequirePermission from "./components/RequirePermission";
import PlanParamCapture from "./components/PlanParamCapture";
import Login from "./pages/Login";
import Onboarding from "./pages/Onboarding";
import Dashboard from "./pages/Dashboard";
import Conversations from "./pages/Conversations";
import Leads from "./pages/Leads";
import Settings from "./pages/Settings";
import Billing from "./pages/Billing";
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
import { AdminAuthProvider } from "./context/AdminAuthContext";
import AdminProtectedRoute from "./components/admin/AdminProtectedRoute";
import AdminLayout from "./components/admin/AdminLayout";
import AdminLogin from "./pages/admin/AdminLogin";
import AdminOverview from "./pages/admin/AdminOverview";
import AdminClients from "./pages/admin/AdminClients";
import AdminClientDetail from "./pages/admin/AdminClientDetail";
import AdminBilling from "./pages/admin/AdminBilling";
import AdminUsage from "./pages/admin/AdminUsage";
import AdminSystem from "./pages/admin/AdminSystem";
import AdminAudit from "./pages/admin/AdminAudit";
import AdminOperators from "./pages/admin/AdminOperators";
import AdminAccount from "./pages/admin/AdminAccount";

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
       <ToastProvider>
        <RealtimeProvider>
        <BillingProvider>
        <PlanParamCapture />
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
            path="/billing"
            element={
              <ProtectedRoute>
                <RequirePermission ownerOnly>
                  <Billing />
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
          {/* Operator console — its own auth (admin JWT in sessionStorage), never the tenant session. */}
          <Route path="/admin" element={<AdminAuthProvider><Outlet /></AdminAuthProvider>}>
            <Route path="login" element={<AdminLogin />} />
            <Route element={<AdminProtectedRoute><AdminLayout /></AdminProtectedRoute>}>
              <Route index element={<AdminProtectedRoute perm="overview.read"><AdminOverview /></AdminProtectedRoute>} />
              <Route path="clients" element={<AdminProtectedRoute perm="clients.read"><AdminClients /></AdminProtectedRoute>} />
              <Route path="clients/:id" element={<AdminProtectedRoute perm="clients.read"><AdminClientDetail /></AdminProtectedRoute>} />
              <Route path="billing" element={<AdminProtectedRoute perm="billing.read"><AdminBilling /></AdminProtectedRoute>} />
              <Route path="usage" element={<AdminProtectedRoute perm="usage.read"><AdminUsage /></AdminProtectedRoute>} />
              <Route path="system" element={<AdminProtectedRoute perm="system.read"><AdminSystem /></AdminProtectedRoute>} />
              <Route path="audit" element={<AdminProtectedRoute perm="audit.read"><AdminAudit /></AdminProtectedRoute>} />
              <Route path="operators" element={<AdminProtectedRoute perm="admins.manage"><AdminOperators /></AdminProtectedRoute>} />
              <Route path="account" element={<AdminAccount />} />
            </Route>
          </Route>
          <Route path="/shop/:slug" element={<CataloguePage />} />
          <Route path="/shop/:slug/product/:sku" element={<ProductPage />} />
          <Route path="/accept-invite" element={<AcceptInvite />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
        </BillingProvider>
        </RealtimeProvider>
       </ToastProvider>
      </BrowserRouter>
    </AuthProvider>
  );
}
