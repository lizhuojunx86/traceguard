#!/usr/bin/env python3
"""Read back which model each probe subagent actually ran on.

Reads ~/.claude/projects directly. No database, no ingest, no network — the
answer is in the transcripts the moment the probes finish.

    python3 read_probe_result.py            # subagents from the last 60 minutes
    python3 read_probe_result.py --minutes 180

Prints, per subagent transcript: the agentType Claude Code recorded, the model
that actually ran, and the model the *parent* main thread was running at the
same time. The parent column is what makes the result readable: "ran haiku" only
means something if the parent was not already on haiku.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

ROOT = Path.home() / ".claude" / "projects"


def first_assistant(path: Path) -> tuple[str | None, str | None]:
    """Return (model, cc_version) from the first assistant line, or (None, None)."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                if msg.get("model"):
                    return msg["model"], rec.get("version")
    except OSError:
        pass
    return None, None


def models_in(path: Path) -> dict[str, int]:
    """Every distinct model on assistant lines, with counts."""
    out: dict[str, int] = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                m = (rec.get("message") or {}).get("model")
                if m:
                    out[m] = out.get(m, 0) + 1
    except OSError:
        pass
    return out


def _agent_dirs() -> list[Path]:
    """Every plausible `.claude/agents` directory, nearest first.

    The first version looked only in `Path.cwd()/.claude/agents`. Run from the
    script's own directory — the most natural place to run it from — that found
    nothing and printed NO DEFINITION FILE FOUND for every probe. The
    degradation was honest and the trigger was the obvious invocation, which is
    the worst combination: the lazy path has to be the correct path, or the
    check quietly stops checking.

    So: walk up from cwd and from this file, then fall back to the user level.
    """
    seen: list[Path] = []
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for d in (start, *start.parents):
            cand = d / ".claude" / "agents"
            if cand.is_dir() and cand not in seen:
                seen.append(cand)
    user = Path.home() / ".claude" / "agents"
    if user.is_dir() and user not in seen:
        seen.append(user)
    return seen


AGENT_DIRS = _agent_dirs()


def declared_model(agent_type: str) -> str | None:
    """The `model:` value the definition file actually declares, or None.

    The first version of this script guessed intent from the model that ran
    ("if 'haiku' in model"). It then judged all four probes of the second round
    wrongly — most sharply `inherit`, where running the parent's model IS the
    documented behaviour and the script called it "NOT honoured". A verdict
    function that cannot see what was asked for should not be printing verdicts.
    """
    for d in AGENT_DIRS:
        f = d / f"{agent_type}.md"
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        head = text.split("---", 2)
        block = head[1] if len(head) > 2 else text
        for line in block.splitlines():
            if line.startswith("model:"):
                return line.split(":", 1)[1].strip()
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--all", action="store_true", help="ignore the time window")
    args = ap.parse_args()

    if not ROOT.is_dir():
        print(f"not found: {ROOT}")
        return 1

    cutoff = time.time() - args.minutes * 60
    metas = [
        p
        for p in ROOT.glob("*/*/subagents/**/agent-*.meta.json")
        if args.all or p.stat().st_mtime >= cutoff
    ]
    if not metas:
        print(
            f"no subagent transcripts modified in the last {args.minutes} min.\n"
            "Run the probes first, or pass --minutes / --all."
        )
        return 1

    rows = []
    for meta in sorted(metas, key=lambda p: p.stat().st_mtime):
        try:
            agent_type = json.loads(meta.read_text(encoding="utf-8")).get("agentType", "?")
        except (OSError, json.JSONDecodeError):
            agent_type = "?"
        jsonl = meta.with_name(meta.name.replace(".meta.json", ".jsonl"))
        model, ver = first_assistant(jsonl)

        # parent main transcript: .../<project>/<sessionId>/subagents/... -> <project>/<sessionId>.jsonl
        session_dir = meta.parents[1]
        while session_dir.name != "subagents" and session_dir.parent != session_dir:
            if (session_dir.parent / f"{session_dir.name}.jsonl").exists():
                break
            session_dir = session_dir.parent
        parent = session_dir.parent / f"{session_dir.name}.jsonl"
        parent_models = models_in(parent) if parent.exists() else {}
        parent_desc = (
            ", ".join(f"{k} x{v}" for k, v in sorted(parent_models.items(), key=lambda kv: -kv[1]))
            or "(parent transcript not found)"
        )
        rows.append((agent_type, model or "?", ver or "?", parent_desc))

    w = max(len(r[0]) for r in rows) + 2
    print(f"{'agentType'.ljust(w)}{'model that RAN'.ljust(32)}{'cc'.ljust(10)}parent main thread")
    print("-" * (w + 32 + 10 + 20))
    for a, m, v, p in rows:
        print(f"{a.ljust(w)}{m.ljust(32)}{v.ljust(10)}{p}")

    print()
    probes = [r for r in rows if r[0].startswith("probe-")]
    if not probes:
        print("No probe-* subagents in this window. Did the invocations run in this project?")
        return 1

    print("VERDICT  (asked-for value read from the definition file, not guessed)")
    for a, m, v, p in probes:
        parent = p.split(" x")[0]
        asked = declared_model(a)
        if asked is None:
            print(f"  {a}: ran {m}, parent {parent}. NO DEFINITION FILE FOUND — cannot judge.")
        elif m == "<synthetic>":
            print(f"  {a}: asked {asked!r} -> HARD ERROR, no model ran. Value rejected.")
        elif asked.strip().lower() == "inherit":
            ok = "as documented" if m == parent else "NOT as documented"
            print(f"  {a}: asked 'inherit' -> ran {m}, parent {parent}. Inherit {ok}.")
        elif m == parent and asked.strip().lower() not in m.lower():
            print(f"  {a}: asked {asked!r} -> ran the PARENT model {m}. Field NOT honoured.")
        else:
            print(f"  {a}: asked {asked!r} -> ran {m}, parent {parent}. Field honoured.")
    print()
    print("Note: 'honoured' here means the run model is consistent with the asked-for")
    print("value. An alias such as 'sonnet' resolves to the current model in that tier,")
    print("so it pins a tier, not a model — read the run column, not the asked column.")
    print()
    print("Record the cc version above. Behaviour here changed at least once already")
    print("(built-in Explore, v2.1.198), so a result without a version is not reproducible.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
