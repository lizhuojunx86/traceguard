"""Report agent definitions that do not pin a model.

A Claude Code subagent whose definition omits ``model:`` runs on whatever the
main thread is running. That is documented upstream and is often what you
want. It stops being what you want when the main thread is on a frontier
model and the subagent is doing work you would never have chosen a frontier
model for — because at that point nobody chose anything, and the cost lands
anyway.

This module answers the cheap half of that question: *which* of your agent
definitions leave the field unset. It reads only the YAML frontmatter of
``.claude/agents/**/*.md``, looks at two keys (``name`` and ``model``), and
prints file paths. It opens no transcripts, makes no network calls, and needs
nothing installed — it is stdlib-only on purpose, so it can be run as a
single file::

    python -m traceguard.routing_audit.agent_lint
    python agent_lint.py ~/.claude/agents ./.claude/agents

The expensive half — what the unpinned ones actually ran and what that cost —
needs a trace store, and is what the rest of ``routing_audit`` is for.

THREE STATES, NOT TWO. ``model:`` absent and ``model: inherit`` both end up
running the parent's model, but they are not the same finding: ``inherit`` is
a decision someone recorded, absence is a field nobody filled in. They are
counted separately and only absence is treated as a lint failure.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOTS = (Path(".claude/agents"), Path.home() / ".claude/agents")

#: ``model:`` values that mean "run whatever the parent is running".
INHERIT_VALUES = frozenset({"inherit", "parent"})


@dataclass(frozen=True)
class AgentDef:
    """One agent definition file, reduced to the two fields that matter."""

    path: Path
    name: str
    model: str | None
    #: True when the file has no YAML frontmatter block at all.
    malformed: bool = False

    @property
    def state(self) -> str:
        if self.malformed:
            return "malformed"
        if self.model is None:
            return "unpinned"
        if self.model.lower() in INHERIT_VALUES:
            return "inherit"
        return "pinned"


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """Top-level scalars from a leading ``---`` block, or None if absent.

    Deliberately not a YAML parser. Agent frontmatter is flat in practice and
    this only ever reads ``name`` and ``model``, so a hand parse keeps the
    module dependency-free. Values are taken verbatim after the first colon,
    stripped of surrounding quotes; nested blocks and list items are skipped
    rather than guessed at.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        if not line.strip() or line.startswith((" ", "\t", "#", "-")):
            continue  # nested value, comment or list item — not a top-level scalar
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            continue
        fields[key.strip()] = value.strip().strip("'\"")
    return None  # opened a block that never closed


def read_agent(path: Path) -> AgentDef:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return AgentDef(path=path, name=path.stem, model=None, malformed=True)
    fields = parse_frontmatter(text)
    if fields is None:
        return AgentDef(path=path, name=path.stem, model=None, malformed=True)
    model = fields.get("model") or None  # empty string is as unpinned as absent
    return AgentDef(path=path, name=fields.get("name") or path.stem, model=model)


def scan(roots: list[Path] | tuple[Path, ...] = DEFAULT_ROOTS) -> list[AgentDef]:
    """Every ``*.md`` under each existing root, recursively, deduplicated.

    Roots are scanned in the order given and the same resolved path is never
    reported twice, so passing overlapping roots (a project dir inside the
    home dir, say) is harmless.
    """
    found: list[AgentDef] = []
    seen: set[Path] = set()
    for root in roots:
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(read_agent(path))
    return found


def summarize(defs: list[AgentDef]) -> dict[str, int]:
    counts = {"pinned": 0, "unpinned": 0, "inherit": 0, "malformed": 0}
    for d in defs:
        counts[d.state] += 1
    return counts


def format_report(defs: list[AgentDef], roots: list[Path] | tuple[Path, ...]) -> str:
    if not defs:
        where = ", ".join(str(Path(r).expanduser()) for r in roots)
        return f"No agent definitions found under: {where}"

    counts = summarize(defs)
    out: list[str] = []
    width = max(len(d.name) for d in defs)

    unpinned = [d for d in defs if d.state == "unpinned"]
    if unpinned:
        out.append(f"Unpinned — these run whatever the main thread runs ({len(unpinned)}):")
        out += [f"  {d.name:<{width}}  {d.path}" for d in unpinned]

    inherit = [d for d in defs if d.state == "inherit"]
    if inherit:
        out.append("")
        out.append(f"Explicit inherit — same behaviour, but chosen ({len(inherit)}):")
        out += [f"  {d.name:<{width}}  {d.path}" for d in inherit]

    malformed = [d for d in defs if d.state == "malformed"]
    if malformed:
        out.append("")
        out.append(f"No frontmatter — not read as agent definitions ({len(malformed)}):")
        out += [f"  {d.name:<{width}}  {d.path}" for d in malformed]

    pinned = [d for d in defs if d.state == "pinned"]
    if pinned:
        out.append("")
        out.append(f"Pinned ({len(pinned)}):")
        out += [f"  {d.name:<{width}}  {d.model}" for d in pinned]

    out.append("")
    out.append(
        f"{len(defs)} definitions: {counts['pinned']} pinned, "
        f"{counts['unpinned']} unpinned, {counts['inherit']} explicit inherit, "
        f"{counts['malformed']} without frontmatter."
    )
    if unpinned:
        out.append(
            "Unpinned is not a bug. It is a cost you have not decided about: "
            "each of those runs on the main thread's model, whatever that is "
            "on the day. What they actually ran, and what it cost, needs a "
            "trace store — see traceguard.routing_audit."
        )
    else:
        out.append("Every definition pins a model. Nothing here inherits by accident.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent_lint",
        description=(
            "Report Claude Code agent definitions with no model: field. "
            "Reads frontmatter only; no transcripts, no network."
        ),
    )
    parser.add_argument(
        "roots",
        nargs="*",
        type=Path,
        help="directories to scan (default: ./.claude/agents and ~/.claude/agents)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also fail when a .md file has no frontmatter",
    )
    args = parser.parse_args(argv)

    roots = args.roots or list(DEFAULT_ROOTS)
    defs = scan(roots)

    if args.json:
        print(
            json.dumps(
                {
                    "roots": [str(Path(r).expanduser()) for r in roots],
                    "summary": summarize(defs),
                    "agents": [
                        {
                            "name": d.name,
                            "path": str(d.path),
                            "model": d.model,
                            "state": d.state,
                        }
                        for d in defs
                    ],
                },
                indent=2,
            )
        )
    else:
        print(format_report(defs, roots))

    counts = summarize(defs)
    failed = counts["unpinned"] or (args.strict and counts["malformed"])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
