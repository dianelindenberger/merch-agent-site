import type { Metadata } from "next";
import { PageShell } from "@/components/page-shell";

export const metadata: Metadata = {
  title: "Terms of Use | Crazy Good Designs LLC",
  description: "Terms of Use for Crazy Good Designs LLC and the private Merch Agent internal analytics platform."
};

const sections = [
  {
    title: "Informational Software",
    body: "Merch Agent provides internal dashboards, reporting, analytics, and AI-assisted summaries for Crazy Good Designs LLC. The information presented by the software is intended to support business review and decision-making. It is not financial, legal, tax, or professional advice."
  },
  {
    title: "No Guarantees",
    body: "Crazy Good Designs LLC does not guarantee advertising performance, marketplace outcomes, revenue, profitability, or uninterrupted service. Advertising decisions involve risk, and all reports or insights should be reviewed with appropriate business judgment."
  },
  {
    title: "Intellectual Property",
    body: "The Merch Agent name, software, interface, source code, workflows, documentation, graphics, and related materials are owned by Crazy Good Designs LLC or its licensors. No rights are granted except as expressly stated in writing."
  },
  {
    title: "Acceptable Use",
    body: "Merch Agent is a private internal platform. It may not be accessed, copied, modified, reverse engineered, sold, sublicensed, or used by third parties without written permission from Crazy Good Designs LLC."
  },
  {
    title: "Limitation of Liability",
    body: "To the maximum extent allowed by law, Crazy Good Designs LLC is not liable for indirect, incidental, consequential, special, or exemplary damages arising from use of this website or Merch Agent. Any liability is limited to the amount permitted under applicable law."
  },
  {
    title: "Third-Party Services",
    body: "Merch Agent may interact with Amazon Ads services, Amazon OAuth, and related reporting systems. Use of those services is subject to the applicable third-party terms, policies, and authorization requirements."
  },
  {
    title: "Contact",
    body: "Questions about these Terms may be sent to dianelindenberger@gmail.com."
  }
];

export default function TermsPage() {
  return (
    <PageShell>
      <section className="container-page py-16 sm:py-24">
        <div className="max-w-3xl">
          <p className="text-sm font-semibold uppercase text-brand">Terms of Use</p>
          <h1 className="mt-3 text-4xl font-semibold tracking-tight sm:text-5xl">Crazy Good Designs LLC Terms of Use</h1>
          <p className="mt-6 text-lg leading-8 text-muted">
            These Terms apply to this website and to Merch Agent, a private internal analytics platform developed by Crazy Good
            Designs LLC for its own Amazon Merch on Demand advertising operations.
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
