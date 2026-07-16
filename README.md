# Crazy Good Designs LLC Website

Production landing website for Crazy Good Designs LLC and Merch Agent, a private internal analytics platform for Amazon Merch on Demand advertising operations.

The site is intentionally written as a company website, not a public SaaS marketing page. It explains that Merch Agent is an internal tool used by Crazy Good Designs LLC and is not currently licensed to third parties.

## Stack

- Next.js 15
- TypeScript
- Tailwind CSS
- Responsive design
- Dark/light mode
- Vercel-ready deployment

## Local Development

```powershell
pnpm install
pnpm dev
```

Then open:

```text
http://localhost:3000
```

## Production Build

```powershell
pnpm build
pnpm start
```

## Environment Variables

Create `.env.local` for local values when needed:

```text
NEXT_PUBLIC_SITE_URL=https://crazy-good-designs-merch-agent.vercel.app
```

`NEXT_PUBLIC_SITE_URL` is used for canonical metadata and Open Graph URL generation. A good free Vercel site name for this project is `https://crazy-good-designs-merch-agent.vercel.app`. For production, set this value to the actual Vercel deployment URL or a custom domain that Crazy Good Designs LLC controls. If omitted, the site defaults to `http://localhost:3000` for local development.

No secrets are required for the static company website. Amazon OAuth credentials and Amazon Ads API secrets should not be added to this frontend site unless a future backend integration explicitly requires it.

## Vercel Deployment

1. Push this folder to a Git repository.
2. In Vercel, create a new project from the repository.
3. Set the project root to `merch-agent-v2` if deploying from the larger Merch Agent repository.
4. Use the default Next.js framework settings.
5. Add `NEXT_PUBLIC_SITE_URL` with the production Vercel URL or a domain you control.
6. Deploy.

## Pages

- `/` - Company landing page
- `/privacy` - Privacy Policy
- `/terms` - Terms of Use

## Notes

The older static prototype remains in `web/` for reference, but the production website is the Next.js app at the root of this folder.
