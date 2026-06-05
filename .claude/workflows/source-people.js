export const meta = {
  name: 'source-people',
  description: 'Stage-1 people sourcing as a subagent fan-out: one people-sourcer per company finds contacts matching the target titles, cited. Token-efficient — per-company results stay in the script; only the merged contact list returns. The web_research path for people_search (great for companies the API providers missed).',
  whenToUse: 'When people_search runs via web_research: find specific people (name, title, LinkedIn URL) at each target company matching a persona, from the open web. Pairs with contact-sourcer, which dedupes + backfills domains + persists.',
  phases: [{ title: 'Source', detail: 'one people-sourcer subagent per company' }],
}

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

if (!Array.isArray(A.companies) || !A.companies.length) {
  return { error: 'no-companies', rows: [], hint: 'pass args = { companies:[{name,domain}], titles:[...] }' }
}
const titles = Array.isArray(A.titles) ? A.titles : []
const seniorities = Array.isArray(A.seniorities) ? A.seniorities : []
const perCompany = A.count_per_company || 5
const model = A.model || 'sonnet'

const SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    people: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        properties: {
          first_name: { type: 'string' }, last_name: { type: 'string' },
          title: { type: 'string' }, linkedin_url: { type: 'string', description: 'vanity /in/ URL, "" if not found' },
          source_url: { type: 'string' },
        },
        required: ['first_name', 'last_name', 'title', 'linkedin_url', 'source_url'],
      },
    },
  },
  required: ['people'],
}

function label(c) { return (c && (c.name || c.company_name || c.domain || c.company_domain)) || JSON.stringify(c) }
function prompt(c) {
  return [
    `Find up to ${perCompany} people CURRENTLY at this company who match the target role.`,
    `Company: ${label(c)}${(c.domain || c.company_domain) ? ` (${c.domain || c.company_domain})` : ''}`,
    titles.length ? `Target titles: ${titles.join(', ')}` : 'Target titles: leadership relevant to the ICP',
    seniorities.length ? `Seniority: ${seniorities.join(', ')}` : '',
    `Return first/last name, exact current title, LinkedIn URL, and the source URL. Verify current employment; never invent a name or URL.`,
  ].filter(Boolean).join('\n')
}

log(`Sourcing people at ${A.companies.length} compan${A.companies.length === 1 ? 'y' : 'ies'} · model ${model}`)

const out = await parallel(A.companies.map(c => () =>
  agent(prompt(c), {
    label: `source:${label(c).slice(0, 32)}`,
    phase: 'Source', schema: SCHEMA, agentType: 'people-sourcer', model,
  }).then(r => ({ company: c, people: (r && r.people) || [] }))
))

const rows = []
for (const res of out.filter(Boolean)) {
  const c = res.company
  for (const p of res.people) {
    if (!p.first_name && !p.last_name) continue
    rows.push({
      first_name: p.first_name, last_name: p.last_name,
      full_name: [p.first_name, p.last_name].filter(Boolean).join(' '),
      title: p.title, linkedin_url: p.linkedin_url,
      company_name: c.name || c.company_name,
      company_domain: c.domain || c.company_domain || '',
      source: 'web_research',
    })
  }
}

log(`Sourced ${rows.length} contacts across ${A.companies.length} companies.`)
return { rows, summary: { companies: A.companies.length, contacts: rows.length } }
