---
name: gtm-setup
description: >
  First-run setup for the GTM pipeline. Asks a few questions, reads the user's website, and
  writes context/icp.md, personas.md, segments.md, exclusions.md and do-not-contact.csv, copies
  gtm.config.yaml and .env from their examples, then runs scripts/doctor.py and suggests a
  first brief. Use when the user says "/gtm-setup", "set this up", "get started", "onboard
  me", or when /gtm finds the context files missing or still templates.
allowed-tools: Read, Write, Bash, WebFetch, WebSearch
---

Set up this checkout so `/gtm <brief>` works. The user should be able to finish in about five
minutes without reading the docs. Input, if any: **$ARGUMENTS**

## Rules
- **Never overwrite** an existing, filled-in file. If `context/icp.md` exists without
  `<placeholder>` text, show it and ask what to change instead.
- **Never write an API key.** Copy `.env.example` to `.env` only if `.env` is missing, and tell
  the user which keys to fill in. Keys never go in any other file.
- **Never invent a customer, a competitor or a seed company.** Seeds and do-not-contact
  entries come from the user. Titles and segments you infer are drafts, and you say so.

## Steps

1. **Copy what is missing** (never overwrite):
   `[ -f gtm.config.yaml ] || cp gtm.config.example.yaml gtm.config.yaml` and the same for
   `.env`.

2. **Ask, in one message** (accept short answers; skip what the user doesn't know):
   1. Your website URL.
   2. What you sell, in one sentence.
   3. Who buys it: job titles, and who else is involved in the decision.
   4. Where: countries or regions.
   5. Three to ten of your best customers (names or domains). These become look-alike seeds.
   6. Anyone you must never contact: current customers, partners, opt-outs. A list of
      domains/emails, a CSV path, or "none for now".
   7. Company types you never sell to (e.g. agencies, competitors).

3. **Read the website** (home, product, customers/case-studies, pricing pages; WebFetch) for
   the product, the buyer, and the industries it names. Use it to draft; do not quote marketing
   claims as facts about the market.

4. **Draft the files** from the `.example` templates, keeping their headings so the pipeline can
   read them:
   - `context/icp.md` — product, segments in priority order, default geography, default
     seniorities, the seeds from answer 5, anti-fit signals.
   - `context/personas.md` — one block per buyer role from answer 3, each with `Titles:`,
     `Also-known-as:` (sector and local-language variants for the user's region), a
     vertical-appropriate `Seniority:`, `Why relevant:` and a `Disambiguation:` line naming the
     wrong senses of the title.
   - `context/segments.md` — A/B/C criteria from the segments, the default 0–10 rubric and
     thresholds.
   - `context/exclusions.md` — answer 7 plus the standard always-skip titles.
   - `context/do-not-contact.csv` — header `email,domain,phone,linkedin_url,reason`, then answer
     6, one entry per row with a reason. If the user said "none", write the header only and tell
     them that means "checked against an empty list".

5. **Show the drafts** (personas and segments in full, the rest summarized) and ask for
   corrections. Apply them, then write the files.

6. **Check the setup:** `python3 scripts/doctor.py`. Explain each FAIL and warning in one line
   with its fix. The usual remaining item is provider keys: say which one key would give the
   biggest improvement for this user's stages (see `docs/single-provider.md`), and that the
   pipeline runs without any keys on the builtin web research, just thinner.

7. **Suggest a first brief** built from their answers, e.g.
   `/gtm find <persona> at <segment> in <region> for <product>`, and mention `/gtm status <id>`
   and `/gtm resume <id>`.
