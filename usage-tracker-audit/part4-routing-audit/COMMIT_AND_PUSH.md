# traceguard — commit + push，需要你在本机执行

**为什么不是我跑的：** 沙箱对 traceguard 的挂载是 create-but-not-delete。git 需要
建立并删除 `.git/index.lock`，删不掉 → 任何 `git add` / `commit` 都会中途失败。
我实测确认过（`rmdir` 也被拒）。

**我已经做完的：** `.gitignore` 改好了——`* 2.*` / `* 3.*` 覆盖了全部 13 个
iCloud 冲突副本（原来只列到 `* 2.py|md|yaml|sh|plist`，所以 " 3." 和
`.json`/`.csv` 那些一直漏在外面），另加 `.claude/settings.local.json`、
`_prefilled_issue_url.txt`。**冲突副本我一个都没删**，只是让 git 不再看见它们。

## ⚠ 先删这个锁，否则上面所有命令都会失败

第一次尝试时四条 `git add` / `git commit` 全部报
`fatal: Unable to create '.../.git/index.lock': File exists`，只有 `git push`
成功——而它推的是之前那个早就 commit 好的 `f622857`。**五轮工程一行都没进去。**

原因是我造成的：我在沙箱里跑过 `git status` 做仓库盘点，git 建了
`.git/index.lock`，然后因为挂载禁止删除而清不掉，从此卡住整个仓库。

```bash
cd ~/Desktop/APP/traceguard
ls -la .git/index.lock        # 时间戳 15:25、owner 不是你，就是这个
rm -f .git/index.lock
git status --porcelain | head # 能正常输出 = 解锁成功
```

删不掉就 `sudo rm -f .git/index.lock`。

**教训（已写进本文件而不是只留在对话里）：不要在这个挂载上跑任何 git 命令，
包括只读的 `git status`——它会写 index.lock，而这个挂载建得了、删不掉。**
仓库盘点改用 `ls` / `cat .gitignore` / 直接读文件。

**另外两个探测垃圾**（已由你清除，留档）：

```bash
rm -f .git/.__wtest && rmdir .probe.* 2>/dev/null
```

---

## 执行

```bash
cd ~/Desktop/APP/traceguard

# 1. 确认 .gitignore 生效，untracked 应该只剩下面 5 个该进仓库的文件
git status --porcelain | grep '^??'
#   ?? routing_decisions_rebuild_log.jsonl
#   ?? traces_routing_audit.2026-07-04-audit.README.md
#   ?? usage-tracker-audit/part4-routing-audit/CC_PROMPT_5.md
#   ?? usage-tracker-audit/part4-routing-audit/PUBLISH_CHECKLIST.md
#   ?? usage-tracker-audit/viberank-83/ISSUE_per_agent_slice.md
#   （多出任何一行就停下来看，别直接 add -A）

# 2. 确认没有 .db 会被提交
git status --porcelain | grep -i '\.db$' && echo "STOP" || echo "no db files — ok"

# 3. 工程改动
git add .gitignore \
        packages/traceguard/src/traceguard/routing_audit/pricing.py \
        packages/traceguard/src/traceguard/routing_audit/ingest_claude_code.py \
        packages/traceguard/src/traceguard/routing_audit/routing_decisions.py \
        packages/traceguard/tests/test_routing_audit_ingest.py \
        packages/traceguard/tests/test_routing_audit_decisions.py \
        TRACEGUARD_SPEC.md

git commit -F- <<'MSG'
fix(routing_audit): price opus-5, deliver unpriced-model warnings, unfreeze manual rows

Four defects, all found while fact-checking the part 4 article, all of the
same class: a check that existed and reported nothing.

- pricing: claude-opus-5 had no PRICES entry, so 5,992 traces (10.3% of the
  store) carried cost_usd = NULL for two weeks. Rate and release date verified
  against anthropic.com/news/claude-opus-5. Repriced to $1,213.91.
- pricing: compute_cost_usd read the 5m/1h cache split only from the nested
  usage shape. Transcripts are nested, the store is flat, so any
  recompute-from-store billed 1h writes at the 5m rate. Now reads both.
  Blast radius measured: $88.75 over 3,918 rows, not the $1,393.49 first
  claimed — see the correction in pricing.py :: cache_creation_split.
- ingest: the missing_price counter incremented and was tested, but
  append_run_log serialised only stats.warnings, which it never entered. The
  alarm fired ten times into stdout while eighteen run-log entries recorded an
  empty warnings list. Warnings now go through a WARNING_KINDS registry.
  written_cost never appears without the count it excluded.
- routing_decisions: generate skipped source='manual' rows whole to protect
  reason/outcome, freezing their derived columns too. All 96 deviations were
  permanently exempt from the policy. Columns are now partitioned into
  human / derived / identity, with a test asserting the three sets cover the
  table exactly. Refreshing them produced 0 verdict flips and 0 human-column
  changes.

Also: task-tag coverage and staleness watchdogs, a derived_drift watchdog,
dry-run counters that no longer report structural zeros, and as_of coercion
(a numeric-looking string silently matched zero rows under SQLite's NUMERIC
affinity).

430 passed, 3 skipped.
MSG

# 4. 文章与记录材料
git add routing_decisions_rebuild_log.jsonl \
        traces_routing_audit.2026-07-04-audit.README.md \
        usage-tracker-audit/part4-routing-audit/ \
        usage-tracker-audit/viberank-83/ISSUE_per_agent_slice.md

git commit -m "docs(part4): published — findings, outline, draft, publish record

https://dev.to/lizhuojunx86/my-routing-policy-and-my-traces-disagreed-96-times-never-once-on-the-main-thread-ffp

FINDINGS.md keeps every claim that was wrong on the way, with the correction
next to it rather than in place of it. Two were mine."

# 5. 推
git push origin main
```

`git push` 会带上之前那个已提交未推的 `f622857`，一共三个 commit。

## 推之前值得看一眼的

`FINDINGS.md` 和 `PUBLISH_CHECKLIST.md` 会进公开仓库。两份都只含 traceguard
自己的数据和已发布文章的材料，**没有 HKEX 相关内容**——我核过。
`CC_PROMPT_*.md` 是中文工作指令，进不进公开仓库你定；它们记录的是真实的修复
经过，留着对读者有价值，但风格上和仓库其他文档不一致。不想要就从第 4 步的
`git add` 里去掉 `usage-tracker-audit/part4-routing-audit/`，改成只加
`FINDINGS.md` 和 `DEVTO_DRAFT_*.md`。
