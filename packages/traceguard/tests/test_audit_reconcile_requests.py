"""Per-request existence reconciliation (SPEC v1.2, layer L1.5).

Totals can cancel: an under-reported call and an over-reported one net out,
and the provider usage API supplies no call counts at all. These tests pin the
question totals cannot ask — is THIS call present on both sides — and the
three disciplines that keep the answer from being noise.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from traceguard import audit
from traceguard.audit.reconcile import (
    CAPTURE_UNMATCHED,
    DIRECTION_OUT_OF_BAND_ONLY,
    DIRECTION_SELF_REPORTED_ONLY,
    DIRECTION_TEXT,
    REQUEST_LEDGER_SCHEMA,
    load_request_ledger,
    parse_request_ledger,
    reconcile_requests,
)
from traceguard.store.models import Trace

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=7)


def _trace(engine, response_id, *, ts=None, project="proj", operation="llm_complete"):
    with Session(engine) as sess:
        sess.add(
            Trace(
                project=project,
                component="c",
                operation=operation,
                input_hash="h" * 64,
                parse_status="success",
                invoked_at=ts or (T0 + timedelta(hours=1)),
                provider_response_id=response_id,
            )
        )
        sess.commit()


def _ledger(*ids, source="acme-gateway", window=(T0, T1), ts=None):
    payload = {
        "ledger": REQUEST_LEDGER_SCHEMA,
        "source": source,
        "requests": [
            {
                "response_id": rid,
                "model": "claude-x",
                "ts": (ts or (T0 + timedelta(hours=1))).isoformat(),
                "tokens_in": 10,
                "tokens_out": 5,
                "cost_usd": None,
            }
            for rid in ids
        ],
    }
    if window is not None:
        payload["window"] = [window[0].isoformat(), window[1].isoformat()]
    return parse_request_ledger(payload)


def _run(engine, ledger, **kw):
    kw.setdefault("starting_at", T0)
    kw.setdefault("ending_at", T1)
    return reconcile_requests(engine, ledger=ledger, **kw)


# ── the four cases the format exists to distinguish ─────────────────────────

def test_full_match_is_clean(engine):
    for rid in ("msg_1", "msg_2"):
        _trace(engine, rid)
    result = _run(engine, _ledger("msg_1", "msg_2"))
    assert result.ok
    assert result.matched == 2
    assert result.findings == []
    assert "2 matched" in result.summary()


def test_ledger_only_is_out_of_band_only(engine):
    _trace(engine, "msg_1")
    result = _run(engine, _ledger("msg_1", "msg_2"))
    assert not result.ok
    assert result.out_of_band_only == ["msg_2"]
    assert result.self_reported_only == []
    finding = result.findings[0]
    assert finding.kind == CAPTURE_UNMATCHED
    assert finding.severity == "WARN"
    assert finding.direction == DIRECTION_OUT_OF_BAND_ONLY
    # The fixed interpretation, verbatim — two runs must read the same claim.
    assert "a call the capture layer did not see — bypass or wrapper coverage gap" in finding.detail


def test_traces_only_is_self_reported_only(engine):
    _trace(engine, "msg_1")
    _trace(engine, "msg_ghost")
    result = _run(engine, _ledger("msg_1"))
    assert result.self_reported_only == ["msg_ghost"]
    finding = next(f for f in result.findings if "msg_ghost" in f.detail)
    assert finding.direction == DIRECTION_SELF_REPORTED_ONLY
    assert (
        "a record the provider side does not vouch for — fabrication, duplication, "
        "or an incomplete ledger" in finding.detail
    )
    assert finding.trace_id is not None  # points at the row to go look at


def test_both_directions_at_once(engine):
    _trace(engine, "msg_1")
    _trace(engine, "msg_ghost")
    result = _run(engine, _ledger("msg_1", "msg_missed"))
    assert result.matched == 1
    assert result.out_of_band_only == ["msg_missed"]
    assert result.self_reported_only == ["msg_ghost"]
    dirs = {f.direction for f in result.findings}
    assert dirs == {DIRECTION_OUT_OF_BAND_ONLY, DIRECTION_SELF_REPORTED_ONLY}


# ── duplicates are their own class, not "missing" ───────────────────────────

def test_duplicate_in_the_ledger_is_reported_separately(engine):
    _trace(engine, "msg_1")
    result = _run(engine, _ledger("msg_1", "msg_1"))
    assert result.ledger_duplicates == {"msg_1": 2}
    assert result.matched == 1
    assert result.out_of_band_only == [] and result.self_reported_only == []
    dup = next(f for f in result.findings if "appears 2 times in the ledger" in f.detail)
    assert dup.kind == CAPTURE_UNMATCHED
    assert dup.direction == DIRECTION_OUT_OF_BAND_ONLY


def test_duplicate_in_traces_is_reported_separately(engine):
    _trace(engine, "msg_1")
    _trace(engine, "msg_1")
    result = _run(engine, _ledger("msg_1"))
    assert result.traces_duplicates == {"msg_1": 2}
    dup = next(f for f in result.findings if "appears on 2 traces" in f.detail)
    assert dup.direction == DIRECTION_SELF_REPORTED_ONLY
    assert "one provider response recorded as several calls" in dup.detail


# ── the three disciplines ───────────────────────────────────────────────────

def test_traces_without_a_response_id_are_counted_and_excluded(engine):
    """NULL means the provider gave no id (pre-column rows, streaming calls).
    Reporting that as self_reported_only would accuse the capture layer of
    fabrication exactly where it was honest about not knowing."""
    _trace(engine, "msg_1")
    _trace(engine, None)
    _trace(engine, None)
    result = _run(engine, _ledger("msg_1"))
    assert result.ok
    assert result.traces_without_response_id == 2
    assert result.self_reported_only == []
    assert "2 trace(s) in the window carry none and were not compared" in result.summary()


def test_a_ledger_that_does_not_cover_the_window_is_refused(engine):
    """Every trace in the uncovered stretch would report as self_reported_only —
    a screenful of findings that are pure artifact (SPEC B3.4)."""
    _trace(engine, "msg_1")
    narrow = _ledger("msg_1", window=(T0, T0 + timedelta(days=1)))
    with pytest.raises(ValueError) as excinfo:
        _run(engine, narrow)
    message = str(excinfo.value)
    assert "does not cover the requested" in message
    assert "Narrow the window" in message


def test_a_ledger_covering_the_window_exactly_is_accepted(engine):
    _trace(engine, "msg_1")
    assert _run(engine, _ledger("msg_1", window=(T0, T1))).ok


def test_a_ledger_with_no_declared_window_is_accepted(engine):
    """`window` is optional; when absent there is nothing to contradict."""
    _trace(engine, "msg_1")
    assert _run(engine, _ledger("msg_1", window=None)).ok


def test_entries_outside_the_window_are_ignored(engine):
    _trace(engine, "msg_1")
    ledger = parse_request_ledger(
        {
            "ledger": REQUEST_LEDGER_SCHEMA,
            "source": "g",
            "requests": [
                {"response_id": "msg_1", "ts": (T0 + timedelta(hours=1)).isoformat()},
                {"response_id": "msg_old", "ts": (T0 - timedelta(days=30)).isoformat()},
            ],
        }
    )
    result = _run(engine, ledger)
    assert result.ledger_requests == 1
    assert result.ok


def test_traces_outside_the_window_are_ignored(engine):
    _trace(engine, "msg_old", ts=T0 - timedelta(days=1))
    assert _run(engine, _ledger(window=None)).ok


def test_project_and_operation_filters_apply(engine):
    _trace(engine, "msg_other", project="other")
    _trace(engine, "msg_embed", operation="embedding")
    assert _run(engine, _ledger(window=None), project="proj").ok


# ── ledger parsing refuses what it cannot reconcile ─────────────────────────

def test_wrong_schema_tag_is_refused(engine):
    with pytest.raises(ValueError, match="not a request-ledger/v1 document"):
        parse_request_ledger({"ledger": "something/v9", "source": "g", "requests": []})


def test_missing_source_is_refused():
    """A finding that cannot say which side vouched for a call is not evidence."""
    with pytest.raises(ValueError, match="non-empty 'source'"):
        parse_request_ledger({"ledger": REQUEST_LEDGER_SCHEMA, "requests": []})


def test_entry_without_a_response_id_is_refused():
    with pytest.raises(ValueError, match="join key"):
        parse_request_ledger(
            {
                "ledger": REQUEST_LEDGER_SCHEMA,
                "source": "g",
                "requests": [{"ts": T0.isoformat()}],
            }
        )


def test_entry_without_a_timestamp_is_refused():
    with pytest.raises(ValueError, match="has no 'ts'"):
        parse_request_ledger(
            {
                "ledger": REQUEST_LEDGER_SCHEMA,
                "source": "g",
                "requests": [{"response_id": "msg_1"}],
            }
        )


def test_naive_timestamp_is_refused():
    with pytest.raises(ValueError, match="must carry a timezone"):
        parse_request_ledger(
            {
                "ledger": REQUEST_LEDGER_SCHEMA,
                "source": "g",
                "requests": [{"response_id": "m", "ts": "2026-09-01T00:00:00"}],
            }
        )


def test_cost_is_kept_as_a_string(engine):
    ledger = parse_request_ledger(
        {
            "ledger": REQUEST_LEDGER_SCHEMA,
            "source": "g",
            "requests": [
                {"response_id": "m", "ts": T0.isoformat(), "cost_usd": 0.0123456789}
            ],
        }
    )
    assert isinstance(ledger.requests[0].cost_usd, str)


def test_load_request_ledger_reads_a_file(tmp_path, engine):
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {
                "ledger": REQUEST_LEDGER_SCHEMA,
                "source": "acme-gateway",
                "window": [T0.isoformat(), T1.isoformat()],
                "requests": [{"response_id": "msg_1", "ts": (T0 + timedelta(hours=1)).isoformat()}],
            }
        ),
        encoding="utf-8",
    )
    ledger = load_request_ledger(path)
    assert ledger.source == "acme-gateway"
    assert [r.response_id for r in ledger.requests] == ["msg_1"]


# ── findings are capped, and the cap is stated ──────────────────────────────

def test_many_unmatched_are_capped_and_the_remainder_is_named(engine):
    """A 40k-row bypass must not produce 40k findings — but the count must not
    disappear either (no silent truncation)."""
    ids = [f"msg_{i:04d}" for i in range(120)]
    result = _run(engine, _ledger(*ids))
    unmatched = [f for f in result.findings if f.direction == DIRECTION_OUT_OF_BAND_ONLY]
    assert len(unmatched) == 51  # 50 itemized + 1 summary line
    assert "and 70 more out_of_band_only response_id(s) not itemized" in unmatched[-1].detail
    assert len(result.out_of_band_only) == 120  # the full list is still on the result


# ── the aggregate path is untouched ─────────────────────────────────────────

def test_capture_mismatch_path_is_unchanged(engine):
    """The two kinds prove different things and must not converge."""
    from traceguard.audit.reconcile import UsageBucket, reconcile

    _trace(engine, "msg_1")
    with Session(engine) as sess:
        row = sess.scalars(__import__("sqlalchemy").select(Trace)).one()
        assert row.provider_response_id == "msg_1"

    result = reconcile(
        engine,
        starting_at=T0,
        ending_at=T1,
        provider=[UsageBucket(T0, T1, "claude-x", 0, 0)],
    )
    assert all(f.kind == "capture_mismatch" for f in result.findings)
    assert all(f.direction is None for f in result.findings)


def test_the_two_kinds_are_distinct_and_both_frozen_warn():
    assert CAPTURE_UNMATCHED != audit.CAPTURE_MISMATCH
    assert audit.FINDING_SEVERITY[CAPTURE_UNMATCHED] == "WARN"
    assert audit.FINDING_SEVERITY[audit.CAPTURE_MISMATCH] == "WARN"
    assert set(DIRECTION_TEXT) == {DIRECTION_OUT_OF_BAND_ONLY, DIRECTION_SELF_REPORTED_ONLY}


# ── CLI ─────────────────────────────────────────────────────────────────────

def test_cli_requests_json_exits_1_on_unmatched(tmp_path, capsys):
    import traceguard
    from traceguard.audit.__main__ import main

    url = f"sqlite:///{tmp_path/'t.db'}"
    eng = traceguard.make_engine(url)
    _trace(eng, "msg_ghost")

    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {
                "ledger": REQUEST_LEDGER_SCHEMA,
                "source": "acme-gateway",
                "window": [T0.isoformat(), T1.isoformat()],
                "requests": [],
            }
        ),
        encoding="utf-8",
    )
    code = main(
        [
            "--db", url, "reconcile",
            "--source", f"requests-json:{path}",
            "--window", f"{T0.isoformat()},{T1.isoformat()}",
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "capture_unmatched" in out
    assert f"direction={DIRECTION_SELF_REPORTED_ONLY}" in out


def test_cli_requests_json_exits_0_when_everything_matches(tmp_path, capsys):
    import traceguard
    from traceguard.audit.__main__ import main

    url = f"sqlite:///{tmp_path/'t.db'}"
    eng = traceguard.make_engine(url)
    _trace(eng, "msg_1")

    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {
                "ledger": REQUEST_LEDGER_SCHEMA,
                "source": "acme-gateway",
                "window": [T0.isoformat(), T1.isoformat()],
                "requests": [{"response_id": "msg_1", "ts": (T0 + timedelta(hours=1)).isoformat()}],
            }
        ),
        encoding="utf-8",
    )
    assert main([
        "--db", url, "reconcile",
        "--source", f"requests-json:{path}",
        "--window", f"{T0.isoformat()},{T1.isoformat()}",
    ]) == 0


def test_cli_rejects_a_ledger_that_does_not_cover_the_window(tmp_path, capsys):
    import traceguard
    from traceguard.audit.__main__ import main

    url = f"sqlite:///{tmp_path/'t.db'}"
    traceguard.make_engine(url)
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {
                "ledger": REQUEST_LEDGER_SCHEMA,
                "source": "g",
                "window": [T0.isoformat(), (T0 + timedelta(days=1)).isoformat()],
                "requests": [],
            }
        ),
        encoding="utf-8",
    )
    assert main([
        "--db", url, "reconcile",
        "--source", f"requests-json:{path}",
        "--window", f"{T0.isoformat()},{T1.isoformat()}",
    ]) == 2
    assert "does not cover the requested" in capsys.readouterr().err
