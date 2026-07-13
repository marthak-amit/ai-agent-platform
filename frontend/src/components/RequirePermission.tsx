import { Navigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import type { PermissionKey } from "../types";

/**
 * Route guard for pages gated by the checklist permission system.
 *
 * Renders children only if the current user is the Owner, or (when
 * `ownerOnly` is not set) holds `permission` in their checklist. Otherwise
 * redirects to /dashboard — mirrors the backend's default-deny model so a
 * non-owner can't reach a page just by typing its URL. Must be nested inside
 * ProtectedRoute, which already handles the unauthenticated/loading cases.
 */
export default function RequirePermission({
  children,
  permission,
  ownerOnly,
}: {
  children: React.ReactNode;
  permission?: PermissionKey;
  ownerOnly?: boolean;
}) {
  const { client } = useAuth();
  const currentUser = client?.current_user;

  const allowed =
    !!currentUser &&
    (currentUser.is_owner || (!ownerOnly && !!permission && currentUser.permissions.includes(permission)));

  if (!allowed) {
    return <Navigate to="/dashboard" replace />;
  }
  return <>{children}</>;
}
