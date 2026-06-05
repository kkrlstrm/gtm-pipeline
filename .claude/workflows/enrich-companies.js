export const meta = {
  name: 'enrich-companies',
  description: 'Account-level company enrichment — Fire Enrich\'s multi-phase flow, baked into Claude-orchestrated subagents: per company, parallel dimension researchers (basics / financial / tech / leadership / signals + custom) then a synthesis+verify pass. Schema-validated, source-cited.',
  whenToUse: 'After a company seed list is chosen and BEFORE people enrichment: take the accepted companies and build deep, cited account intel (funding, tech stack, leadership, hiring/why-now signals, description) so the qualifier scores on substance and personalization has fuel. The list and custom fields change per run; the orchestration does not.',
  phases: [
    { title: 'Dimensions', detail: 'parallel subagent per company per dimension (Fire Enrich\'s phases)' },
    { title: 'Synthesize', detail: 'merge a company\'s dimensions into one record + verify each claim against its source' },
  ],
}

// ---------------------------------------------------------------------------
// args (JSON object; tolerant of string / {args} / {input} wrapping):
//   companies     : (required) array of { name, domain?, linkedin_url?, ...any context }
//   custom_fields : (optional) array of extra fields to research, e.g.
//                   ["24/7 support?", "SOC2 status", "primary ICP they sell to"]
//   dimensions    : (optional) override the default Fire-Enrich dimension set
//   model         : (optional) 'haiku' (DEFAULT for dimension breadth) | 'sonnet' | 'opus' | 'inherit'
//   synthModel    : (optional) model for the synthesize+verify pass (DEFAULT 'sonnet')
//   useFirecrawl  : (optional bool) if FIRECRAWL_API_KEY is set, prefer the firecrawl
//                   adapter for cleaner page content (Claude still does the extraction).
// Returns { rows: [companyIntelRecord...], summary } — rows drop straight into
// `storage/cli.py upsert_companies`.
// ---------------------------------------------------------------------------

function resolveArgs(a) {
  let x = a
  if (typeof x === 'string') { try { x = JSON.parse(x) } catch (e) { /* leave */ } }
  if (x && typeof x === 'object' && !Array.isArray(x.companies)) {
    if (x.args && Array.isArray(x.args.companies)) x = x.args
    else if (x.input && Array.isArray(x.input.companies)) x = x.input
  }
  return x && typeof x === 'object' ? x : {}
}
const A = resolveArgs(args)
log(`args arrived as ${typeof args}; companies = ${Array.isArray(A.companies) ? A.companies.length : 'none'}`)

if (!Array.isArray(A.companies) || !A.companies.length) {
  return { error: 'no-companies', rows: [], hint: 'pass args = { companies:[{name,domain}], custom_fields?:[] }' }
}

const customFields = Array.isArray(A.custom_fields) ? A.custom_fields : []
const dimModel  = A.model || 'haiku'        // cheap breadth across many companies
const synthModel = A.synthModel || 'sonnet' // careful merge + source verification
const useFirecrawl = !!A.useFirecrawl
function modelOpt(m) { return m && m !== 'inherit' ? { model: m } : {} }

// Fire Enrich's phases, as discrete research dimensions.
const DEFAULT_DIMENSIONS = [
  { key: 'basics',     ask: 'HQ location (city, country), year founded, employee count (a number or tight range), industry and sub-industry, and a one-paragraph plain description of what they do.' },
  { key: 'financial',  ask: 'Funding stage (e.g. Seed/Series A/Public/Bootstrapped), total capital raised, most recent round (amount + date), and named investors.' },
  { key: 'tech',       ask: 'Core technology stack and notable tools — languages, frameworks, cloud/infra, and major SaaS they run on (from job posts, BuiltWith-style signals, engineering blog).' },
  { key: 'leadership', ask: 'The CEO and 1-3 of the most relevant leaders for a sales conversation (name + exact title).' },
  { key: 'signals',    ask: 'Recent buying / expansion signals and the "why now": hiring surges (esp. relevant roles), new funding, product launches, market or regulatory moves, M&A. Prefer the last 12 months.' },
]
const dimensions = Array.isArray(A.dimensions) && A.dimensions.length ? A.dimensions : DEFAULT_DIMENSIONS
if (customFields.length) {
  dimensions.push({ key: 'custom', ask: `Find these specific custom fields: ${customFields.join('; ')}.` })
}

function companyLabel(c) {
  if (c && typeof c === 'object') return c.name || c.company_name || c.domain || c.company_domain || JSON.stringify(c)
  return String(c)
}
function companyContext(c) {
  if (!c || typeof c !== 'object') return ''
  return Object.entries(c).map(([k, v]) => `${k}: ${v}`).join('\n')
}

const FIRECRAWL_HINT = useFirecrawl
  ? 'If you need a page\'s full content, you MAY scrape it with `python3 providers/firecrawl/adapter.py --capability scrape --input \'{"url":"<url>"}\'` for cleaner text; otherwise use WebSearch/WebFetch.'
  : 'Use WebSearch to find sources and WebFetch to read them.'

const GUARDRAILS = [
  'Never invent a value. If you cannot verify a field from a real page you opened, leave it "" and say so in note. A blank, honest field beats a confident guess.',
  'Every non-empty field must be supported by a source URL you actually opened — list those in sources[].',
  'Prefer the company\'s own site, reputable press, filings, and job posts. Corroborate funding/financials from a second source when you can.',
  'Be concrete and current; prefer the last 12 months for signals. Note the date of anything time-sensitive.',
]

const DIM_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    dimension: { type: 'string' },
    fields: { type: 'object', additionalProperties: true, description: 'flat map of field -> value (string or array) for this dimension; "" if unknown' },
    sources: { type: 'array', items: { type: 'string' }, description: 'URLs actually opened that support the fields' },
    note: { type: 'string', description: 'caveats / what could not be found' },
  },
  required: ['dimension', 'fields', 'sources', 'note'],
}

const INTEL_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    company_name: { type: 'string' },
    company_domain: { type: 'string' },
    linkedin_url: { type: 'string' },
    industry: { type: 'string' },
    country: { type: 'string' },
    intel: {
      type: 'object', additionalProperties: true,
      properties: {
        founded_year: { type: 'string' }, hq_location: { type: 'string' },
        description: { type: 'string' }, estimated_employees: { type: 'string' },
        sub_industry: { type: 'string' }, funding_stage: { type: 'string' },
        total_raised: { type: 'string' }, last_round: { type: 'string' },
        investors: { type: 'array', items: { type: 'string' } },
        tech_stack: { type: 'array', items: { type: 'string' } },
        leadership: { type: 'array', items: { type: 'object', additionalProperties: true } },
        signals: { type: 'array', items: { type: 'string' } },
        custom: { type: 'object', additionalProperties: true },
      },
    },
    sources: { type: 'array', items: { type: 'string' } },
    verified: { type: 'boolean' },
    note: { type: 'string' },
  },
  required: ['company_name', 'intel', 'sources', 'verified'],
}

function dimPrompt(c, d) {
  return [
    `Research ONE dimension of this company.`,
    `Company: **${companyLabel(c)}**`,
    companyContext(c) ? `Known context:\n${companyContext(c)}` : '',
    `\nDimension "${d.key}": ${d.ask}`,
    `\n${FIRECRAWL_HINT}`,
    `\nRules:`, ...GUARDRAILS.map(g => `- ${g}`),
    `\nReturn via the structured tool: dimension="${d.key}", fields={...}, sources=[urls you opened], note.`,
  ].filter(Boolean).join('\n')
}

function synthPrompt(c, dimResults) {
  return [
    `Synthesize and VERIFY account intel for: **${companyLabel(c)}**`,
    companyContext(c) ? `Known context:\n${companyContext(c)}` : '',
    `\nPer-dimension research (each with its own sources):`,
    JSON.stringify(dimResults, null, 2),
    `\nProduce ONE company intel record:`,
    `- Fold the dimension fields into the intel object (founded_year, hq_location, description, estimated_employees, sub_industry, funding_stage, total_raised, last_round, investors[], tech_stack[], leadership[], signals[], custom{}).`,
    `- For each claim you keep, confirm it is actually supported by one of the cited sources; DROP anything unsupported and leave that field empty.`,
    `- Set company_domain to the best known domain. Collect all corroborating URLs into sources[].`,
    `- Set verified=true only if the core fields (what they do + at least one of funding/size/signals) are backed by real sources.`,
    `- Keep it factual and current. Put caveats in note.`,
    `\nReturn via the structured tool. company_name must be "${companyLabel(c)}".`,
  ].filter(Boolean).join('\n')
}

log(`Enriching ${A.companies.length} compan${A.companies.length === 1 ? 'y' : 'ies'} across ${dimensions.length} dimensions · dim model: ${dimModel} · synth model: ${synthModel} · firecrawl: ${useFirecrawl}`)

// Pipeline, no barrier: each company runs its dimension fan-out then synthesis
// independently, so a fast company synthesizes while others are still researching.
const out = await pipeline(
  A.companies,
  // Stage 1 — parallel dimension subagents (Fire Enrich's phases) for this company.
  (c) => parallel(dimensions.map(d => () =>
    agent(dimPrompt(c, d), {
      label: `dim:${d.key}:${companyLabel(c).slice(0, 24)}`,
      phase: 'Dimensions', schema: DIM_SCHEMA, ...modelOpt(dimModel),
    }).then(r => r || { dimension: d.key, fields: {}, sources: [], note: 'agent skipped' })
  )),
  // Stage 2 — synthesize + verify into one record.
  (dimResults, c) => agent(synthPrompt(c, dimResults), {
    label: `synth:${companyLabel(c).slice(0, 32)}`,
    phase: 'Synthesize', schema: INTEL_SCHEMA, ...modelOpt(synthModel),
  }).then(rec => {
    if (!rec) return null
    rec.enriched = true                       // mark for storage
    if (!rec.company_domain && c && typeof c === 'object') rec.company_domain = c.domain || c.company_domain || ''
    return rec
  }),
)

const rows = out.filter(Boolean)
const verified = rows.filter(r => r.verified).length
log(`Done: ${rows.length}/${A.companies.length} companies enriched, ${verified} verified.`)

return {
  rows,                                       // -> storage/cli.py upsert_companies { companies: rows }
  summary: {
    companies_in: A.companies.length,
    enriched: rows.length,
    verified,
    unverified: rows.length - verified,
    dimensions: dimensions.map(d => d.key),
  },
}
