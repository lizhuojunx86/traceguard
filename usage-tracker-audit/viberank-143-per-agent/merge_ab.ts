/**
 * Run viberank's own mergeMachineContribution over a real --by-agent report.
 *
 * The point of importing their entry point rather than restating the rule is
 * that the comparison is then against the code that runs, not against my
 * reading of it. #143 changed this function; everything else here is inert.
 *
 *   node merge_ab.ts <path-to-ccusage.ts> <path-to-cc-byagent.json> <nc_keep>
 *
 * For every day carrying both Claude and another tool, it builds the pair the
 * drift path sees:
 *
 *   prior     the day as observed
 *   incoming  the same day with Claude pruned to CLAUDE_KEEP, and every other
 *             agent scaled by nc_keep (1 = re-reported unchanged, 0 = gone)
 *
 * then merges with acceptLower = true, which is the verdict "the Claude corpus
 * shows this month was deleted", and reports how much non-Claude money came
 * out the other side.
 */
import { readFileSync } from "node:fs";

const [, , modulePath, reportPath, ncArg] = process.argv;
if (!modulePath || !reportPath) {
  console.error("usage: node merge_ab.ts <ccusage.ts> <cc-byagent.json> [nc_keep]");
  process.exit(2);
}

const { mergeMachineContribution } = await import(modulePath);

/** How much of the Claude slice the incoming report still finds. */
const CLAUDE_KEEP = 0.4;
/** How much of every other agent's slice it still finds. */
const NC_KEEP = Number(ncArg ?? 1);

const FIELDS = [
  "inputTokens",
  "outputTokens",
  "cacheCreationTokens",
  "cacheReadTokens",
  "totalTokens",
  "totalCost",
] as const;

const isClaude = (name: string) => name.startsWith("claude");
const scale = (s: any, k = 1) =>
  Object.fromEntries(FIELDS.map((f) => [f, s[f] * k])) as any;
const sum = (slices: any[]) =>
  slices.reduce((a, b) => Object.fromEntries(FIELDS.map((f) => [f, a[f] + b[f]])) as any);

const report = JSON.parse(readFileSync(reportPath, "utf8"));

let observed = 0;
let kept = 0;
let claudeAfter = 0;
let mixedDays = 0;
const moved: string[] = [];

for (const row of report.daily) {
  const slices = row.agents ?? [];
  const names: string[] = slices.map((s: any) => s.agent);
  if (!names.some(isClaude) || names.length < 2) continue;
  mixedDays++;

  const priorBreak: Record<string, any> = {};
  const incomingBreak: Record<string, any> = {};
  for (const s of slices) {
    priorBreak[s.agent] = scale(s);
    if (isClaude(s.agent)) incomingBreak[s.agent] = scale(s, CLAUDE_KEEP);
    else if (NC_KEEP > 0) incomingBreak[s.agent] = scale(s, NC_KEEP);
  }

  const contribution = (breakdowns: Record<string, any>) => ({
    ...sum(Object.values(breakdowns)),
    modelsUsed: row.modelsUsed ?? [],
    agents: Object.keys(breakdowns),
    agentBreakdowns: breakdowns,
  });

  const { contributions } = mergeMachineContribution(
    { m1: contribution(priorBreak) },
    "m1",
    contribution(incomingBreak),
    true
  );
  const after = contributions["m1"].agentBreakdowns ?? {};

  for (const s of slices) {
    if (isClaude(s.agent)) {
      claudeAfter += after[s.agent]?.totalCost ?? 0;
      continue;
    }
    observed += s.totalCost;
    const survived = after[s.agent]?.totalCost ?? 0;
    kept += survived;
    if (Math.abs(survived - s.totalCost) > 1e-9) moved.push(`${row.period} ${s.agent}`);
  }
}

console.log(`module                 ${modulePath}`);
console.log(`claude keep / nc keep  ${CLAUDE_KEEP} / ${NC_KEEP}`);
console.log(`mixed days             ${mixedDays}`);
console.log(`non-claude observed    ${observed.toFixed(2)}`);
console.log(`non-claude after merge ${kept.toFixed(2)}`);
console.log(`non-claude slices moved ${moved.length}`);
console.log(`claude after merge     ${claudeAfter.toFixed(2)}`);
if (moved.length) console.log(`first moved: ${moved.slice(0, 5).join(", ")}`);
