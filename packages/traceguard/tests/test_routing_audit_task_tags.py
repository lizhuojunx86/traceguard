"""Tests for routing_audit task_type tagging (units, heuristic, CSV, pivot).

Synthetic fixtures only — no real session data. Reuses the assistant-line
builder from test_routing_audit_ingest.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from test_routing_audit_ingest import (
    MODEL_PRICED_A,
    MODEL_PRICED_B,
    USAGE_CACHED,
    USAGE_SIMPLE,
    _assistant_line,
)
from traceguard.routing_audit.ingest_claude_code import ingest
from traceguard.routing_audit.models import RoutingAuditTaskTag
from traceguard.routing_audit.task_tags import (
    classify_prompt,
    export_csv,
    format_pivot,
    import_csv,
    iter_session_units,
    redact_summary,
    tag_heuristic,
)
from traceguard.store.models import make_engine

SESS_TAG = "dddd4444-0000-0000-0000-000000000004"
SESS_UNTAGGED = "eeee5555-0000-0000-0000-000000000005"
CWD = "/Users/test/Desktop/APP/huadian"

T_H1 = "2026-06-05T10:00:00.000Z"
T_A1 = "2026-06-05T10:01:00.000Z"
T_H2 = "2026-06-05T10:05:00.000Z"
T_A2 = "2026-06-05T10:06:00.000Z"
T_H3 = "2026-06-05T12:00:00.000Z"  # 115 min after T_H2 → new unit at 60min gap
T_A3 = "2026-06-05T12:01:00.000Z"

PROMPT_IMPLEMENT = "帮我实现一个新的 ingest 模块，加个 CLI"
PROMPT_CONTINUE = "继续"
PROMPT_DEBUG = "这里报错了 sk-abcdefgh12345678ZZ 帮我 fix 修复一下"


def _user_prompt_line(session_id: str, ts: str, text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": f"uu-{ts}",
            "sessionId": session_id,
            "timestamp": ts,
            "cwd": CWD,
            "gitBranch": "feat/tagging",
            "message": {"role": "user", "content": text},
        }
    )


def _tool_result_line(session_id: str, ts: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": f"tr-{ts}",
            "sessionId": session_id,
            "timestamp": ts,
            "cwd": CWD,
            "toolUseResult": {"status": "ok"},
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "tool output"}],
            },
        }
    )


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    proj = root / "-Users-test-Desktop-APP-huadian"
    proj.mkdir(parents=True)
    lines = [
        _user_prompt_line(SESS_TAG, T_H1, PROMPT_IMPLEMENT),
        _assistant_line(
            session_id=SESS_TAG, message_id="msg_t1", uuid="ut-1", ts=T_A1,
            cwd=CWD, model=MODEL_PRICED_A, usage=USAGE_CACHED,
        ),
        _tool_result_line(SESS_TAG, T_A1),
        _user_prompt_line(SESS_TAG, T_H2, PROMPT_CONTINUE),
        _assistant_line(
            session_id=SESS_TAG, message_id="msg_t2", uuid="ut-2", ts=T_A2,
            cwd=CWD, model=MODEL_PRICED_A, usage=USAGE_SIMPLE,
        ),
        _user_prompt_line(SESS_TAG, T_H3, PROMPT_DEBUG),
        _assistant_line(
            session_id=SESS_TAG, message_id="msg_t3", uuid="ut-3", ts=T_A3,
            cwd=CWD, model=MODEL_PRICED_B, usage=USAGE_SIMPLE,
        ),
    ]
    (proj / f"{SESS_TAG}.jsonl").write_text("\n".join(lines), encoding="utf-8")
    # Session with no human prompts → no unit → its trace stays untagged.
    (proj / f"{SESS_UNTAGGED}.jsonl").write_text(
        _assistant_line(
            session_id=SESS_UNTAGGED, message_id="msg_t9", uuid="ut-9",
            ts="2026-06-06T09:00:00.000Z", cwd=CWD, model=MODEL_PRICED_A,
            usage=USAGE_SIMPLE,
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'task_tags_test.db'}"


def test_classify_prompt() -> None:
    assert classify_prompt(PROMPT_IMPLEMENT)[0] == "coding-implement"
    assert classify_prompt("这里报错了，帮我修复")[0] == "coding-debug"
    assert classify_prompt("你觉得该不该用 sqlite，给点建议")[0] == "decision-advisor"
    assert classify_prompt("部署到生产并 push")[0] == "ops-routine"
    assert classify_prompt("调研一下竞品，对比分析")[0] == "research-explore"
    assert classify_prompt("润色这篇文章的第三章节")[0] == "writing-doc"
    assert classify_prompt("随便聊聊") == ("other", 0)
    # ASCII terms match on word boundaries: "prefix" must not hit "fix".
    assert classify_prompt("prefix the name")[0] == "other"
    # Branch prefix is a weak signal when the text says nothing.
    assert classify_prompt("看这个", git_branch="fix/broken")[0] == "coding-debug"


def test_redact_summary() -> None:
    s = redact_summary("  修复\n\n这个  " + "sk-abcdefgh12345678ZZ" + " 的问题 " + "x" * 200)
    assert "sk-abcdefgh" not in s
    assert "[REDACTED]" in s
    assert "\n" not in s
    assert len(s) <= 100


def test_iter_session_units(source_root: Path) -> None:
    units = list(iter_session_units(source_root))
    assert [u.unit_id for u in units] == [f"{SESS_TAG}#s01", f"{SESS_TAG}#s02"]
    u1, u2 = units
    assert u1.n_turns == 2 and u2.n_turns == 1
    assert u1.ts_start == datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)
    assert u1.ts_end == u2.ts_start == datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
    assert u2.ts_end is None
    assert u1.project == "huadian"
    assert u1.first_prompt == PROMPT_IMPLEMENT


def test_heuristic_write_export_import_roundtrip(source_root: Path, db_url: str, tmp_path: Path) -> None:
    dry = tag_heuristic(source_root, db_url, write=False)
    assert dry.units == 2
    assert dry.by_type == {"coding-implement": 1, "coding-debug": 1}
    assert not (tmp_path / "task_tags_test.db").exists()  # dry-run wrote nothing

    stats = tag_heuristic(source_root, db_url, write=True)
    assert stats.inserted == 2

    csv_path = tmp_path / "units.csv"
    assert export_csv(csv_path, db_url, source_root) == 2
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    by_id = {r["unit_id"]: r for r in rows}
    assert by_id[f"{SESS_TAG}#s01"]["task_type"] == "coding-implement"
    assert by_id[f"{SESS_TAG}#s01"]["source"] == "heuristic"
    # Summary is redacted prompt text; secrets masked.
    assert "实现" in by_id[f"{SESS_TAG}#s01"]["summary"]
    assert "sk-abcdefgh" not in by_id[f"{SESS_TAG}#s02"]["summary"]

    # Manual correction: import only the edited row.
    edited = tmp_path / "edited.csv"
    with edited.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["unit_id", "task_type"])
        writer.writeheader()
        writer.writerow({"unit_id": f"{SESS_TAG}#s01", "task_type": "writing-doc"})
        writer.writerow({"unit_id": "no-such-unit#s01", "task_type": "other"})
        writer.writerow({"unit_id": f"{SESS_TAG}#s02", "task_type": "not-a-type"})
    imp = import_csv(edited, db_url)
    assert imp.updated == 1
    assert imp.by_type["(unit not in db)"] == 1
    assert imp.by_type["(bad task_type)"] == 1

    engine = make_engine(db_url)
    with Session(engine) as sess:
        tag = sess.get(RoutingAuditTaskTag, f"{SESS_TAG}#s01")
        assert (tag.task_type, tag.source) == ("writing-doc", "manual")

    # Heuristic re-run never overwrites manual rows.
    rerun = tag_heuristic(source_root, db_url, write=True)
    assert rerun.manual_kept == 1 and rerun.updated == 1
    with Session(engine) as sess:
        tag = sess.get(RoutingAuditTaskTag, f"{SESS_TAG}#s01")
        assert (tag.task_type, tag.source) == ("writing-doc", "manual")


def test_report_pivot_joins_traces_by_window(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    tag_heuristic(source_root, db_url, write=True)
    report = format_pivot(db_url)

    lines = {line.split()[0]: line for line in report.splitlines() if line.strip()}
    # Unit 1 window owns msg_t1 + msg_t2 (opus); unit 2 owns msg_t3 (haiku);
    # the promptless session's trace is untagged.
    assert "coding-implement" in lines
    assert lines["coding-implement"].split()[1] == MODEL_PRICED_A
    assert lines["coding-implement"].split()[2] == "2"
    assert lines["coding-debug"].split()[1] == MODEL_PRICED_B
    assert lines["coding-debug"].split()[2] == "1"
    assert "(untagged)" in lines

    # cache-hit share for coding-implement =
    # cache_read / (input + cache_read + cache_creation) over its traces.
    cached = USAGE_CACHED
    simple = USAGE_SIMPLE
    read = cached["cache_read_input_tokens"]
    denom = (
        cached["input_tokens"] + cached["cache_read_input_tokens"]
        + cached["cache_creation_input_tokens"] + simple["input_tokens"]
    )
    assert f"{read / denom:.1%}" in lines["coding-implement"]
