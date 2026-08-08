"""Tests for the contract-external routing_audit Claude Code ingest.

All fixture data below is hand-built synthetic JSONL mirroring the observed
Claude Code session schema (see ingest_claude_code module docstring). No real
session data is used or committed.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.routing_audit.ingest_claude_code import (
    append_run_log,
    collect_records,
    ingest,
    map_project,
    rollback_batch,
)
from traceguard.routing_audit.models import RoutingAuditIngestLog
from traceguard.routing_audit.pricing import (
    KNOWN_RELEASED_AT,
    PRICES,
    ModelPrice,
    cache_creation_split,
    compute_cost_usd,
)
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
    # Scoped to freeze warnings — this test's stated subject. The line here was
    # `assert first.warnings == []`, which incidentally also asserted "and no
    # warning of any other kind ever exists"; that broke when the unpriced-model
    # alarm was added, because the fixture's main transcript carries
    # MODEL_UNPRICED by design. Narrowed rather than deleted: what this test
    # verifies about freeze warnings is unchanged. The other-kind case now has
    # its own coverage in test_unpriced_model_produces_a_warning.
    assert not [w for w in first.warnings if "available_to_us_at" in w]

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


def test_since_skips_files_older_than_cutoff(source_root: Path) -> None:
    # Backdate one main transcript; --since between then and now must skip it.
    old_file = source_root / "-Users-test-Desktop-APP-huadian" / f"{SESS_MAIN}.jsonl"
    old_epoch = (datetime(2026, 1, 1, tzinfo=timezone.utc)).timestamp()
    os.utime(old_file, (old_epoch, old_epoch))

    cutoff = datetime(2026, 5, 1, tzinfo=timezone.utc)
    records, stats = collect_records(source_root, since=cutoff)
    assert stats.files_skipped_mtime >= 1
    # The backdated MAIN file's messages are gone; its (fresh) subagent file
    # and the other project remain.
    assert "msg_001" not in records and "msg_002" not in records
    assert "msg_101" in records  # subagent transcript was not backdated
    assert "msg_201" in records  # other project untouched

    # No cutoff → full scan sees the backdated file again.
    records_full, _ = collect_records(source_root)
    assert "msg_001" in records_full


def test_written_cost_sums_persisted_rows(source_root: Path, db_url: str) -> None:
    stats = ingest(source_root, db_url, write=True)
    engine = make_engine(db_url)
    with Session(engine) as sess:
        db_total = sum(
            (t.cost_usd for t in sess.scalars(select(Trace)) if t.cost_usd is not None),
            Decimal("0"),
        )
    assert stats.written_cost == db_total
    assert stats.written_cost > 0


def test_since_days_string_via_cli(source_root: Path, db_url: str, tmp_path: Path) -> None:
    from traceguard.routing_audit.ingest import main as cli_main

    log = tmp_path / "run.log"
    # Everything is fresh, so --since 1d keeps all files; a JSON line is logged.
    rc = cli_main(
        ["--source", str(source_root), "--db", db_url, "--write", "--since", "1d",
         "--log-file", str(log)]
    )
    assert rc == 0
    entry = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert entry["wrote"] is True
    assert entry["written"] == 6
    assert entry["files_skipped_mtime"] == 0
    assert Decimal(entry["new_cost_usd"]) > 0


def test_append_run_log_never_raises(tmp_path: Path) -> None:
    from traceguard.routing_audit.ingest_claude_code import IngestStats

    log = tmp_path / "sub" / "run.log"  # parent dir does not exist yet
    stats = IngestStats(records=3, written=2, written_cost=Decimal("1.5"))
    stats.batch_id = "cc-test"
    append_run_log(log, stats, wrote=True, error=None)
    entry = json.loads(log.read_text(encoding="utf-8").strip())
    assert entry["batch_id"] == "cc-test"
    assert entry["new_cost_usd"] == "1.500000"


def test_since_future_cutoff_skips_everything(source_root: Path) -> None:
    future = datetime.now(timezone.utc) + timedelta(days=1)
    records, stats = collect_records(source_root, since=future)
    assert records == {}
    assert stats.records == 0
    assert stats.files_main == 0 and stats.files_subagent == 0
    assert stats.files_skipped_mtime >= 3  # every fixture file was skipped


# ---------------------------------------------------------------------------
# Unpriced-model alarm.
#
# claude-opus-5 entered the corpus 2026-07-25 with no PRICES entry. 5,992 traces
# were written with cost_usd = NULL, dropped out of every cost total, and could
# be judged neither compliant nor deviant — for two weeks, with no alarm.
#
# A missing_price counter and a stdout WARNING both already existed, and the
# counter had a test. Neither helped: the WARNING only reached format_report()'s
# stdout, buried under a several-thousand-row table, while the machine-readable
# run log the scheduled job keeps recorded zero warnings for the whole period.
#
# So these tests assert the WARNING and its delivery, not the counter. A
# counter-only assertion is precisely what stayed green while the bug ran.
# ---------------------------------------------------------------------------


def test_unpriced_model_produces_a_warning(source_root: Path, db_url: str) -> None:
    stats = ingest(source_root, db_url, write=True)
    hits = [w for w in stats.warnings if MODEL_UNPRICED in w]
    assert hits, f"a model with no PRICES entry must warn; got warnings={stats.warnings}"
    assert "pricing.PRICES" in hits[0]
    assert stats.unpriced_models[MODEL_UNPRICED] == 1


def test_unpriced_warning_reaches_the_run_log(
    source_root: Path, db_url: str, tmp_path: Path
) -> None:
    """The actual defect: the alarm existed but never reached the monitored channel.

    append_run_log() serialises stats.warnings, so a warning that lands only in
    stdout is invisible to anything reading the job's trail.
    """
    stats = ingest(source_root, db_url, write=True)
    log = tmp_path / "run.log"
    append_run_log(log, stats, wrote=True, error=None)

    entry = json.loads(log.read_text(encoding="utf-8").strip())
    assert any(MODEL_UNPRICED in w for w in entry["warnings"])
    assert entry["unpriced_models"][MODEL_UNPRICED] == 1
    assert entry["written_unpriced"] >= 1


def test_unpriced_warning_fires_on_dry_run_too(source_root: Path) -> None:
    """Learning a model is unpriced must not require committing to a write."""
    stats = ingest(source_root, write=False)
    assert any(MODEL_UNPRICED in w for w in stats.warnings)


def test_cost_total_always_reports_its_exclusions(source_root: Path, db_url: str) -> None:
    """A total that silently omits unpriced rows reads as complete when it is not."""
    from traceguard.routing_audit.ingest_claude_code import format_report

    stats = ingest(source_root, db_url, write=True)
    assert stats.written_unpriced >= 1
    report = format_report(stats, wrote=True)
    assert "unpriced and excluded" in report
    assert f"{stats.written_unpriced} unpriced" in report


def test_priced_model_raises_no_unpriced_warning(source_root: Path, db_url: str) -> None:
    """Guard against the alarm going off for models that are priced."""
    stats = ingest(source_root, db_url, write=True)
    for priced in (MODEL_PRICED_A, MODEL_PRICED_B):
        assert priced not in stats.unpriced_models
        assert not [w for w in stats.warnings if priced in w and "pricing.PRICES" in w]


def test_release_dates_and_prices_do_not_drift() -> None:
    """Every model with a known release date must also have a price.

    No fixture, no corpus, no maintenance. Both dicts live in pricing.py and are
    hand-edited together; adding a release date and forgetting the price is the
    exact half-edit that produced the opus-5 gap. (The converse is deliberately
    not asserted — a price may legitimately be known before the release date is
    verified, which is opus-4-7's current state.)
    """
    missing = sorted(m for m in KNOWN_RELEASED_AT if m not in PRICES)
    assert not missing, f"models with a release date but no PRICES entry: {missing}"


def test_only_unpriced_models_yield_null_cost(source_root: Path, db_url: str) -> None:
    """The invariant, in the form a test can hold: output tokens ⇒ a cost.

    A trace that produced output must carry cost_usd unless its model is one the
    price table genuinely does not cover — in which case the run must have
    warned about that model. There is no third case in which cost may be NULL.
    """
    ingest(source_root, db_url, write=True)
    engine = make_engine(db_url)
    with Session(engine) as sess:
        for trace in sess.scalars(select(Trace)):
            if not trace.tokens_out:
                continue
            if trace.cost_usd is None:
                assert trace.model_id not in PRICES, (
                    f"trace {trace.trace_id} produced {trace.tokens_out} output tokens "
                    f"with cost_usd NULL, but {trace.model_id} IS priced"
                )
            else:
                assert trace.cost_usd > 0


def test_opus_5_is_priced_and_dated() -> None:
    """Regression lock on the specific model this whole change is about."""
    price = PRICES["claude-opus-5"]
    assert price.input_per_mtok == Decimal("5.00")
    assert price.output_per_mtok == Decimal("25.00")
    assert price.fast_multiplier == Decimal("2")
    # Announcement date, not first local observation (2026-07-25 02:06:28).
    assert KNOWN_RELEASED_AT["claude-opus-5"] == datetime(2026, 7, 24, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Cache-creation TTL split: the shape mismatch that made 2.0x unreachable.
#
# USAGE_CACHED above uses the NESTED {"cache_creation": {...}} shape. Zero of
# the 58,210 rows in the local store carry it; 58,194 carry the FLAT
# cache_creation_5m / cache_creation_1h keys. So every cache test in this file
# exercised a branch production never takes, all of them passed, and meanwhile
# every 1-hour cache write in the store was billed at the 5-minute 1.25x rate
# — $1,393.49 low across 27,130 traces.
#
# Multipliers verified 2026-08-08 against the platform prompt-caching page:
# 5m write 1.25x, 1h write 2x, read 0.1x of base input.
# ---------------------------------------------------------------------------

# The production shape. Keep this the default for new cache fixtures.
USAGE_FLAT_1H = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 10_000,
    "cache_creation_5m": 4_000,
    "cache_creation_1h": 6_000,
    "speed": "standard",
}


def test_flat_cache_creation_keys_use_cache_write_1h_mult() -> None:
    """The regression that matters: flat keys must bill 1h writes at 2x."""
    p = PRICES[MODEL_PRICED_A]
    expected = (
        4_000 * p.input_per_mtok * p.cache_write_5m_mult
        + 6_000 * p.input_per_mtok * p.cache_write_1h_mult
    ) / Decimal(1_000_000)
    got = compute_cost_usd(MODEL_PRICED_A, USAGE_FLAT_1H)
    assert got == expected.quantize(Decimal("0.000001"))

    # And it must differ from billing the whole lot at the 5m rate — the exact
    # wrong answer the old code produced.
    all_5m = (10_000 * p.input_per_mtok * p.cache_write_5m_mult) / Decimal(1_000_000)
    assert got != all_5m.quantize(Decimal("0.000001"))
    assert got > all_5m.quantize(Decimal("0.000001"))


def test_cache_creation_nested_and_flat_shapes_agree() -> None:
    nested = {
        "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 10_000,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 4_000,
            "ephemeral_1h_input_tokens": 6_000,
        },
    }
    assert compute_cost_usd(MODEL_PRICED_A, nested) == compute_cost_usd(
        MODEL_PRICED_A, USAGE_FLAT_1H
    )


def test_nested_wins_when_a_record_carries_both() -> None:
    both = dict(USAGE_FLAT_1H)
    both["cache_creation"] = {
        "ephemeral_5m_input_tokens": 10_000,
        "ephemeral_1h_input_tokens": 0,
    }
    m5, h1 = cache_creation_split(both)
    assert (m5, h1) == (10_000, 0)


def test_cache_creation_split_reconciles_against_the_total() -> None:
    """24 production rows disagree between split and total, in both directions.

    The total is the billable count; the split is its composition. No token may
    be dropped (under-reporting split) or invented (over-reporting split).
    """
    # Split under-reports the total: the remainder is 5m, nothing is lost.
    under = {"cache_creation_input_tokens": 86_009,
             "cache_creation_5m": 16_295, "cache_creation_1h": 0}
    assert cache_creation_split(under) == (86_009, 0)

    # Split over-reports the total: clamp, do not invent tokens.
    over = {"cache_creation_input_tokens": 4_566,
            "cache_creation_5m": 11_543, "cache_creation_1h": 0}
    assert cache_creation_split(over) == (4_566, 0)

    # 1h under-reported against a larger total: 1h keeps its premium, rest is 5m.
    mixed = {"cache_creation_input_tokens": 8_895,
             "cache_creation_5m": 0, "cache_creation_1h": 7_459}
    assert cache_creation_split(mixed) == (1_436, 7_459)

    # Consistent rows — the other 58,170 — are untouched.
    ok = {"cache_creation_input_tokens": 10_000,
          "cache_creation_5m": 4_000, "cache_creation_1h": 6_000}
    assert cache_creation_split(ok) == (4_000, 6_000)


def test_cache_creation_split_without_a_total_is_trusted() -> None:
    assert cache_creation_split(
        {"cache_creation_5m": 100, "cache_creation_1h": 200}
    ) == (100, 200)


def test_cache_creation_without_a_split_is_all_5m() -> None:
    assert cache_creation_split({"cache_creation_input_tokens": 4_000}) == (4_000, 0)
    assert cache_creation_split({}) == (0, 0)


def test_every_declared_multiplier_is_reachable() -> None:
    """THE INVARIANT: a multiplier declared on ModelPrice must be reachable.

    A constant that no production input can route through is indistinguishable
    from an absent one — except that it looks handled. cache_write_1h_mult was
    exactly that for the life of the store. This drives each multiplier with a
    usage block that isolates it and asserts the cost lands on that multiplier,
    so a future field cannot be declared and left unwired.
    """
    p = PRICES[MODEL_PRICED_A]
    base = p.input_per_mtok
    mtok = Decimal(1_000_000)

    cases = {
        "cache_read_mult": (
            {"cache_read_input_tokens": 1_000},
            1_000 * base * p.cache_read_mult,
        ),
        "cache_write_5m_mult": (
            {"cache_creation_input_tokens": 1_000, "cache_creation_5m": 1_000,
             "cache_creation_1h": 0},
            1_000 * base * p.cache_write_5m_mult,
        ),
        "cache_write_1h_mult": (
            {"cache_creation_input_tokens": 1_000, "cache_creation_5m": 0,
             "cache_creation_1h": 1_000},
            1_000 * base * p.cache_write_1h_mult,
        ),
        "fast_multiplier": (
            {"input_tokens": 1_000, "speed": "fast"},
            1_000 * base * p.fast_multiplier,
        ),
    }
    declared = {
        f for f in ModelPrice.__dataclass_fields__
        if f.endswith("_mult") or f.endswith("_multiplier")
    }
    assert declared <= set(cases), (
        f"ModelPrice declares multiplier(s) with no reachability test: "
        f"{sorted(declared - set(cases))}"
    )
    for name, (usage, expected) in cases.items():
        got = compute_cost_usd(MODEL_PRICED_A, usage)
        assert got == (expected / mtok).quantize(Decimal("0.000001")), name


# ---------------------------------------------------------------------------
# Task-tag coverage watchdog.
#
# Untagged traces are skipped by routing_decisions BEFORE the tier lookup, so a
# stalled tagger produces no wrong verdict — it produces no verdict, which is
# indistinguishable from "nothing to report". The tagger stopped on 2026-07-03
# and 31,142 traces piled up behind it for five weeks with every report green.
#
# Tested on its TRUE-POSITIVE path with injected state, not by having watched it
# misfire: a watchdog only ever seen crying wolf has not been verified.
# ---------------------------------------------------------------------------


def _add_tag(db_url: str, session_id: str, ts_start: datetime, ts_end=None, seq: int = 0) -> None:
    from traceguard.routing_audit.models import RoutingAuditTaskTag

    engine = make_engine(db_url)
    with Session(engine) as sess:
        sess.add(
            RoutingAuditTaskTag(
                unit_id=f"{session_id}#s{ts_start:%Y%m%d%H%M%S}-{seq}", session_id=session_id,
                project="huadian", ts_start=ts_start, ts_end=ts_end,
                n_turns=1, task_type="research-explore", source="heuristic", batch_id="t",
            )
        )
        sess.commit()


def test_watchdog_fires_when_no_tags_exist(source_root: Path, db_url: str) -> None:
    stats = ingest(source_root, db_url, write=True)
    assert any("task-tag table is EMPTY" in w for w in stats.warnings)


def test_watchdog_fires_on_low_coverage_and_staleness(source_root: Path, db_url: str) -> None:
    from traceguard.routing_audit.ingest_claude_code import tag_coverage_warnings

    ingest(source_root, db_url, write=True)
    # Tag a window covering only the earliest fixture trace, and leave the tag
    # far behind the newest trace.
    _add_tag(db_url, SESS_MAIN, datetime(2026, 5, 1, tzinfo=timezone.utc),
             datetime(2026, 5, 2, tzinfo=timezone.utc))

    warnings = tag_coverage_warnings(make_engine(db_url))
    assert any("coverage" in w and "below the" in w for w in warnings), warnings
    assert any("days behind" in w for w in warnings), warnings


def test_watchdog_silent_when_tagging_is_current(source_root: Path, db_url: str) -> None:
    """The other half: it must not cry wolf on a healthy pipeline."""
    from traceguard.routing_audit.ingest_claude_code import tag_coverage_warnings

    ingest(source_root, db_url, write=True)
    engine = make_engine(db_url)
    with Session(engine) as sess:
        spans = {
            (t.output_parsed["session_id"], t.invoked_at)
            for t in sess.scalars(select(Trace))
        }
    for i, (session_id, ts) in enumerate(sorted(spans)):
        # one open-ended tag per trace → full coverage, newest tag current
        _add_tag(db_url, session_id, ts - timedelta(minutes=1), seq=i)

    assert tag_coverage_warnings(engine) == []


def test_watchdog_warnings_reach_the_run_log(
    source_root: Path, db_url: str, tmp_path: Path
) -> None:
    stats = ingest(source_root, db_url, write=True)
    log = tmp_path / "run.log"
    append_run_log(log, stats, wrote=True, error=None)
    entry = json.loads(log.read_text(encoding="utf-8").strip())
    assert any("task-tag" in w for w in entry["warnings"])


# ---------------------------------------------------------------------------
# Warning-kind canary.
#
# Narrowing `assert first.warnings == []` in the freeze test removed a canary:
# that snapshot also caught "an alarm nobody expected showed up". Restoring it
# as a fixture-specific expected SET would just move the snapshot — it needs
# editing every time a legitimate alarm is added, and decays into a rubber
# stamp. Asserting membership in a REGISTERED set keeps the signal and turns
# the maintenance into the right friction: register the kind, once.
# ---------------------------------------------------------------------------


def test_every_warning_carries_a_registered_kind(source_root: Path, db_url: str) -> None:
    from traceguard.routing_audit.ingest_claude_code import WARNING_KINDS

    stats = ingest(source_root, db_url, write=True)
    assert stats.warnings, "fixture should raise at least one warning"
    for w in stats.warnings:
        assert w.startswith("["), f"warning is not kind-tagged: {w!r}"
        kind = w[1:].split("]", 1)[0]
        assert kind in WARNING_KINDS, (
            f"unregistered warning kind {kind!r} — add it to WARNING_KINDS with a "
            f"one-line comment saying what it means. Full text: {w!r}"
        )


def test_warn_rejects_an_unregistered_kind() -> None:
    from traceguard.routing_audit.ingest_claude_code import warn

    assert warn("unpriced_model", "x") == "[unpriced_model] x"
    with pytest.raises(ValueError, match="unregistered warning kind"):
        warn("brand_new_alarm_nobody_registered", "x")


def test_registered_kinds_are_all_actually_emittable() -> None:
    """The registry must not accumulate kinds nothing emits any more.

    A stale entry would let a future test pass on a kind that no code path can
    produce, which is the same failure mode as an unreachable multiplier.
    """
    import inspect

    from traceguard.routing_audit import ingest_claude_code as mod
    from traceguard.routing_audit.ingest_claude_code import WARNING_KINDS

    src = inspect.getsource(mod)
    for kind in WARNING_KINDS:
        assert f'"{kind}",' in src or f"'{kind}'," in src, (
            f"WARNING_KINDS declares {kind!r} but no warn() call emits it"
        )
