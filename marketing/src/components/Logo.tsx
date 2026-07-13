import logoSrc from "../assets/sellertalk24.png";

interface LogoProps {
  className?: string;
  alt?: string;
}

export default function Logo({ className = "h-10 w-auto", alt = "SellerTalk24" }: LogoProps) {
  return <img src={logoSrc} alt={alt} className={className} />;
}
