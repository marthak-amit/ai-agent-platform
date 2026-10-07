import { useEffect } from "react";
import { useLocation } from "react-router-dom";
import { capturePlanFromSearch } from "../utils/pendingPlan";

/** Renders nothing: remembers `?plan=<code>` from any URL (marketing-site CTAs) before login/redirects drop it. */
export default function PlanParamCapture() {
  const { search } = useLocation();
  useEffect(() => {
    capturePlanFromSearch(search);
  }, [search]);
  return null;
}
