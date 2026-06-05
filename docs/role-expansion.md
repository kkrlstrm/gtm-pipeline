# Role & segment expansion (ambiguous titles → an explicit, reviewable set)

Providers match on the **literal** title string. So a brief that says "principal" and a config
that lists `["Principal"]` will silently miss every **Head of School**, **Headteacher**, and
**School Director** — and recall lost at sourcing is unrecoverable: the qualifier can drop a
wrong "principal" (a principal *engineer*, a PE-firm *principal*), but it can never add a head
of school you never searched for.

The framework already solved the *same* problem for geography, deterministically:
`config/region-expansion.yaml` is a versioned table ("Europe → 17 countries") the agent **reads**
rather than guesses. Titles and industries get the agentic sibling of that table — one the
orchestrator **generates from the brief**, you **approve at Gate #1**, and every stage then reads.

## How it works

1. **Infer (once, at brief time).** When you run `/gtm …`, the orchestrator turns the shorthand
   role and market into an **expansion profile**: the title equivalence class for the role *in
   this vertical*, a vertical-appropriate seniority band, the polysemous senses to exclude, and
   the industry/keyword/org-type variants for company discovery. It seeds from any persona
   `Titles:`/`Also-known-as:` in `context/personas.md` and unions — hand-authored variants always
   win.
2. **Review (Gate #1).** The profile is part of the plan, tagged inferred vs from-context. You
   edit it there — the recall/precision tradeoff becomes a reviewable artifact, not one person's
   tacit knowledge.
3. **Freeze.** On approval it's persisted to the list's `search_criteria.expansion`.
4. **Read (every stage).** `contact-sourcer` searches the frozen title set (union with persona
   titles); `company-discovery` uses the segment variants; the web `people-sourcer` subagent
   treats the set as equivalent matches; `contact-qualifier` uses `exclude_senses` to trim the
   wrong senses. **No stage re-infers** — inference happens once, and the "don't invent a title
   table" guardrail stays intact because every stage is *reading an approved table*.

The principle that keeps it safe is the one the framework runs everywhere: **expansion surfaces
at a gate, never silently.**

## The expansion profile

Stored under `search_criteria.expansion`:

```yaml
expansion:
  vertical: "K-12 education"        # drives the seniority interpretation
  roles:
    - requested: "principal"
      titles: ["Principal", "Head of School", "Headteacher", "School Director", "Upper School Head"]
      seniority: ["school-leader"]   # NOT forced into [VP, CxO, Director]
      exclude_senses: ["principal engineer", "principal consultant", "PE/finance principal", "principal architect"]
      source: "inferred + personas.md:Also-known-as"
  segment:
    industries: ["Primary/Secondary Education", "K-12 Schools", "School Districts"]
    keywords: ["public school district", "independent school", "charter network"]
    org_types: ["school district", "independent/private school", "charter network"]
    exclude: ["higher education", "tutoring/edtech vendors"]
  notes: "Widened 'principal' to building-leader synonyms; excluded corporate/engineering senses."
```

## Why a K-12 example

The built-in worked example (`examples/dach-fintech-cfos/`) is corporate-shaped: seniority bands
are `[VP, CxO, Director]`, the role is "CFO." Education, SLED, government, and nonprofit ladders
don't map onto that — a K-12 **principal** isn't a "Director," a **superintendent** isn't a
"CxO." That's exactly where literal-title matching fails hardest, so it's the case worth showing:

| Brief says | Literal match finds | Expansion finds | Excludes (precision) |
|---|---|---|---|
| "principal" (K-12) | "Principal" only | Principal, Head of School, Headteacher, School Director, Upper School Head | Principal Engineer, PE-firm Principal, Principal Architect |
| "superintendent" | "Superintendent" only | Superintendent, Chief Schools Officer, District Administrator, Head of Schools | Construction Superintendent |

The seniority for both is a `school-leader` / `district-cabinet` band — not a corporate one.

## Controlling it

`gtm.config.yaml` → `defaults.autonomy.role_expansion`:

- **`confirm`** (default) — generate the profile and surface it at Gate #1 for edit. Never silent.
- **`auto`** — generate and use it; still shown in the plan, just no separate pause.
- **`off`** — legacy: use only the literal persona `Titles:`. No inference.

## Pinning variants by hand

For variants you *always* want (or want to forbid), don't rely on inference — put them in
`context/personas.md`:

```
## Persona: Building Leader
Titles: [Principal]
Also-known-as: [Head of School, Headteacher, School Director, Upper School Head]
Seniority: [school-leader]
Disambiguation: exclude "Principal Engineer" / PE-firm "Principal" — require a school org.
```

These are unioned with whatever the orchestrator infers and always win — the deterministic floor
under the agentic layer.
