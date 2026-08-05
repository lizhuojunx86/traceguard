#!/usr/bin/env python3
"""Two checks on the corpus discriminator shipped in viberank #121.

1. Coverage. `collectCorpus()` walks ~/.claude/projects and emits a per-month
   {files, bytes} block. The server treats a month missing from that block as
   "emptied entirely". This prints which months the transcript tree can speak
   for against which months the submission actually carries, so the gap between
   "absent" and "deleted" is visible.

2. Source scope. The CLI builds totals with `ccusage daily --json`, which since
   ccusage v20 is the all-agent report. The corpus counts Claude Code
   transcripts only. This prints the per-agent split of the same totals.

Reimplements corpus.js month attribution exactly: a file is counted in every
month between its first and last "timestamp", inclusive, at full byte size.
"""

import collections
import json
import os
import re
import subprocess
import sys

ROOT = os.path.expanduser("~/.claude/projects")
TIMESTAMP = re.compile(rb'"timestamp"\s*:\s*"([^"]+)"')


def list_transcripts(root=ROOT):
    """Every .jsonl under root, recursively — matches listTranscripts()."""
    found = []
    for directory, _, files in os.walk(root):
        for name in files:
            if name.endswith(".jsonl"):
                found.append(os.path.join(directory, name))
    return found


def bounds_of(path):
    """First and last timestamp in a transcript — matches boundsOf()."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    stamps = TIMESTAMP.findall(data)
    if not stamps:
        return None
    return stamps[0].decode(), stamps[-1].decode()


def months_spanned(first, last):
    """Every month a file touches, inclusive — matches monthsSpanned()."""
    a, b = first[:7], last[:7]
    if not re.fullmatch(r"\d{4}-\d{2}", a):
        return []
    if not re.fullmatch(r"\d{4}-\d{2}", b) or b < a:
        return [a]
    months = []
    year, month = int(a[:4]), int(a[5:7])
    end_year, end_month = int(b[:4]), int(b[5:7])
    while (year, month) <= (end_year, end_month):
        months.append(f"{year}-{month:02d}")
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return months


def collect_corpus(root=ROOT):
    """Per-month {files, bytes} — matches collectCorpus()."""
    by_month = collections.defaultdict(lambda: {"files": 0, "bytes": 0})
    files = list_transcripts(root)
    dated = 0
    for path in files:
        bounds = bounds_of(path)
        if bounds is None:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        dated += 1
        for month in months_spanned(*bounds):
            by_month[month]["files"] += 1
            by_month[month]["bytes"] += size
    return dict(by_month), len(files), dated


def run_ccusage():
    """The submission the CLI would send, plus its per-agent split."""
    out = subprocess.run(
        ["npx", "-y", "ccusage@latest", "daily", "--by-agent", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def main():
    corpus, total_files, dated_files = collect_corpus()
    print(f"transcript tree: {total_files} .jsonl files, {dated_files} carrying a timestamp")
    print(f"months the corpus block can speak for: {', '.join(sorted(corpus))}\n")

    try:
        data = run_ccusage()
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"could not run ccusage ({exc}); coverage table skipped", file=sys.stderr)
        return 1

    by_month = collections.defaultdict(collections.Counter)
    per_agent_cost = collections.Counter()
    per_agent_days = collections.Counter()
    claudeless_days = 0

    for entry in data["daily"]:
        month = entry["period"][:7]
        agents = {a["agent"]: a["totalCost"] for a in entry.get("agents", [])}
        if "claude" not in agents:
            claudeless_days += 1
        for agent, cost in agents.items():
            by_month[month][agent] += cost
            per_agent_cost[agent] += cost
            per_agent_days[agent] += 1

    days = len(data["daily"])
    grand = sum(per_agent_cost.values())

    print("month     submitted    non-claude   share   corpus?")
    for month in sorted(by_month):
        totals = by_month[month]
        month_total = sum(totals.values())
        non_claude = month_total - totals.get("claude", 0.0)
        share = 100 * non_claude / month_total if month_total else 0.0
        covered = "yes" if month in corpus else "ABSENT"
        print(f"{month}  ${month_total:>10,.2f}  ${non_claude:>9,.2f}  {share:>6.2f}%   {covered}")

    print(f"\n{days} days submitted, {claudeless_days} of them with no Claude Code data at all")
    print(f"grand total ${grand:,.2f}\n")
    print("agent        cost        share    days")
    for agent, cost in per_agent_cost.most_common():
        print(f"{agent:<10} ${cost:>10,.2f}  {100 * cost / grand:>6.2f}%  {per_agent_days[agent]:>5d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
