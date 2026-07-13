import { Head } from "vite-react-ssg";

interface SEOProps {
  title: string;
  description: string;
  path: string;
}

const SITE_URL = "https://sellertalk24.com"; // TODO: replace with the real production domain

export default function SEO({ title, description, path }: SEOProps) {
  const url = `${SITE_URL}${path}`;
  return (
    <Head>
      <title>{title}</title>
      <meta name="description" content={description} />
      <link rel="canonical" href={url} />
      <meta property="og:type" content="website" />
      <meta property="og:title" content={title} />
      <meta property="og:description" content={description} />
      <meta property="og:url" content={url} />
      <meta name="twitter:card" content="summary_large_image" />
      <meta name="twitter:title" content={title} />
      <meta name="twitter:description" content={description} />
    </Head>
  );
}
