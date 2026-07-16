import type { Metadata } from "next";
import { PageShell } from "@/components/page-shell";

export const metadata: Metadata = {
  title: "Privacy Policy | Crazy Good Designs LLC",
  description: "Privacy Policy for Crazy Good Designs LLC and the private Merch Agent internal analytics platform."
};

const sections = [
  {
    title: "Information Collected",
    body: "Crazy Good Designs LLC may collect business contact information, website usage information, device and browser information, and internal operational data needed to run Merch Agent. Merch Agent may process Amazon Ads performance data, campaign data, search term data, reporting exports, and Merch on Demand sales information used by Crazy Good Designs LLC for its own advertising analysis."
  },
  {
    title: "Cookies",
    body: "This website may use essential cookies or local browser storage to remember basic preferences such as theme selection. If analytics are enabled, cookies or similar technologies may be used to understand aggregate website usage."
  },
  {
    title: "Analytics",
    body: "Website analytics, if used, are limited to understanding traffic, performance, and reliability. Analytics data is reviewed in aggregate and is not used to sell, license, or publicly market Merch Agent."
  },
  {
    title: "Amazon OAuth",
    body: "Merch Agent may use Amazon OAuth or related authorization flows to connect Crazy Good Designs LLC accounts with Amazon Ads services. OAuth tokens, authorization codes, and related credentials are used only to authenticate API requests and maintain authorized access for internal business operations."
  },
  {
    title: "Amazon Ads API Data",
    body: "Amazon Ads API data may include campaign settings, targeting information, search terms, impressions, clicks, spend, sales, orders, attribution metrics, and report metadata. This data is used to generate dashboards, analytics, automated reporting, and AI-assisted summaries for Crazy Good Designs LLC. It is not sold or licensed to third parties."
  },
  {
    title: "Security",
    body: "Crazy Good Designs LLC uses reasonable administrative, technical, and organizational safeguards to protect internal data, API credentials, and reporting information. Access is limited to business purposes. No method of transmission or storage is perfectly secure, but the company works to reduce risk and protect sensitive information."
  },
  {
    title: "User Rights",
    body: "Depending on applicable law, individuals may have the right to request access, correction, deletion, or restriction of personal information. Requests can be submitted using the contact information below. Because Merch Agent is a private internal tool, most data processed by the platform relates to Crazy Good Designs LLC business operations."
  },
  {
    title: "Contact Information",
    body: "Questions about this Privacy Policy or privacy practices may be sent to dianelindenberger@gmail.com."
  }
];

export default function PrivacyPage() {
  return (
    <PageShell>
      <section className="container-page py-16 sm:py-24">
        <div className="max-w-3xl">
          <p className="text-sm font-semibold uppercase text-brand">Privacy Policy</p>
          <h1 className="mt-3 text-4xl font-semibold tracking-tight sm:text-5xl">Crazy Good Designs LLC Privacy Policy</h1>
          <p className="mt-6 text-lg leading-8 text-muted">
            This Privacy Policy explains how Crazy Good Designs LLC handles information related to this website and Merch
            Agent, a private internal analytics platform used by the company for Amazon Merch on Demand advertising operations.
          </p>
          <p className="mt-4 text-sm text-muted">Last updated: July 15, 2026</p>
        </div>

        <div className="mt-12 grid gap-5">
          {sections.map((section) => (
            <article key={section.title} className="glass-card rounded-3xl p-6 sm:p-7">
              <h2 className="text-2xl font-semibold tracking-tight">{section.title}</h2>
              <p className="mt-4 leading-8 text-muted">{section.body}</p>
            </article>
          ))}
        </div>
      </section>
    </PageShell>
  );
}
