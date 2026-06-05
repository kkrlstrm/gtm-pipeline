export const meta = {
  name: 'score-leads',
  description: 'Stage-2 qualification as a Haiku batch fan-out: contacts are chunked and each chunk is scored by a lead-scorer subagent against the ICP rubric (QUALIFY/MAYBE/SKIP + 0-10 + persona/segment + reason). Cheap and consistent — Haiku, batched, results stay in the script.',
  whenToUse: 'When the qualifier needs to score many sourced contacts against segments.md/personas.md/exclusions.md. Returns one scored row per contact for the qualifier to review (Gate #2) and persist.',
  phases: [{ title: 'Score', detail: 'one Haiku lead-scorer per batch of contacts' }],
}

function resolveArgs(a) {
  let x = a
  if (typeof x === 'string') { try { x = JSON.parse(x) } catch (e) { /* leave */ } }
  if (x && typeof x === 'object' && !Array.isArray(x.contacts)) {
    if (x.args && Array.isArray(x.args.contacts)) x = x.args
    else if (x.input && Array.isArray(x.input.contacts)) x = x.input
  }
  return x && typeof x === 'object' ? x : {}
}
const A = resolveArgs(args)

if (!Array.isArray(A.contacts) || !A.contacts.length) {
  return { error: 'no-contacts', rows: [], hint: 'pass args = { contacts:[{id,title,company_name,...}], rubric, personas, exclusions }' }
}
const rubric = A.rubric || 'No explicit rubric provided — treat every in-persona, non-excluded contact as QUALIFY (single tier).'
const personas = A.personas || ''
const exclusions = A.exclusions || ''
const batchSize = A.batch_size || 15      // many contacts per Haiku agent = few agents = cheap
const model = A.model || 'haiku'

const RESULT_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    results: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        properties: {
          id: { type: 'number' },
          qualification_status: { type: 'string', enum: ['QUALIFY', 'MAYBE', 'SKIP'] },
          qualification_score: { type: 'number' },
          matched_persona: { type: 'string' },
          company_segment: { type: 'string' },
          reason: { type: 'string' },
        },
        required: ['id', 'qualification_status', 'qualification_score', 'matched_persona', 'company_segment', 'reason'],
      },
    },
  },
  required: ['results'],
}

// chunk contacts into batches
const batches = []
for (let i = 0; i < A.contacts.length; i += batchSize) batches.push(A.contacts.slice(i, i + batchSize))

function prompt(batch) {
  return [
    `Score each contact below against this ICP rubric.`,
    `\n## Personas\n${personas}`,
    `\n## Segments + scoring rubric + thresholds\n${rubric}`,
    exclusions ? `\n## Exclusions (apply FIRST — match => SKIP, score 0)\n${exclusions}` : '',
    `\n## Contacts (score every one; preserve each id)\n${JSON.stringify(batch, null, 2)}`,
    `\nReturn one result per contact via the structured tool: id, qualification_status, qualification_score, matched_persona, company_segment, reason.`,
  ].filter(Boolean).join('\n')
}

log(`Scoring ${A.contacts.length} contacts in ${batches.length} batch(es) of ${batchSize} · model ${model}`)

const out = await parallel(batches.map((batch, i) => () =>
  agent(prompt(batch), {
    label: `score:batch-${i + 1}`,
    phase: 'Score', schema: RESULT_SCHEMA, agentType: 'lead-scorer', model,
  }).then(r => (r && r.results) || [])
))

const rows = out.filter(Boolean).flat()
const tally = rows.reduce((m, r) => (m[r.qualification_status] = (m[r.qualification_status] || 0) + 1, m), {})
log(`Scored ${rows.length}/${A.contacts.length}: ${JSON.stringify(tally)}`)
return { rows, summary: { contacts_in: A.contacts.length, scored: rows.length, tally } }
