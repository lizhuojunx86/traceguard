#!/usr/bin/env node
/**
 * Drive claude-code-templates' own ConversationAnalyzer over a synthetic corpus
 * whose exact token totals are known, and compare what it reports against the
 * manifest.
 *
 * Nothing here reimplements their accounting: the analyzer class is required
 * straight out of a checkout of the upstream repo, `loadConversations()` does
 * its own recursive file discovery, its own parsing and its own
 * `calculateRealTokenUsage()`. This script only supplies the directory, a
 * minimal stateCalculator stub (two methods `loadConversations` calls purely
 * for status labelling), and the comparison.
 *
 * Usage:
 *   node run_check.js <repo-checkout> <fake-home> <manifest.json>
 *
 * Exit code 0 when reported totals equal the manifest, 1 when they do not.
 */

const path = require('path');
const fs = require('fs');

async function main() {
  const [repo, home, manifestPath] = process.argv.slice(2);
  if (!repo || !home || !manifestPath) {
    console.error('usage: node run_check.js <repo-checkout> <fake-home> <manifest.json>');
    process.exit(2);
  }

  const analyzerPath = path.join(repo, 'cli-tool/src/analytics/core/ConversationAnalyzer.js');
  if (!fs.existsSync(analyzerPath)) {
    console.error(`not found: ${analyzerPath}`);
    process.exit(2);
  }
  const ConversationAnalyzer = require(analyzerPath);
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));

  // `claudeDir` upstream is path.join(homedir(), '.claude') -- see
  // cli-tool/src/analytics.js:88 -- so hand it exactly that.
  const claudeDir = path.join(home, '.claude');

  // loadConversations() calls only these two, for status strings we do not assert on.
  const stateCalculator = {
    determineConversationStatus: () => 'inactive',
    determineConversationState: () => 'idle',
  };

  const analyzer = new ConversationAnalyzer(claudeDir);
  const conversations = await analyzer.loadConversations(stateCalculator);

  const reported = conversations.reduce(
    (acc, c) => {
      const u = c.tokenUsage || {};
      acc.input += u.inputTokens || 0;
      acc.output += u.outputTokens || 0;
      acc.cacheCreation += u.cacheCreationTokens || 0;
      acc.cacheRead += u.cacheReadTokens || 0;
      acc.messagesWithUsage += u.messagesWithUsage || 0;
      acc.total += c.tokens || 0; // what computeSummary() sums into summary.totalTokens
      return acc;
    },
    { input: 0, output: 0, cacheCreation: 0, cacheRead: 0, messagesWithUsage: 0, total: 0 }
  );

  const truth = manifest.all.correct;
  const perLine = manifest.all.if_summed_per_line;
  const n = (x) => x.toLocaleString('en-US');
  const ratio = (a, b) => (b === 0 ? 'n/a' : `x${(a / b).toFixed(3)}`);

  console.log('');
  console.log(`transcripts discovered      : ${conversations.length}` +
    `  (manifest: ${manifest.files.main_transcripts} main + ${manifest.files.subagent_transcripts} subagent)`);
  console.log(`distinct assistant messages : ${n(manifest.all.distinct_assistant_messages)}`);
  console.log(`assistant lines with usage  : ${n(manifest.all.assistant_jsonl_lines_with_usage)}`);
  console.log(`messagesWithUsage reported  : ${n(reported.messagesWithUsage)}`);
  console.log('');
  console.log('field                 reported        correct    if summed per line   reported/correct');
  const rows = [
    ['input_tokens', reported.input, truth.input_tokens, perLine.input_tokens],
    ['output_tokens', reported.output, truth.output_tokens, perLine.output_tokens],
    ['cache_creation', reported.cacheCreation, truth.cache_creation_input_tokens, perLine.cache_creation_input_tokens],
    ['cache_read', reported.cacheRead, truth.cache_read_input_tokens, perLine.cache_read_input_tokens],
    ['total (in+out)', reported.total, truth.total_input_plus_output, perLine.total_input_plus_output],
  ];
  for (const [label, got, want, naive] of rows) {
    console.log(
      `${label.padEnd(16)}${n(got).padStart(14)}${n(want).padStart(15)}${n(naive).padStart(22)}${ratio(got, want).padStart(19)}`
    );
  }
  console.log('');

  const mismatches = rows.filter(([, got, want]) => got !== want);
  const matchesNaive = rows.every(([, got, , naive]) => got === naive);

  if (mismatches.length === 0) {
    console.log('PASS  reported totals equal the manifest (each message counted once).');
    process.exit(0);
  }
  console.log(`FAIL  ${mismatches.length}/${rows.length} fields differ from ground truth.`);
  if (matchesNaive) {
    console.log('      Every field equals the per-line sum exactly, i.e. each assistant');
    console.log('      message is counted once per content-block line instead of once.');
  }
  process.exit(1);
}

main().catch((err) => {
  console.error(err);
  process.exit(2);
});
