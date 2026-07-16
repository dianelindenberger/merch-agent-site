import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap"
});

const siteUrl = process.env.NEXT_PUBLIC_SITE_URL || "http://localhost:3000";
const title = "Crazy Good Designs LLC | Merch Agent";
const description =
  "Merch Agent is a private internal analytics platform from Crazy Good Designs LLC for Amazon Merch on Demand advertising reporting, dashboards, and AI-powered insights.";

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f7f9fc" },
    { media: "(prefers-color-scheme: dark)", color: "#070b12" }
  ]
};

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title,
  description,
  applicationName: "Merch Agent",
  authors: [{ name: "Crazy Good Designs LLC" }],
  creator: "Crazy Good Designs LLC",
  publisher: "Crazy Good Designs LLC",
  keywords: [
    "Crazy Good Designs LLC",
    "Merch Agent",
    "Amazon Merch on Demand",
    "Amazon Ads analytics",
    "advertising reporting"
  ],
  openGraph: {
    title,
    description,
    url: siteUrl,
    siteName: "Crazy Good Designs LLC",
    type: "website",
    locale: "en_US"
  },
  twitter: {
    card: "summary",
    title,
    description
  },
  icons: {
    icon: "/favicon.svg"
  }
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={`${inter.variable} antialiased`}>
        <script
          dangerouslySetInnerHTML={{
            __html:
              "try{var t=localStorage.getItem('theme');var d=t?t==='dark':matchMedia('(prefers-color-scheme: dark)').matches;document.documentElement.classList.toggle('dark',d)}catch(e){}"
          }}
        />
        {children}
      </body>
    </html>
  );
}
