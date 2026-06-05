# Security

## Bring-your-own-keys, local environment only

This framework is **bring-your-own-keys (BYOK)**. You supply your provider API
keys in a local `.env` file, and the pipeline reads them from your local process
environment **only**.

**Hard invariant:** the framework never fetches, downloads, or otherwise pulls
secrets over the network. There is no remote secret store, no "fetch config from
a private repo" bootstrap, no phone-home. Adapters and agents read keys from
environment variables (e.g. `APOLLO_API_KEY`) at runtime and nowhere else. This
invariant is enforced before publish by `scripts/scrub-check.sh`, which fails the
build if any network secret-fetch pattern or secret-shaped string is found in the
tree.

## What this means for you

- Keep your keys in `.env`. It is gitignored — do not commit it.
- Load them into your shell before invoking the pipeline:
  `set -a && source .env && set +a`.
- Only the providers whose env vars are set will be used. Missing keys are skipped
  with a log line; nothing is transmitted for a provider you have not configured.
- Your provider keys are sent **only** to that provider's own API endpoint over
  HTTPS, exactly as each provider's manifest declares — never to any third party
  and never to the framework's authors.

## Storage

- The `local` storage backend writes only to `./.gtm-data` (gitignored). No
  contact data leaves your machine except via the providers you explicitly wire
  into a waterfall.
- The `postgres` backend connects to the database you point `DATABASE_URL` at. You
  control that database.

## Compliance & acceptable use

This framework moves data between providers you choose; it does not grant permission to use
any of them. **You are responsible** for:

- **Provider terms of service**, including automation and scraping limits. Some capabilities
  reach LinkedIn data through the open web (`web_research` people/company search) or a
  third-party scraper (`apify`); using them may carry ToS and rate-limit obligations that are
  yours to honor.
- **Data-protection law** — GDPR, CCPA, and local rules — when you source, store, enrich, or
  contact individuals. The DACH example is illustrative; real outreach to EU residents has a
  legal-basis requirement.
- **Sending and consent** — anti-spam and opt-out obligations once a list reaches a sequencer.

The framework gives you the seams to choose compliant providers and the gates to keep a human
on sending. Confirm your own legal basis before a live run.

## Reporting a vulnerability

If you find a security issue — especially anything that could cause a secret to be
written to disk in a tracked file, logged, or transmitted to an unintended
destination — please open a private security advisory on the repository rather
than a public issue. Include the file, the reproduction, and the impact.

## Pre-publish gate

Before making any fork or distribution public, run:

```bash
bash scripts/scrub-check.sh
```

It must exit `0`. Install `gitleaks` for an authoritative secondary scan
(`brew install gitleaks`).
