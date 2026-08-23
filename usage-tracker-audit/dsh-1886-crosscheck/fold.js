// Folds a DSH sessions root through a real usage-projection.ts and prints the
// four buckets as JSON. Nothing is reimplemented: apply() and view() come from
// the vendor/fork source, transpiled but otherwise untouched.
import fs from 'node:fs'
import path from 'node:path'

const variant = process.env.VARIANT
const root = process.argv[process.argv.length - 1]
const mod = await import(`${process.env.BUILD ?? "./build"}/${variant}.js`)
const def = mod.tokenUsageProjectionDefinition

// The definition shape changed upstream between 47f9438 and b150a551b8:
// old = { schema, view, stateVersion }, new = { stateSchema, wire: { view } }.
const view = def.wire?.view ?? def.view
const stateVersion = def.stateVersion

const sessions = []
const walk = (d) => {
  for (const e of fs.readdirSync(d, { withFileTypes: true })) {
    const p = path.join(d, e.name)
    if (e.isDirectory()) walk(p)
    else if (e.name === 'session.jsonl') sessions.push(p)
  }
}
walk(root)
sessions.sort()

const totals = { uncachedInputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0 }
const per = []
for (const file of sessions) {
  let state = def.init()
  const lines = fs.readFileSync(file, 'utf8').split('\n').filter(Boolean)
  for (const line of lines) {
    const ev = JSON.parse(line)
    if (ev.type === 'session') continue      // header, not an event
    state = def.apply(state, ev)
  }
  const v = view(state)
  per.push({ session: path.basename(path.dirname(file)), ...v })
  for (const k of Object.keys(totals)) totals[k] += v[k] ?? 0
}
if (process.env.VERBOSE) console.error(JSON.stringify({ variant, stateVersion, per }, null, 2))
console.log(JSON.stringify({ ...totals, stateVersion }))
