import Link from "next/link";

type LogoProps = {
  showText?: boolean;
};

export function Logo({ showText = true }: LogoProps) {
  return (
    <Link href="/" className="group inline-flex items-center gap-3" aria-label="Merch Agent home">
      <svg
        className="h-10 w-10 shrink-0"
        viewBox="0 0 64 64"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        aria-hidden="true"
      >
        <rect width="64" height="64" rx="18" fill="#111827" />
        <rect x="15" y="34" width="7" height="13" rx="2" fill="#60A5FA" />
        <rect x="27" y="26" width="7" height="21" rx="2" fill="#3B82F6" />
        <rect x="39" y="18" width="7" height="29" rx="2" fill="#93C5FD" />
        <path
          d="M15 29.5C21.7 29.5 25.2 20.5 31.8 20.5C36.4 20.5 38.2 15 44.8 15H49"
          stroke="#F8FAFC"
          strokeWidth="3"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <path d="M45 10.5L50 15L45 19.5" stroke="#F8FAFC" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
        <circle cx="19" cy="17" r="2.4" fill="#60A5FA" />
        <circle cx="29" cy="13" r="2.4" fill="#60A5FA" />
        <circle cx="39" cy="10" r="2.4" fill="#60A5FA" />
        <path d="M21.4 16.2L26.6 13.8M31.4 12.5L36.6 10.8" stroke="#60A5FA" strokeWidth="1.6" strokeLinecap="round" />
      </svg>
      {showText ? (
        <span className="grid leading-none">
          <span className="text-base font-semibold tracking-tight text-foreground">Merch Agent</span>
          <span className="mt-1 text-xs font-medium text-muted">Crazy Good Designs LLC</span>
        </span>
      ) : null}
    </Link>
  );
}
