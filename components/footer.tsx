import Link from "next/link";
import { Logo } from "./logo";

const links = [
  { href: "/", label: "Home" },
  { href: "/privacy", label: "Privacy" },
  { href: "/terms", label: "Terms" },
  { href: "/#contact", label: "Contact" }
];

export function Footer() {
  const year = new Date().getFullYear();

  return (
    <footer className="border-t border-line">
      <div className="container-page flex flex-col gap-8 py-10 md:flex-row md:items-center md:justify-between">
        <div className="space-y-4">
          <Logo />
          <p className="max-w-md text-sm leading-6 text-muted">
            Private analytics and reporting software for Crazy Good Designs LLC advertising operations.
          </p>
        </div>
        <div className="flex flex-wrap gap-5 text-sm font-medium text-muted">
          {links.map((link) => (
            <Link key={link.href} href={link.href} className="transition hover:text-foreground">
              {link.label}
            </Link>
          ))}
        </div>
      </div>
      <div className="container-page border-t border-line py-5 text-sm text-muted">
        © {year} Crazy Good Designs LLC • All Rights Reserved
      </div>
    </footer>
  );
}
