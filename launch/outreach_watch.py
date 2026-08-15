#!/usr/bin/env python3
"""Local outreach watcher — replaces the cloud routine that cannot reach these hosts.

Runs on this Mac, where `gh` is authenticated (so any public repo is readable)
and dev.to / Reddit are reachable. Compares every target against a state file
and reports only what moved.

    ./outreach_watch.py            # check, update state, report changes
    ./outreach_watch.py --seed     # write current state without reporting
    ./outreach_watch.py --quiet    # no desktop notification (still logs)

Changes authored by the user themselves are recorded but never alerted on.
Unreachable targets are reported as "unknown", never silently as "no change" —
a watcher that cannot see is not a watcher that sees nothing.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ME = "lizhuojunx86"
HERE = Path(__file__).resolve().parent
STATE = HERE / "outreach-state.json"
LOG = HERE / "outreach-watch.log"
ALERT = HERE / "outreach-ALERT.md"

DEVTO = {  # article id -> label, for the articles worth a hand-written name
    # Numbering follows the audit series, not publication order: epsActual
    # predates the series and is watched separately. This dict no longer has
    # to be complete — `discover_devto` reads the live article list, so a new
    # post is watched the day it ships whether or not it is named here. An
    # earlier version relied on this dict alone and silently stopped counting
    # at four articles, missing the most-commented one in the series.
    "4073932": "dev.to #1 (missing model line)",
    "4227628": "dev.to #2 (splitrail audit)",
    "4313223": "dev.to #3 (cct dedup)",
    "4346772": "dev.to #4 (routing deviations)",
    "4351188": "dev.to #5 (measured with his own code)",
    "3931693": "dev.to (epsActual, pre-series)",
}

GH_THREADS = [  # (repo, number, label)
    ("Maciek-roboblog/Claude-Code-Usage-Monitor", 237, "CCUM PR #237 (dedup fix)"),
    ("Maciek-roboblog/Claude-Code-Usage-Monitor", 226, "CCUM #226 (rewrite drift)"),
    ("Maciek-roboblog/Claude-Code-Usage-Monitor", 158, "CCUM #158 (resumed session)"),
    ("junhoyeo/tokscale", 1011, "tokscale #1011 (input estimate)"),
    ("junhoyeo/tokscale", 994, "tokscale #994 (drift, fixed)"),
    ("Piebald-AI/splitrail", 220, "splitrail #220 (snapshot sum)"),
    ("Piebald-AI/splitrail", 222, "splitrail PR #222 (merged)"),
    ("sculptdotfun/viberank", 83, "viberank #83 (drift spec)"),
    ("davila7/claude-code-templates", 754, "cct PR #754 (dedup fix)"),
]

REDDIT = [  # (url, label)
    ("https://old.reddit.com/r/Go_Stock/comments/1utbpge/.rss", "reddit r/Go_Stock post"),
    ("https://old.reddit.com/r/Go_Stock/comments/1uo6zaj/comment/ovppv0k/.rss",
     "reddit r/ClaudeAI megathread comment"),
]

RELEASE_REPOS = ["junhoyeo/tokscale", "Piebald-AI/splitrail"]

UNKNOWN = "__unreachable__"


def run(cmd: list[str], timeout: int = 45) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return out.stdout if out.returncode == 0 else None


def curl(url: str) -> str | None:
    return run(["curl", "-sS", "--max-time", "30", "-A", "outreach-watch", url])


def gh(path: str) -> object | None:
    """GitHub read: `gh` when its credentials are reachable, else unauthenticated.

    Under launchd the keychain-backed `gh` token is not available, so every
    call would fail. Everything watched here is public, and one run makes far
    fewer than the 60 requests/hour anonymous budget, so falling back keeps
    the scheduled run working instead of reporting a wall of "unreachable".
    """
    raw = run(["gh", "api", "-H", "Accept: application/vnd.github+json", path])
    if raw is None:
        raw = run(["curl", "-sS", "--max-time", "30", "-A", "outreach-watch",
                   "-H", "Accept: application/vnd.github+json",
                   f"https://api.github.com/{path.lstrip('/')}"])
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def discover_devto() -> dict[str, str]:
    """Every published article, not just the hand-listed ones.

    The watcher exists to say whether anyone is responding; a target list that
    has to be edited by hand under-reports exactly when it matters most — right
    after a new post ships, which is when replies actually arrive. Falls back to
    the curated dict if the listing is unreachable, so a dev.to outage narrows
    coverage rather than emptying it.
    """
    raw = curl(f"https://dev.to/api/articles?username={ME}&per_page=100")
    if raw is None:
        return dict(DEVTO)
    try:
        arts = json.loads(raw)
    except json.JSONDecodeError:
        return dict(DEVTO)
    if not isinstance(arts, list) or not arts:
        return dict(DEVTO)

    found = dict(DEVTO)
    for art in arts:
        aid = str(art.get("id") or "")
        if not aid or aid in found:
            continue
        title = (art.get("title") or "untitled")[:60]
        found[aid] = f"dev.to (new): {title}"
    return found


def probe_devto(article_id: str) -> dict:
    comments = curl(f"https://dev.to/api/comments?a_id={article_id}")
    article = curl(f"https://dev.to/api/articles/{article_id}")
    if comments is None or article is None:
        return {"status": UNKNOWN}
    try:
        cs = json.loads(comments)
        art = json.loads(article)
    except json.JSONDecodeError:
        return {"status": UNKNOWN}

    def flatten(nodes):
        for n in nodes:
            yield n
            yield from flatten(n.get("children") or [])

    all_comments = list(flatten(cs))
    latest = max(all_comments, key=lambda c: c.get("created_at") or "", default=None)
    return {
        "status": "ok",
        "comments": len(all_comments),
        # Total comments conflate their words with my replies, and my own
        # replies move the count too. This is the number that means "someone
        # out there responded", and it is what gets alerted on.
        "external_comments": sum(
            1 for c in all_comments if (c.get("user") or {}).get("username") != ME
        ),
        "reactions": art.get("public_reactions_count", 0),
        "last_author": (latest or {}).get("user", {}).get("username"),
        "last_at": (latest or {}).get("created_at"),
    }


def probe_gh_thread(repo: str, number: int) -> dict:
    issue = gh(f"repos/{repo}/issues/{number}")
    comments = gh(f"repos/{repo}/issues/{number}/comments?per_page=100")
    if not isinstance(issue, dict) or not isinstance(comments, list):
        return {"status": UNKNOWN}
    human = [c for c in comments if not (c.get("user", {}).get("login", "")).endswith("[bot]")]
    last = human[-1] if human else None
    state = {
        "status": "ok",
        "state": issue.get("state"),
        "comments": len(human),
        "last_author": (last or {}).get("user", {}).get("login"),
        "last_at": (last or {}).get("created_at"),
        # Who opened it and when: a thread I opened that never drew a reply is
        # just as stale as one where I spoke last, and has no comment to date.
        "opened_by": (issue.get("user") or {}).get("login"),
        "opened_at": issue.get("created_at"),
    }
    if "pull_request" in issue:
        pr = gh(f"repos/{repo}/pulls/{number}")
        if isinstance(pr, dict):
            state["merged"] = bool(pr.get("merged"))
        reviews = gh(f"repos/{repo}/pulls/{number}/reviews")
        if isinstance(reviews, list):
            state["reviews"] = len(
                [r for r in reviews if not r.get("user", {}).get("login", "").endswith("[bot]")]
            )
    return state


def probe_reddit(url: str) -> dict:
    """Reddit rate-limits hard (429 on back-to-back requests), so pace and retry."""
    for attempt in range(3):
        if attempt:
            time.sleep(15 * attempt)
        body = curl(url)
        if body and ("<entry" in body or "<feed" in body):
            return {"status": "ok", "entries": body.count("<entry")}
    return {"status": UNKNOWN}


def probe_release(repo: str) -> dict:
    rel = gh(f"repos/{repo}/releases?per_page=1")
    if not isinstance(rel, list) or not rel:
        return {"status": UNKNOWN}
    return {"status": "ok", "tag": rel[0].get("tag_name"), "at": rel[0].get("published_at")}


def probe_own_issues() -> dict:
    items = gh(f"repos/{ME}/traceguard/issues?state=open&per_page=50")
    if not isinstance(items, list):
        return {"status": UNKNOWN}
    return {"status": "ok", "open": len([i for i in items if "pull_request" not in i])}


def probe_mentions() -> dict:
    known = {f"{repo}#{num}" for repo, num, _ in GH_THREADS}
    res = gh(f"search/issues?q=%22{ME}%22+in:comments&sort=updated&order=desc&per_page=15")
    if not isinstance(res, dict) or "items" not in res:
        return {"status": UNKNOWN}
    cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    fresh = []
    for it in res["items"]:
        try:
            updated = datetime.fromisoformat(it["updated_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if updated < cutoff:
            continue
        repo = it.get("repository_url", "").split("/repos/")[-1]
        key = f"{repo}#{it.get('number')}"
        if key in known or repo.startswith(f"{ME}/"):
            continue
        fresh.append({"key": key, "title": it.get("title", "")[:80], "url": it.get("html_url")})
    return {"status": "ok", "items": fresh}


def collect() -> dict:
    snap: dict = {"at": datetime.now().astimezone().isoformat(timespec="seconds")}
    for aid, label in discover_devto().items():
        snap[f"devto:{aid}"] = {"label": label, **probe_devto(aid)}
    for repo, num, label in GH_THREADS:
        snap[f"gh:{repo}#{num}"] = {"label": label, **probe_gh_thread(repo, num)}
    for i, (url, label) in enumerate(REDDIT):
        if i:
            time.sleep(20)  # pace Reddit; back-to-back requests get 429
        snap[f"reddit:{label}"] = {"label": label, **probe_reddit(url)}
    for repo in RELEASE_REPOS:
        snap[f"release:{repo}"] = {"label": f"{repo} latest release", **probe_release(repo)}
    snap["own:issues"] = {"label": "traceguard open issues", **probe_own_issues()}
    snap["mentions"] = {"label": "GitHub mentions (48h, outside known threads)", **probe_mentions()}
    return snap


WATCHED_FIELDS = ("comments", "external_comments", "reactions", "state", "merged",
                  "reviews", "entries", "tag", "open")

# A thread where I spoke last (or opened it and nobody replied) and that has
# been quiet this long is my move, not theirs.
STALE_DAYS = 7
# Don't repeat the same nudge daily — a permanent banner stops being read.
RENUDGE_DAYS = 7


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def stale_nudges(prev: dict, cur: dict) -> list[tuple[str, str]]:
    """Threads waiting on ME, as (state-key, message).

    This is the other half of watching: `diff` reports what someone else did,
    which is useless for a PR that is simply sitting there. Nothing in the
    upstream state ever changes to say "your move" — that has to be derived.
    """
    now = datetime.now(timezone.utc)
    out: list[tuple[str, str]] = []
    for key, entry in cur.items():
        if not key.startswith("gh:") or not isinstance(entry, dict):
            continue
        if entry.get("status") != "ok" or entry.get("state") != "open" or entry.get("merged"):
            continue

        # The last time the ball was in their court.
        if entry.get("last_author") == ME:
            since, what = _parse_iso(entry.get("last_at")), "my last comment"
        elif not entry.get("comments") and entry.get("opened_by") == ME:
            since, what = _parse_iso(entry.get("opened_at")), "opened, still no reply"
        else:
            continue
        if since is None:
            continue

        days = (now - since).days
        if days < STALE_DAYS:
            continue

        last_nudge = _parse_iso((prev.get(key) or {}).get("nudged_at"))
        if last_nudge and (now - last_nudge).days < RENUDGE_DAYS:
            continue

        out.append((key, f"{entry.get('label', key)}: quiet {days}d ({what}) — your move"))
    return out


def diff(prev: dict, cur: dict) -> tuple[list[str], list[str]]:
    """Return (alerts, unreachable-labels). Own-authored changes are not alerts."""
    alerts, unreachable = [], []
    for key, now in cur.items():
        if key == "at" or not isinstance(now, dict):
            continue
        label = now.get("label", key)
        if now.get("status") == UNKNOWN:
            unreachable.append(label)
            continue
        before = prev.get(key)
        if not isinstance(before, dict) or before.get("status") != "ok":
            # Nothing comparable; state simply updates. Except for an article
            # discovered mid-flight that already carries outside replies —
            # staying quiet there repeats the miss this watcher just had.
            if key.startswith("devto:") and now.get("external_comments"):
                alerts.append(
                    f"{label}: newly watched, already has "
                    f"{now['external_comments']} outside comment(s)"
                    f" (latest by {now.get('last_author')} at {now.get('last_at')})"
                )
            continue

        if key == "mentions":
            seen = {i["key"] for i in before.get("items", [])}
            for item in now.get("items", []):
                if item["key"] not in seen:
                    alerts.append(f"{label}: {item['key']} — {item['title']}\n    {item['url']}")
            continue

        moved = [f for f in WATCHED_FIELDS
                 if f in now and f in before and now[f] != before[f]]
        if not moved:
            continue
        by_me = now.get("last_author") == ME and moved == ["comments"]
        detail = ", ".join(f"{f}: {before[f]} -> {now[f]}" for f in moved)
        line = f"{label}: {detail}"
        if now.get("last_author"):
            line += f" (latest by {now['last_author']} at {now.get('last_at')})"
        if by_me:
            line = "[own post, not an alert] " + line
        elif "external_comments" in moved:
            # The only signal in this whole file that means a stranger engaged.
            alerts.append("someone replied — " + line)
        else:
            alerts.append(line)
    return alerts, unreachable


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true", help="record current state, report nothing")
    ap.add_argument("--quiet", action="store_true", help="skip the desktop notification")
    args = ap.parse_args()

    cur = collect()
    prev = {}
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = {}

    alerts, unreachable = ([], []) if args.seed else diff(prev, cur)
    nudges = [] if args.seed else stale_nudges(prev, cur)

    # Never let an unreachable probe overwrite a good reading with nothing.
    merged = dict(prev)
    for key, val in cur.items():
        if isinstance(val, dict) and val.get("status") == UNKNOWN and key in prev:
            continue
        merged[key] = val
    # Carry nudge timestamps forward, stamping the ones fired this run.
    fired = {key for key, _ in nudges}
    for key, val in merged.items():
        if not key.startswith("gh:") or not isinstance(val, dict):
            continue
        if key in fired:
            val["nudged_at"] = cur["at"]
        elif (prev.get(key) or {}).get("nudged_at"):
            val["nudged_at"] = prev[key]["nudged_at"]
    merged["at"] = cur["at"]
    STATE.write_text(json.dumps(merged, indent=1, ensure_ascii=False), encoding="utf-8")

    stamp = cur["at"]
    if args.seed:
        summary = f"seeded {len([k for k in cur if k != 'at'])} targets"
    elif alerts and nudges:
        summary = f"{len(alerts)} new, {len(nudges)} waiting on me"
    elif alerts:
        summary = f"{len(alerts)} new"
    elif nudges:
        summary = f"{len(nudges)} waiting on me"
    elif unreachable:
        summary = f"no change ({len(unreachable)} unreachable)"
    else:
        summary = "no change"

    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp} {summary}\n")
        for a in alerts:
            fh.write(f"    ALERT {a}\n")
        for _, n in nudges:
            fh.write(f"    NUDGE {n}\n")
        for u in unreachable:
            fh.write(f"    UNREACHABLE {u}\n")

    print(f"{stamp} — {summary}")
    for a in alerts:
        print(f"  ALERT  {a}")
    for _, n in nudges:
        print(f"  NUDGE  {n}")
    for u in unreachable:
        print(f"  UNKNOWN  {u} (could not read; state left untouched)")

    if alerts or nudges:
        sections = []
        if alerts:
            sections.append("## They moved\n\n" + "\n".join(f"- {a}" for a in alerts))
        if nudges:
            sections.append("## Waiting on me\n\n" + "\n".join(f"- {n}" for _, n in nudges))
        ALERT.write_text(
            f"# Outreach alert — {stamp}\n\n" + "\n\n".join(sections) + "\n",
            encoding="utf-8",
        )
        if not args.quiet:
            first = alerts[0] if alerts else nudges[0][1]
            body = first.split("\n")[0][:120]
            subprocess.run(
                ["osascript", "-e",
                 f'display notification {json.dumps(body)} with title "traceguard outreach" '
                 f'subtitle {json.dumps(summary)}'],
                capture_output=True,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
