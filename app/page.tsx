import Link from "next/link";
import { PageShell } from "@/components/page-shell";

const features = [
  {
    title: "Campaign Analytics",
    body: "Automatically organize campaign performance across marketplaces.",
    stat: "ACOS, ROAS, spend"
  },
  {
    title: "Search Term Intelligence",
    body: "Track winning search terms and identify wasted ad spend.",
    stat: "queries, clicks, orders"
  },
  {
    title: "AI Insights",
    body: "Identify trends, suggest optimizations, and summarize account performance.",
    stat: "summaries and signals"
  },
  {
    title: "Unified Dashboard",
    body: "Combine advertising and Merch sales into one interface.",
    stat: "sales plus ads"
  },
  {
    title: "Daily Automation",
    body: "Automatically synchronize reports instead of manually downloading CSV files.",
    stat: "scheduled reporting"
  },
  {
    title: "Built for Amazon Merch",
    body: "Designed specifically for Merch on Demand advertisers.",
    stat: "private MOD workflow"
  }
];

const metrics = [
  ["Ad spend", "$154.06", "+8.2%"],
  ["Attributed sales", "$1,221.54", "+12.5%"],
  ["ROAS", "7.93", "above target"],
  ["Campaigns", "38", "active review"]
];

export default function Home() {
  return (
    <PageShell>
      <section className="container-page grid min-h-[calc(100vh-4rem)] items-center gap-12 py-16 lg:grid-cols-[1fr_0.9fr] lg:py-24">
        <div className="animate-rise">
          <div className="mb-6 inline-flex rounded-full border border-line bg-card px-4 py-2 text-sm font-semibold text-muted">
            Crazy Good Designs LLC private internal platform
          </div>
          <h1 className="max-w-4xl text-balance text-5xl font-semibold tracking-tight text-foreground sm:text-6xl lg:text-7xl">
            Private Advertising Analytics for Amazon Merch on Demand
          </h1>
          <p className="mt-7 max-w-2xl text-lg leading-8 text-muted sm:text-xl">
            Merch Agent automatically combines Amazon Ads performance with Merch on Demand sales data to provide dashboards,
            analytics, and AI-powered insights that help optimize advertising decisions.
          </p>
          <p className="mt-4 max-w-2xl text-base leading-7 text-muted">
            Merch Agent is proprietary software developed and used internally by Crazy Good Designs LLC to manage Amazon
            advertising operations.
          </p>
          <div className="mt-10 flex flex-col gap-3 sm:flex-row">
            <Link
              href="#features"
              className="inline-flex items-center justify-center rounded-full bg-brand px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-blue-500/20 transition hover:-translate-y-0.5"
            >
              Learn More
            </Link>
            <Link
              href="#contact"
              className="inline-flex items-center justify-center rounded-full border border-line bg-card-strong px-6 py-3 text-sm font-semibold text-foreground transition hover:-translate-y-0.5 hover:border-brand"
            >
              Contact
            </Link>
          </div>
        </div>

        <div className="animate-float">
          <div className="glass-card rounded-[2rem] p-4">
            <div className="rounded-[1.5rem] border border-line bg-card-strong p-5">
              <div className="flex items-center justify-between border-b border-line pb-5">
                <div>
                  <p className="text-sm font-semibold text-brand">Merch Agent</p>
                  <h2 className="mt-1 text-2xl font-semibold">Advertising command view</h2>
                </div>
                <div className="rounded-full bg-[var(--brand-soft)] px-3 py-1 text-xs font-bold text-brand">Private</div>
              </div>
              <div className="grid gap-3 py-5 sm:grid-cols-2">
                {metrics.map(([label, value, note]) => (
                  <div key={label} className="rounded-2xl border border-line bg-background/60 p-4">
                    <p className="text-xs font-semibold uppercase text-muted">{label}</p>
                    <p className="mt-2 text-2xl font-semibold">{value}</p>
                    <p className="mt-1 text-sm text-brand">{note}</p>
                  </div>
                ))}
              </div>
              <div className="space-y-3 rounded-2xl border border-line bg-background/60 p-4">
                <div className="flex items-center justify-between text-sm">
                  <span className="font-semibold">Search term review</span>
                  <span className="text-muted">Last 7 days</span>
                </div>
                {["Watermelon cat shirt", "funny horse saying", "retro Halloween tee"].map((term, index) => (
                  <div key={term} className="grid grid-cols-[1fr_auto] gap-3">
                    <div className="h-2 overflow-hidden rounded-full bg-[var(--brand-soft)]">
                      <div className="h-full rounded-full bg-brand" style={{ width: `${86 - index * 18}%` }} />
                    </div>
                    <span className="text-xs font-semibold text-muted">{term}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </section>

      <section id="features" className="container-page py-20">
        <div className="mx-auto max-w-2xl text-center">
          <p className="text-sm font-semibold uppercase text-brand">Features</p>
          <h2 className="mt-3 text-balance text-4xl font-semibold tracking-tight">Built around the advertising decisions that matter daily.</h2>
        </div>
        <div className="mt-12 grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {features.map((feature) => (
            <article key={feature.title} className="glass-card rounded-3xl p-6 transition hover:-translate-y-1 hover:border-brand">
              <p className="text-sm font-semibold text-brand">{feature.stat}</p>
              <h3 className="mt-5 text-xl font-semibold">{feature.title}</h3>
              <p className="mt-3 leading-7 text-muted">{feature.body}</p>
            </article>
          ))}
        </div>
      </section>

      <section id="about" className="container-page py-20">
        <div className="grid gap-8 lg:grid-cols-[0.9fr_1.1fr] lg:items-center">
          <div>
            <p className="text-sm font-semibold uppercase text-brand">About Crazy Good Designs LLC</p>
            <h2 className="mt-3 text-balance text-4xl font-semibold tracking-tight">Internal software for responsible advertising operations.</h2>
            <p className="mt-5 text-base font-semibold text-muted">Raleigh, North Carolina, USA</p>
          </div>
          <div className="glass-card rounded-3xl p-7 text-lg leading-8 text-muted">
            <p>
              Crazy Good Designs LLC is an independent design studio and Amazon Merch on Demand publisher. We develop
              proprietary software to automate advertising analytics, reporting, and business operations for our own brands.
            </p>
            <p className="mt-5">
              Crazy Good Designs LLC develops internal software that helps manage Amazon Merch on Demand advertising. Merch
              Agent is currently a private internal analytics platform used to automate reporting and improve advertising
              decisions.
            </p>
            <p className="mt-5">
              Merch Agent is not currently licensed to third parties and is not presented as public software. The platform is
              used by Crazy Good Designs LLC to support its own Amazon advertising operations.
            </p>
          </div>
        </div>
      </section>

      <section id="contact" className="container-page py-20">
        <div className="rounded-[2rem] border border-line bg-foreground p-8 text-background sm:p-10 lg:p-12">
          <div className="grid gap-8 md:grid-cols-[1fr_auto] md:items-center">
            <div>
              <p className="text-sm font-semibold uppercase opacity-70">Contact</p>
              <h2 className="mt-3 text-3xl font-semibold tracking-tight sm:text-4xl">Crazy Good Designs LLC</h2>
              <p className="mt-4 max-w-2xl leading-7 opacity-75">
                For company, privacy, or Amazon Ads API review inquiries, contact the business directly.
              </p>
            </div>
            <a
              href="mailto:dianelindenberger@gmail.com"
              className="inline-flex rounded-full bg-background px-6 py-3 text-sm font-semibold text-foreground transition hover:-translate-y-0.5"
            >
              dianelindenberger@gmail.com
            </a>
          </div>
        </div>
      </section>
    </PageShell>
  );
}
