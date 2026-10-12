import { Navigate, useLocation } from "react-router-dom";
import { useAdminAuth } from "../../context/AdminAuthContext";
import type { AdminPermission } from "../../types/admin";
import { Spinner } from "./ui";

/**
 * Gate for admin pages: signed in, password not pending a forced change, and (optionally) holding `perm`.
 * The server enforces every permission too — this only decides what the UI shows.
 */
export default function AdminProtectedRoute({ children, perm }: { children: React.ReactNode; perm?: AdminPermission }) {
  const { admin, loading, can } = useAdminAuth();
  const location = useLocation();

  if (loading) return <Spinner />;
  if (!admin) return <Navigate to="/admin/login" replace state={{ from: location.pathname }} />;
  if (admin.must_change_password && location.pathname !== "/admin/account") {
    return <Navigate to="/admin/account" replace />;
  }
  if (perm && !can(perm)) {
    return (
      <div className="max-w-md mx-auto mt-24 text-center">
        <h1 className="text-xl font-semibold text-gray-900">Not allowed</h1>
        <p className="text-sm text-gray-500 mt-2">Your role ({admin.role}) doesn't include access to this page.</p>
      </div>
    );
  }
  return <>{children}</>;
}
