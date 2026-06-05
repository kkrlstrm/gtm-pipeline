export const meta = {
  name: 'discover-companies',
  description: 'Stage-0 company discovery as a Sonnet subagent fan-out: one company-researcher per search angle (look-alike from each seed, or each segment/geography), then merge + dedupe by domain. Token-efficient — angle results stay in the script; only the merged list returns.',
  whenToUse: 'When company_search runs via web_research and you need a real, cited list of companies that fit the ICP — look-alikes from seed companies, or firms matching a segment in a geography. Pairs with company-discovery, which then filters + persists the result.',
  phases: [{ title: 'Search', detail: 'one company-researcher subagent per angle' }],
}

function resolveArgs(a) {
  let x = a
  if (typeof x === 'string') { try { x = JSON.parse(x) } catch (e) { /* leave */ } }
  if (x && typeof x === 'object' && x.args && typeof x.args === 'object') x = x.args
  else if (x && typeof x === 'object' && x.input && typeof x.input === 'object') x = x.input
  return x && typeof x === 'object' ? x : {}
}
const A = resolveArgs(args)

const seeds = Array.isArray(A.seeds) ? A.seeds : (Array.isArray(A.seed_companies) ? A.seed_companies : [])
const segments = Array.isArray(A.segments) ? A.segments : []
const geographies = Array.isArray(A.geographies) ? A.geographies : (A.geography ? [A.geography] : [])
const criteria = A.criteria || ''
const perAngle = A.count_per_angle || 15
const model = A.model || 'sonnet'

// Build search angles. Each becomes one company-researcher subagent.
const angles = []
for (const s of seeds) angles.push({ kind: 'look-alike', label: `look-alike of ${s}`, brief: `Find companies similar to "${s}" (same kind of business, comparable size/market).` })
for (const seg of segments) {
  const geoStr = geographies.length ? ` in ${geographies.join(', ')}` : ''
  angles.push({ kind: 'segment', label: `${seg}${geoStr}`, brief: `Find companies matching this segment${geoStr}: ${seg}.` })
}
if (!angles.length) angles.push({ kind: 'criteria', label: 'criteria', brief: criteria || 'Find companies matching the ICP.' })

if (!angles.length) return { error: 'no-angles', rows: [] }

const SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    companies: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        properties: {
          company_name: { type: 'string' },
          company_domain: { type: 'string', description: 'primary domain, "" if unknown' },
          reason: { type: 'string', description: 'one line why it fits' },
          source_url: { type: 'string' },
        },
        required: ['company_name', 'company_domain', 'reason', 'source_url'],
      },
    },
  },
  required: ['companies'],
}

function prompt(angle) {
  return [
    `Find up to ${perAngle} REAL companies for this angle.`,
    `Angle (${angle.kind}): ${angle.brief}`,
    geographies.length ? `Geography: ${geographies.join(', ')}.` : '',
    criteria ? `Extra ICP criteria: ${criteria}` : '',
    `Return name + primary domain + a one-line reason + the source URL you used. Real companies only; never invent a domain.`,
  ].filter(Boolean).join('\n')
}

log(`Discovering across ${angles.length} angle(s) · model ${model}`)

const out = await parallel(angles.map(angle => () =>
  agent(prompt(angle), {
    label: `discover:${angle.label.slice(0, 36)}`,
    phase: 'Search', schema: SCHEMA, agentType: 'company-researcher', model,
  }).then(r => ({ angle: angle.label, companies: (r && r.companies) || [] }))
))

// Merge + dedupe by normalized domain (fallback to lowercased name).
const seen = new Set(), rows = []
for (const res of out.filter(Boolean)) {
  for (const c of res.companies) {
    const dom = (c.company_domain || '').toLowerCase().replace(/^https?:\/\//, '').replace(/^www\./, '').split('/')[0].replace(/\.$/, '')
    const key = dom || (c.company_name || '').trim().toLowerCase()
    if (!key || seen.has(key)) continue
    seen.add(key)
    rows.push({
      company_name: c.company_name, company_domain: dom,
      discovery_reason: c.reason, sources: c.source_url ? [c.source_url] : [],
      source: 'web_research',
    })
  }
}

log(`Discovered ${rows.length} unique companies across ${angles.length} angle(s).`)
return { rows, summary: { angles: angles.length, companies: rows.length } }
