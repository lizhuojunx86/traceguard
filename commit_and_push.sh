#!/usr/bin/env bash
# traceguard — 提交并推送本轮全部改动。
#
# 用法：  cd ~/Desktop/APP/traceguard && bash commit_and_push.sh
#
# 闸门全部内置。任何一道不过就中止并说明怎么办，不会留下半提交状态。
set -uo pipefail

cd "$(dirname "$0")" || exit 1
echo "仓库：$(pwd)"
echo

# ── 闸门 0：陈旧的 index.lock ────────────────────────────────────────────
if [ -e .git/index.lock ]; then
  echo "✗ 中止：.git/index.lock 存在。"
  echo
  ls -la .git/index.lock
  echo
  echo "  这是上一次 git 进程没清理掉的锁（沙箱挂载建得了、删不掉）。"
  echo "  修复：  rm -f .git/index.lock   然后重跑本脚本。"
  exit 1
fi

# ── 闸门 1：有东西要提交吗 ──────────────────────────────────────────────
if [ -z "$(git status --porcelain)" ]; then
  echo "✓ 工作区干净，没有要提交的东西。"
  echo "  仍未推送的 commit："
  git log --oneline @{u}..HEAD 2>/dev/null || echo "    （无上游信息）"
  exit 0
fi

echo "── 暂存前的状态 ─────────────────────────────────────────────"
git status --short
echo

git add -A

# ── 闸门 2：绝不提交数据 ────────────────────────────────────────────────
DANGER=$(git diff --cached --name-only | grep -E '\.db$|\.sqlite3?$|^data/|\.env$|/probe-.*\.md$|settings\.local' || true)
if [ -n "$DANGER" ]; then
  echo "✗ 中止：以下文件不应进入仓库，但已被暂存："
  echo "$DANGER" | sed 's/^/    /'
  echo
  echo "  已回滚暂存区，仓库未被改动。"
  echo "  检查 .gitignore，或手动 git reset 后再跑。"
  git reset >/dev/null
  exit 1
fi

# ── 闸门 3：体积 ───────────────────────────────────────────────────────
BIG=$(git diff --cached --name-only | while read -r f; do
        [ -f "$f" ] && [ "$(wc -c <"$f")" -gt 2000000 ] && echo "$f ($(du -h "$f" | cut -f1))"
      done)
if [ -n "$BIG" ]; then
  echo "✗ 中止：有超过 2 MB 的文件被暂存："
  echo "$BIG" | sed 's/^/    /'
  git reset >/dev/null
  exit 1
fi

N=$(git diff --cached --name-only | wc -l | tr -d ' ')
echo "── 将提交 $N 个文件 ───────────────────────────────────────────"
git diff --cached --stat | tail -25
echo

git commit -F- <<'MSG'
probe: user-authored model: fields are honoured; census of a 30k-star repo

Settles an open question with measurement rather than argument. Four issues on
anthropics/claude-code over seven months report that a subagent definition's
`model:` field is ignored (#43869 still open, #18346 and #31550 closed
not_planned), while the documentation says it is honoured.

Seven probes on Claude Code 2.1.226, main thread on claude-opus-5, each a
project-level definition carrying one `model:` value, read back from the
transcript:

  haiku                        -> claude-haiku-4-5-20251001   honoured
  claude-haiku-4-5-20251001    -> same                        honoured
  sonnet                       -> claude-sonnet-5             honoured (tier, not model)
  claude-sonnet-4-5            -> claude-sonnet-4-5-20250929  honoured
  claude-sonnet-4-5-20250929   -> same                        honoured
  inherit                      -> parent model                honoured, as documented
  Claude Sonnet 4.5            -> hard error, zero tokens     rejected
  Claude Sonnet 4              -> hard error, zero tokens     rejected
  Claude Sonnet 4.5 (copilot)  -> hard error, zero tokens     rejected
  claude-sonnet-4              -> hard error, zero tokens     retired generation

The correct statement about #43869 is "not reproducible on 2.1.226 along the two
paths I tested", not "the bug does not exist".

Census of davila7/claude-code-templates, all 423 agent definitions, every
distinct spelling tested rather than inferred: 356 omit the field and inherit,
48 pin a tier by alias, 3 pin an exact model, 1 inherits explicitly, 8 hard-error
on install, 7 have no frontmatter and were not tested. Filed as issue #788.

Three of the eight were spelled correctly and worked when written. That
generation was retired and they broke with no commit, no alert and no version
change. Pinning a model has a shelf life; pinning a tier does not.

Also here:
- SPEC appendix B3, four operating principles distilled from real defects, each
  with the instance that produced it, plus a fifth added after stating a
  contingent fact as a necessary one twice in one audit.
- read_probe_result.py, and two bugs fixed in it. Its first verdict logic
  guessed intent from the model that ran and called `inherit` "not honoured".
  Its first lookup path resolved only against cwd, so running it from its own
  directory reported NO DEFINITION FILE FOUND for every probe. A check that
  degrades honestly at the most natural invocation is not a check.
- Part 4 correction record: three edits applied to the published post after
  finding that the mechanism I gave for the compliant subagents was wrong.
MSG

RC=$?
if [ $RC -ne 0 ]; then
  echo "✗ commit 失败（退出码 $RC）。没有推送。"
  exit $RC
fi

echo
echo "── 推送前，本地领先上游的 commit ─────────────────────────────"
git log --oneline @{u}..HEAD 2>/dev/null || git log --oneline -3
echo

git push origin main
RC=$?
echo
if [ $RC -eq 0 ]; then
  echo "✓ 推送成功。"
  git log --oneline -1
else
  echo "✗ 推送失败（退出码 $RC）。commit 已在本地，重跑 git push origin main 即可。"
fi
exit $RC
