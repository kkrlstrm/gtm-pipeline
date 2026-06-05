# Contributing

Thanks for improving the framework. The architecture has one rule that keeps it
maintainable: **agents are provider- and ICP-agnostic.** Almost every change is a
provider manifest, a context template, or storage — not an agent prompt.

## The rule of thumb

- Changing *how a capability is fulfilled* (a new vendor, a different endpoint, a quirk)?
  → that's a **provider** (`providers/<name>/manifest.yaml`, + `adapter.py` if gnarly).
  See [docs/writing-a-provider.md](docs/writing-a-provider.md).
- Changing *who you target*? → that's **context** (`context/*.md`), not code.
- Changing *which providers run in what order*? → that's **config**
  (`gtm.config.yaml` waterfalls). See [docs/swapping-providers.md](docs/swapping-providers.md).
- If you find yourself editing an `agents/*.md` to support a specific provider, stop —
  the provider-specific part belongs in the manifest.

## Before you open a PR

```bash
bash scripts/selftest.sh      # must pass (storage round-trip, adapter estimates, plan)
bash scripts/scrub-check.sh   # must exit 0 — no secrets, no source-org strings
```

- Keep adapters **stdlib-only** and reading secrets **from the environment only**. The
  framework never fetches secrets over the network — see [SECURITY.md](SECURITY.md).
- Preserve the three cross-stage invariants (member-ID URLs, empty-domain exclusion, dedup
  normalization parity) — they're documented in the agents and `docs/`.
- Add new provider keys to `.env.example`; add new capabilities/records to
  `docs/capabilities.md`.

## Reporting security issues

Open a private security advisory rather than a public issue. See [SECURITY.md](SECURITY.md).
