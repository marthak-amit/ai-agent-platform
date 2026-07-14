import { Navigate } from "react-router-dom";
import SEO from "./SEO";

interface SectionRedirectProps {
  section: string;
  title: string;
  description: string;
  path: string;
}

/** Renders SEO tags for a legacy standalone-page URL, then redirects to its section on the home page. */
export default function SectionRedirect({ section, title, description, path }: SectionRedirectProps) {
  return (
    <>
      <SEO title={title} description={description} path={path} />
      <Navigate to={`/#${section}`} replace />
    </>
  );
}
