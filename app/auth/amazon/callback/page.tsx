import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "Amazon Authorization Callback | Merch Agent",
  description: "Authorization callback page for Merch Agent Amazon Ads API setup."
};

type CallbackPageProps = {
  searchParams: Promise<{
    code?: string;
    state?: string;
    error?: string;
    error_description?: string;
  }>;
};

export default async function AmazonCallbackPage({ searchParams }: CallbackPageProps) {
  const params = await searchParams;

  return (
    <main className="min-h-screen bg-background px-6 py-16 text-foreground">
      <section className="mx-auto max-w-3xl rounded-3xl border border-line bg-card-strong p-8 shadow-xl">
        <p className="text-sm font-semibold uppercase text-brand">Merch Agent</p>
        <h1 className="mt-3 text-3xl font-semibold tracking-tight">Amazon Authorization Callback</h1>
        <p className="mt-4 leading-7 text-muted">
          This private setup page is used by Crazy Good Designs LLC to complete Login with Amazon authorization for
          the Amazon Ads API.
        </p>

        {params.error ? (
          <div className="mt-8 rounded-2xl border border-red-200 bg-red-50 p-5 text-red-900">
            <h2 className="font-semibold">Authorization returned an error</h2>
            <p className="mt-2 break-words text-sm">{params.error}</p>
            {params.error_description ? <p className="mt-2 break-words text-sm">{params.error_description}</p> : null}
          </div>
        ) : (
          <div className="mt-8 rounded-2xl border border-line bg-background/70 p-5">
            <h2 className="font-semibold">Authorization code</h2>
            {params.code ? (
              <>
                <p className="mt-2 text-sm leading-6 text-muted">
                  Copy this code into the local Merch Agent authorization setup. Treat it as sensitive.
                </p>
                <code className="mt-4 block max-h-48 overflow-auto rounded-xl border border-line bg-card p-4 text-sm">
                  {params.code}
                </code>
              </>
            ) : (
              <p className="mt-2 text-sm leading-6 text-muted">
                No authorization code is present yet. You will see one here after completing the Login with Amazon
                authorization flow.
              </p>
            )}
          </div>
        )}

        <Link href="/" className="mt-8 inline-flex text-sm font-semibold text-brand">
          Return to company site
        </Link>
      </section>
    </main>
  );
}
