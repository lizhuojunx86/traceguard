"""Tests for the contract-external routing_audit Claude Code ingest.

All fixture data below is hand-built synthetic JSONL mirroring the observed
Claude Code session schema (see ingest_claude_code module docstring). No real
session data is used or committed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.routing_audit.ingest_claude_code import (
    collect_records,
    ingest,
    map_project,
    rollback_batch,
)
from traceguard.routing_audit.models import RoutingAuditIngestLog
from traceguard.routing_audit.pricing import KNOWN_RELEASED_AT, PRICES, compute_cost_usd
from traceguard.sdk.normalizer import input_hash
from traceguard.store.models import ModelRegistryEntry, Trace, make_engine

SECRET = "TOP_SECRET_FIXTURE_CONTENT_9f2c"
MODEL_PRICED_A = "claude-opus-4-8"
MODEL_PRICED_B = "claude-haiku-4-5-20251001"
MODEL_UNPRICED = "unpriced-test-model"

SESS_MAIN = "aaaa1111-0000-0000-0000-000000000001"
SESS_OTHER = "bbbb2222-0000-0000-0000-000000000002"

TS_ERR = "2026-05-30T07:00:00.000Z"
TS_OTHER = "2026-05-30T08:00:00.000Z"
# Subagent haiku message predates the main-transcript haiku message (TS_B) on
# purpose: a --no-subagents first run then freezes a too-late
# available_to_us_at, which the wider re-run must WARN about.
TS_SUB1 = "2026-05-31T09:00:00.000Z"
TS_A = "2026-06-01T10:00:00.000Z"
TS_B = "2026-06-01T11:00:00.000Z"
TS_SUB2 = "2026-06-02T10:00:00.000Z"

# Streaming first-line snapshot: tiny output_tokens, stop_reason still null.
USAGE_SNAPSHOT = {
    "input_tokens": 3,
    "output_tokens": 4,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
    "service_tier": "standard",
    "speed": "standard",
}
USAGE_SIMPLE = {
    "input_tokens": 1000,
    "output_tokens": 2000,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
    "service_tier": "standard",
    "speed": "standard",
}
USAGE_CACHED = {
    "input_tokens": 500,
    "output_tokens": 100,
    "cache_read_input_tokens": 10_000,
    "cache_creation_input_tokens": 3_000,
    "cache_creation": {
        "ephemeral_5m_input_tokens": 2_000,
        "ephemeral_1h_input_tokens": 1_000,
    },
    "service_tier": "standard",
    "speed": "standard",
}


def _assistant_line(
    *,
    session_id: str,
    message_id: str | None,
    uuid: str,
    ts: str,
    cwd: str,
    model: str,
    usage: dict | None,
    stop_reason: str = "end_turn",
    is_api_error: bool = False,
) -> str:
    rec = {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": None,
        "sessionId": session_id,
        "timestamp": ts,
        "cwd": cwd,
        "version": "2.1.198",
        "gitBranch": "main",
        "entrypoint": "claude-vscode",
        "userType": "external",
        "requestId": "req_fixture",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "stop_reason": stop_reason,
            "usage": usage,
            "content": [{"type": "text", "text": f"the answer is {SECRET}"}],
        },
    }
    if is_api_error:
        rec["isApiErrorMessage"] = True
        rec["apiErrorStatus"] = 529
    return json.dumps(rec)


def _user_line(session_id: str, ts: str, cwd: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": "user-uuid-1",
            "sessionId": session_id,
            "timestamp": ts,
            "cwd": cwd,
            "message": {"role": "user", "content": f"my secret prompt {SECRET}"},
        }
    )


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    huadian_cwd = "/Users/test/Desktop/APP/huadian"
    proj = root / "-Users-test-Desktop-APP-huadian"
    proj.mkdir(parents=True)

    main_lines = [
        _user_line(SESS_MAIN, TS_A, huadian_cwd),
        # Streaming duplication: one API message written as multiple lines
        # sharing message.id. The FIRST line is a partial snapshot (tiny
        # output_tokens, null stop_reason); the LAST line carries the final
        # usage/stop_reason. Must collapse to ONE trace with the LAST values.
        _assistant_line(
            session_id=SESS_MAIN, message_id="msg_001", uuid="u-1", ts=TS_A,
            cwd=huadian_cwd, model=MODEL_PRICED_A, usage=USAGE_SNAPSHOT,
            stop_reason=None,
        ),
        _assistant_line(
            session_id=SESS_MAIN, message_id="msg_001", uuid="u-2", ts=TS_A,
            cwd=huadian_cwd, model=MODEL_PRICED_A, usage=USAGE_SIMPLE,
        ),
        _assistant_line(
            session_id=SESS_MAIN, message_id="msg_002", uuid="u-3", ts=TS_B,
            cwd=huadian_cwd, model=MODEL_PRICED_B, usage=USAGE_SNAPSHOT,
            stop_reason=None,
        ),
        _assistant_line(
            session_id=SESS_MAIN, message_id="msg_002", uuid="u-4", ts=TS_B,
            cwd=huadian_cwd, model=MODEL_PRICED_B, usage=USAGE_CACHED,
            stop_reason="max_tokens",
        ),
        # API-error line: synthetic model, no message.id, no usage.
        _assistant_line(
            session_id=SESS_MAIN, message_id=None, uuid="err-uuid-1", ts=TS_ERR,
            cwd=huadian_cwd, model="<synthetic>", usage=None,
            stop_reason=None, is_api_error=True,
        ),
        '{"this is not valid json — an "assistant" typo line',
        json.dumps({"type": "attachment", "sessionId": SESS_MAIN}),
    ]
    (proj / f"{SESS_MAIN}.jsonl").write_text("\n".join(main_lines), encoding="utf-8")

    # Subagent transcript WITH meta (agentType Explore).
    sub_dir = proj / SESS_MAIN / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-abc123.jsonl").write_text(
        _assistant_line(
            session_id=SESS_MAIN, message_id="msg_101", uuid="u-101", ts=TS_SUB1,
            cwd=huadian_cwd, model=MODEL_PRICED_B, usage=USAGE_SIMPLE,
        ),
        encoding="utf-8",
    )
    (sub_dir / "agent-abc123.meta.json").write_text(
        json.dumps({"agentType": "Explore", "spawnDepth": 1}), encoding="utf-8"
    )
    # Nested workflow subagent WITHOUT meta → component "unknown".
    wf_dir = sub_dir / "workflows" / "wf_test"
    wf_dir.mkdir(parents=True)
    (wf_dir / "agent-def456.jsonl").write_text(
        _assistant_line(
            session_id=SESS_MAIN, message_id="msg_102", uuid="u-102", ts=TS_SUB2,
            cwd=huadian_cwd, model=MODEL_PRICED_A, usage=USAGE_SIMPLE,
        ),
        encoding="utf-8",
    )

    # Second project dir, non-canonical name + unpriced model.
    other = root / "-Users-test-other-proj"
    other.mkdir()
    (other / f"{SESS_OTHER}.jsonl").write_text(
        _assistant_line(
            session_id=SESS_OTHER, message_id="msg_201", uuid="u-201", ts=TS_OTHER,
            cwd="/Users/test/Other-Proj", model=MODEL_UNPRICED, usage=USAGE_SIMPLE,
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'routing_audit_test.db'}"


def test_map_project() -> None:
    assert map_project("/Users/test/Desktop/APP/huadian") == "huadian"
    assert map_project("/x/quant_alpha_v2") == "quant_alpha_v2"
    assert map_project("/x/quant-alpha-v2") == "quant_alpha_v2"
    assert map_project("/Users/test/Other-Proj") == "other_proj"
    assert map_project(None) == "unknown"
    assert map_project("") == "unknown"


def test_dry_run_parses_but_writes_nothing(source_root: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "never_created.db"
    stats = ingest(source_root, f"sqlite:///{db_path}", write=False)
    assert not db_path.exists()
    assert stats.records == 6
    assert stats.duplicate_lines == 2
    assert stats.malformed_lines == 1
    assert stats.error_records == 1
    assert stats.written == 0
    assert stats.files_main == 2
    assert stats.files_subagent == 2


def test_write_field_mapping(source_root: Path, db_url: str) -> None:
    stats = ingest(source_root, db_url, write=True)
    assert stats.written == 6
    assert stats.batch_id

    engine = make_engine(db_url)
    with Session(engine) as sess:
        rows = {r.output_parsed["message_id"] or "err": r for r in sess.scalars(select(Trace))}
    assert len(rows) == 6

    r = rows["msg_001"]
    assert r.project == "huadian"
    assert r.component == "main"
    assert r.operation == "llm_complete"
    assert r.model_id == MODEL_PRICED_A
    assert r.parse_status == "success"
    assert r.invoked_at == datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    # Last streamed line wins: final usage and stop_reason, not the
    # first-line snapshot (output_tokens=4, stop_reason=null).
    assert r.tokens_in == 1000
    assert r.tokens_out == 2000
    assert r.output_parsed["stop_reason"] == "end_turn"
    # Independent recompute for the no-cache case: (in*P_in + out*P_out)/1e6.
    p = PRICES[MODEL_PRICED_A]
    expected = (1000 * p.input_per_mtok + 2000 * p.output_per_mtok) / Decimal(1_000_000)
    assert r.cost_usd == expected.quantize(Decimal("0.000001"))
    assert r.error_class is None

    # Cached + truncated (max_tokens → partial); cost uses the 5m/1h split.
    r2 = rows["msg_002"]
    assert r2.parse_status == "partial"
    assert r2.tokens_in == 500 + 10_000 + 3_000
    p2 = PRICES[MODEL_PRICED_B]
    expected2 = (
        500 * p2.input_per_mtok
        + 10_000 * p2.input_per_mtok * p2.cache_read_mult
        + 2_000 * p2.input_per_mtok * p2.cache_write_5m_mult
        + 1_000 * p2.input_per_mtok * p2.cache_write_1h_mult
        + 100 * p2.output_per_mtok
    ) / Decimal(1_000_000)
    assert r2.cost_usd == expected2.quantize(Decimal("0.000001"))

    # API-error record: failed, no model_id (synthetic not registered), no cost.
    r_err = rows["err"]
    assert r_err.parse_status == "failed"
    assert r_err.model_id is None
    assert r_err.error_class == "api_error"
    assert r_err.cost_usd is None
    assert r_err.tokens_in is None and r_err.tokens_out is None
    assert r_err.output_parsed["raw_model"] == "<synthetic>"

    # Subagent component resolution.
    assert rows["msg_101"].component == "Explore"
    assert rows["msg_102"].component == "unknown"

    # Non-canonical project keeps its own slug; unpriced model → cost NULL.
    r_other = rows["msg_201"]
    assert r_other.project == "other_proj"
    assert r_other.model_id == MODEL_UNPRICED
    assert r_other.cost_usd is None
    assert stats.missing_price == 1


def test_input_hash_uses_sdk_function(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    engine = make_engine(db_url)
    with Session(engine) as sess:
        trace = sess.scalars(
            select(Trace).where(Trace.output_parsed["message_id"].as_string() == "msg_001")
        ).one()
    assert trace.input_hash == input_hash(
        {
            "source": "claude_code_session",
            "session_id": SESS_MAIN,
            "message_id": "msg_001",
        }
    )


def test_no_content_leakage(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    engine = make_engine(db_url)
    with Session(engine) as sess:
        for trace in sess.scalars(select(Trace)):
            dump = json.dumps(
                {
                    "input_summary": trace.input_summary,
                    "output_parsed": trace.output_parsed,
                    "error_message": trace.error_message,
                }
            )
            assert SECRET not in dump
            assert (trace.input_summary or "") == (trace.input_summary or "")[:200]
        for log in sess.scalars(select(RoutingAuditIngestLog)):
            assert SECRET not in json.dumps(
                {"file": log.source_file, "msg": log.source_message_id}
            )


def test_model_auto_registration(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    engine = make_engine(db_url)
    with Session(engine) as sess:
        entries = {e.model_id: e for e in sess.scalars(select(ModelRegistryEntry))}

    # Synthetic error model is NOT registered.
    assert set(entries) == {MODEL_PRICED_A, MODEL_PRICED_B, MODEL_UNPRICED}
    for entry in entries.values():
        assert entry.released_at <= entry.available_to_us_at
        assert entry.model_family == "anthropic"
        assert entry.capability_class == "general-llm"

    # available_to_us_at = first appearance in the data.
    assert entries[MODEL_PRICED_A].available_to_us_at == datetime(
        2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc
    )
    assert entries[MODEL_PRICED_B].available_to_us_at == datetime(
        2026, 5, 31, 9, 0, 0, tzinfo=timezone.utc
    )
    # Known release dates are used when present; unknown models fall back to
    # first-seen (see pricing.py docstring).
    assert entries[MODEL_PRICED_A].released_at == KNOWN_RELEASED_AT[MODEL_PRICED_A]
    assert entries[MODEL_PRICED_B].released_at == KNOWN_RELEASED_AT[MODEL_PRICED_B]
    assert entries[MODEL_UNPRICED].released_at == entries[MODEL_UNPRICED].available_to_us_at


def test_idempotent_rerun(source_root: Path, db_url: str) -> None:
    first = ingest(source_root, db_url, write=True)
    assert first.written == 6
    second = ingest(source_root, db_url, write=True)
    assert second.written == 0
    assert second.already_ingested == 6

    engine = make_engine(db_url)
    with Session(engine) as sess:
        assert len(list(sess.scalars(select(Trace)))) == 6
        assert len(list(sess.scalars(select(RoutingAuditIngestLog)))) == 6


def test_rollback_batch(source_root: Path, db_url: str) -> None:
    first = ingest(source_root, db_url, write=True)
    assert first.written == 6

    # A later session appears; only it lands in the second batch.
    proj = source_root / "-Users-test-Desktop-APP-huadian"
    (proj / "cccc3333-0000-0000-0000-000000000003.jsonl").write_text(
        _assistant_line(
            session_id="cccc3333-0000-0000-0000-000000000003",
            message_id="msg_301", uuid="u-301", ts="2026-06-03T09:00:00.000Z",
            cwd="/Users/test/Desktop/APP/huadian", model=MODEL_PRICED_A,
            usage=USAGE_SIMPLE,
        ),
        encoding="utf-8",
    )
    second = ingest(source_root, db_url, write=True)
    assert second.written == 1
    assert second.batch_id != first.batch_id

    n_traces, n_log = rollback_batch(second.batch_id, db_url)
    assert (n_traces, n_log) == (1, 1)

    engine = make_engine(db_url)
    with Session(engine) as sess:
        remaining = list(sess.scalars(select(Trace)))
        assert len(remaining) == 6  # first batch intact
        logs = list(sess.scalars(select(RoutingAuditIngestLog)))
        assert {log.batch_id for log in logs} == {first.batch_id}

    # Rolled-back records can be re-ingested afterwards.
    third = ingest(source_root, db_url, write=True)
    assert third.written == 1


def test_rollback_dry_run_previews_without_deleting(source_root: Path, db_url: str) -> None:
    stats = ingest(source_root, db_url, write=True)
    assert stats.batch_id is not None

    n_traces, n_log = rollback_batch(stats.batch_id, db_url, dry_run=True)
    assert (n_traces, n_log) == (6, 6)

    # CLI path: --rollback combined with --dry-run must not delete either.
    from traceguard.routing_audit.ingest import main as cli_main

    assert cli_main(["--rollback", stats.batch_id, "--db", db_url, "--dry-run"]) == 0

    engine = make_engine(db_url)
    with Session(engine) as sess:
        assert len(list(sess.scalars(select(Trace)))) == 6
        assert len(list(sess.scalars(select(RoutingAuditIngestLog)))) == 6


def test_registry_freeze_warning_on_wider_rerun(source_root: Path, db_url: str) -> None:
    # First run skips subagents → haiku registered with the LATER
    # main-transcript first-seen. model_registry is insert-only, so the wider
    # re-run (subagent haiku message at TS_SUB1 < TS_B) must warn that traces
    # now predate available_to_us_at (invariant-2 shape).
    first = ingest(source_root, db_url, write=True, include_subagents=False)
    assert first.written == 4
    assert first.warnings == []

    second = ingest(source_root, db_url, write=True)
    assert second.written == 2  # the two subagent messages
    assert any(
        MODEL_PRICED_B in w and "available_to_us_at" in w for w in second.warnings
    )


def test_collect_records_on_missing_root(tmp_path: Path) -> None:
    records, stats = collect_records(tmp_path / "does-not-exist")
    assert records == {}
    assert stats.records == 0


def test_compute_cost_without_nested_split_treats_all_as_5m() -> None:
    p = PRICES[MODEL_PRICED_A]
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 4_000,
        # no nested cache_creation object (older schema variant)
    }
    expected = (4_000 * p.input_per_mtok * p.cache_write_5m_mult) / Decimal(1_000_000)
    assert compute_cost_usd(MODEL_PRICED_A, usage) == expected.quantize(Decimal("0.000001"))
    assert compute_cost_usd("no-such-model", usage) is None
    assert compute_cost_usd(MODEL_PRICED_A, None) is None


def test_compute_cost_fast_tier() -> None:
    # fast bills at fast_multiplier x the standard sheet across all token
    # kinds (recomputed from the price table, not frozen constants).
    p = PRICES[MODEL_PRICED_A]
    assert p.fast_multiplier is not None
    standard = compute_cost_usd(MODEL_PRICED_A, USAGE_CACHED)
    fast = compute_cost_usd(MODEL_PRICED_A, dict(USAGE_CACHED, speed="fast"))
    assert fast == (standard * p.fast_multiplier).quantize(Decimal("0.000001"))

    # Models without a published fast price refuse to guess...
    assert PRICES[MODEL_PRICED_B].fast_multiplier is None
    assert compute_cost_usd(MODEL_PRICED_B, dict(USAGE_SIMPLE, speed="fast")) is None
    # ...as do unknown future tiers; absent/standard speed bills as standard.
    assert compute_cost_usd(MODEL_PRICED_A, dict(USAGE_SIMPLE, speed="turbo")) is None
    assert compute_cost_usd(MODEL_PRICED_A, USAGE_SIMPLE) == compute_cost_usd(
        MODEL_PRICED_A, {k: v for k, v in USAGE_SIMPLE.items() if k != "speed"}
    )
