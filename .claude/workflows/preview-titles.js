export const meta = {
  name: 'preview-titles',
  description: 'Title preview before sourcing: on a small sample of target companies, one people-sourcer per company lists the REAL titles held in the target function, and the script marks which of them the approved title set would match literally. Cheap (a few agents), and it shows the reviewer what the expansion catches and misses before any sourcing is paid for.',
  whenToUse: 'At the role-expansion gate, after the orchestrator has inferred the title set and before people_search runs. Pass 2-5 sample companies (ICP seeds, or the first discovered companies) plus the inferred titles and excluded senses.',
  phases: [{ title: 'Sample', detail: 'one people-sourcer per sample company lists the titles actually in use' }],
}

// args: { companies:[{name,domain}], titles:[...], exclude_senses?:[...], function?: 'finance',
//         sample?: 3, model?: 'sonnet', agentType?: 'people-sourcer' | '' }
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
if (!Array.isArray(A.companies) || !A.companies.length || !Array.isArray(A.titles) || !A.titles.length) {
  return { error: 'bad-args', hint: 'pass args = { companies:[{name,domain}], titles:[...], exclude_senses?:[...] }' }
}
const sample = A.companies.slice(0, A.sample || 3)
const titles = A.titles
const excl = Array.isArray(A.exclude_senses) ? A.exclude_senses : []
const fn = A.function || 'the function these titles belong to'
const model = A.model || 'sonnet'
const AGENT = A.agentType === undefined ? 'people-sourcer' : A.agentType
function agentOpt(t) { return t ? { agentType: t } : {} }

const SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    observed: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        properties: {
          title: { type: 'string', description: 'the exact title as published' },
          person: { type: 'string', description: 'who holds it, "" if the page does not say' },
          source_url: { type: 'string' },
        },
        required: ['title', 'person', 'source_url'],
      },
    },
  },
  required: ['observed'],
}

function label(c) { return (c && (c.name || c.company_name || c.domain || c.company_domain)) || JSON.stringify(c) }
function prompt(c) {
  return [
    `List the titles actually in use for ${fn} at this company — every title you find in that function, not only the ones below.`,
    `Company: ${label(c)}${(c.domain || c.company_domain) ? ` (${c.domain || c.company_domain})` : ''}`,
    `For reference, the campaign currently plans to search for: ${titles.join('; ')}.`,
    `Include titles that mean the same job under a different name, and senior/junior neighbours in the same function. Up to 15.`,
    `Only titles you saw on a page you opened (team page, directory, org chart, LinkedIn result). Cite it. Never invent a title.`,
  ].join('\n')
}

log(`Previewing titles at ${sample.length} sample compan${sample.length === 1 ? 'y' : 'ies'} · model ${model}`)

const out = await parallel(sample.map(c => () =>
  agent(prompt(c), {
    label: `preview:${label(c).slice(0, 32)}`,
    phase: 'Sample', schema: SCHEMA, ...agentOpt(AGENT), model,
  }).then(r => r ? { company: label(c), observed: r.observed || [] } : { company: label(c), failed: true })
))

// Literal match = what a provider would find if you searched the set as typed. The gap
// between "observed" and "matched_literal" is the recall the expansion has to cover.
const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9 ]/g, ' ').replace(/\s+/g, ' ').trim()
// An entry that normalizes to "" would make includes('') match every title.
const set = titles.map(norm).filter(Boolean)
const exclN = excl.map(norm).filter(Boolean)
const seen = new Map()
const failed = []
for (const res of out) {
  if (!res || res.failed) { failed.push(res ? res.company : '?'); continue }
  for (const o of res.observed) {
    const t = norm(o.title)
    if (!t) continue
    const entry = seen.get(t) || { title: o.title, companies: [], examples: [] }
    if (!entry.companies.includes(res.company)) entry.companies.push(res.company)
    if (entry.examples.length < 2) entry.examples.push({ person: o.person, source_url: o.source_url })
    entry.matched_literal = set.some(s => t === s || t.includes(s))
    entry.hits_excluded_sense = exclN.some(s => t.includes(s))
    seen.set(t, entry)
  }
}
const observed = [...seen.values()].sort((a, b) => b.companies.length - a.companies.length)
const unmatched = observed.filter(o => !o.matched_literal && !o.hits_excluded_sense)
log(`Observed ${observed.length} distinct titles; ${unmatched.length} not in the set.${failed.length ? ` FAILED: ${failed.join(', ')}` : ''}`)
return {
  observed, unmatched, failed,
  summary: { sampled: sample.length, distinct_titles: observed.length, not_in_set: unmatched.length, failed: failed.length },
}
