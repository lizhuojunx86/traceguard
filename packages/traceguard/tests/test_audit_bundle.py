"""Evidence bundle export + offline verify (evidence-bundle/v1, SPEC v1.2).

The load-bearing property is that `full` and `hash_only` support DIFFERENT
conclusions and never share a word. Most of these tests exist to keep the
weaker mode from being read as the stronger one.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from traceguard import audit, sources
from traceguard.audit.bundle import (
    BUNDLE_SCHEMA,
    CONTENT_NOT_RECOMPUTED,
    INFO,
    anchor_record,
    export_bundle,
    load_bundle,
    verify_bundle,
    write_bundle,
)
from traceguard.sdk.tracer import Tracer
from traceguard.store.models import Trace

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "docs" / "specs" / "evidence-bundle-v1.schema.json"
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def chained(engine):
    """An audited DB with three chained traces, one carrying a source snapshot."""
    audit.enable(engine)
    sources.enable(engine)
    tg = Tracer(engine=engine)
    for i in range(3):
        with tg.span("proj", "comp", "llm_complete", feature_as_of=NOW) as span:
            span.record_input({"q": f"question {i}"})
            span.record_output(parsed={"answer": f"answer {i}"})
            if i == 0:
                span.record_source(
                    sources.SourceSnapshot(
                        source_uri="https://v.example/eps",
                        source_kind="vendor_api",
                        content_hash="a" * 64,
                        retrieved_at=NOW - timedelta(days=1),
                        published_at=NOW - timedelta(days=2),
                    ),
                    strict=False,
                )
    yield engine
    audit.detach(engine)


# ── round trip, both modes ──────────────────────────────────────────────────

def test_full_round_trip_verifies(chained):
    bundle = export_bundle(chained)
    assert bundle["schema"] == BUNDLE_SCHEMA
    assert bundle["content_mode"] == "full"
    assert len(bundle["traces"]) == 3
    assert len(bundle["chain"]["entries"]) == 3
    assert len(bundle["source_snapshots"]) == 1
    assert bundle["approvals"] == []  # reserved, always empty today

    result = verify_bundle(bundle)
    assert result.ok
    assert result.content_recomputed == 3
    assert "VERIFIED (full)" in result.summary()


def test_hash_only_round_trip_verifies_linkage_only(chained):
    bundle = export_bundle(chained, content_mode="hash_only")
    for trace in bundle["traces"]:
        for stripped in ("input_summary", "output_parsed", "error_message"):
            assert stripped not in trace
        assert "input_hash" in trace  # the digest stays; the content does not

    result = verify_bundle(bundle)
    assert result.ok
    assert result.content_recomputed == 0
    info = [f for f in result.findings if f.kind == CONTENT_NOT_RECOMPUTED]
    assert len(info) == 1 and info[0].severity == INFO


def test_the_two_modes_never_share_a_word(chained):
    """A hash_only pass says nothing about content; saying 'verified' for both
    would be this format's worst failure mode in one word."""
    full = verify_bundle(export_bundle(chained)).summary()
    hash_only = verify_bundle(export_bundle(chained, content_mode="hash_only")).summary()
    assert "VERIFIED (full)" in full
    assert "VERIFIED" not in hash_only
    assert "LINKAGE OK (hash_only)" in hash_only
    assert "content was NOT recomputed" in hash_only


def test_hash_only_always_carries_the_scope_finding_even_when_clean(chained):
    result = verify_bundle(export_bundle(chained, content_mode="hash_only"))
    assert result.ok  # a clean result...
    assert any(f.kind == CONTENT_NOT_RECOMPUTED for f in result.findings)  # ...still annotated


# ── tamper detection: what each mode catches ────────────────────────────────

def test_full_mode_detects_changed_trace_content(chained):
    bundle = export_bundle(chained)
    bundle["traces"][1]["output_parsed"] = {"answer": "tampered"}
    result = verify_bundle(bundle)
    assert not result.ok
    assert any(f.kind == "hash_mismatch" for f in result.breaks)


def test_hash_only_mode_CANNOT_detect_changed_content(chained):
    """Stated as a test because it is the documented limit, not an oversight:
    with the content stripped there is nothing left to recompute against."""
    bundle = export_bundle(chained, content_mode="hash_only")
    bundle["traces"][1]["input_hash"] = "0" * 64  # a content-ish field, changed
    result = verify_bundle(bundle)
    assert result.ok  # undetected — and the summary says why
    assert "content was NOT recomputed" in result.summary()


@pytest.mark.parametrize("mode", ["full", "hash_only"])
def test_both_modes_detect_a_broken_prev_hash(chained, mode):
    bundle = export_bundle(chained, content_mode=mode)
    bundle["chain"]["entries"][2]["prev_hash"] = "0" * 64
    result = verify_bundle(bundle)
    assert not result.ok
    assert any(f.kind == "link_broken" for f in result.breaks)


def test_full_mode_detects_forged_entry_metadata(chained):
    """Entry metadata is inside the preimage, so re-pointing trace_id shows up."""
    bundle = export_bundle(chained)
    bundle["chain"]["entries"][0]["entry_type"] = "backfill"
    assert any(f.kind == "hash_mismatch" for f in verify_bundle(bundle).breaks)


def test_a_referenced_trace_missing_from_the_bundle_is_a_break(chained):
    bundle = export_bundle(chained)
    bundle["traces"] = bundle["traces"][:1]
    assert any(f.kind == "missing_trace" for f in verify_bundle(bundle).breaks)


# ── anchors ─────────────────────────────────────────────────────────────────

def test_matching_anchor_is_checked(chained):
    head = audit.export_anchor(chained)
    bundle = export_bundle(chained, anchors=[anchor_record(head, location="/mnt/x/a.jsonl")])
    result = verify_bundle(bundle)
    assert result.ok
    assert result.anchors_checked == 1


def test_anchor_disagreeing_with_the_head_is_a_break(chained):
    head = audit.export_anchor(chained)
    record = anchor_record(head)
    record["row_hash"] = "9" * 64
    result = verify_bundle(export_bundle(chained, anchors=[record]))
    assert not result.ok
    assert any(f.kind == "anchor_mismatch" for f in result.breaks)


def test_unknown_anchor_kind_is_a_break(chained):
    bundle = export_bundle(chained, anchors=[{"kind": "carrier-pigeon"}])
    assert any(f.kind == "anchor_malformed" for f in verify_bundle(bundle).breaks)


def test_rfc3161_anchor_is_structure_checked_not_verified(chained):
    """traceguard reports a well-formed token as PRESENT, never as valid: it
    does no cryptography (zero runtime deps) — that is `openssl ts`'s job."""
    good = {
        "kind": "rfc3161",
        "tsa_url": "https://freetsa.org/tsr",
        "digest_alg": "sha256",
        "message_imprint": "b" * 64,
        "token_b64": "MIIBog==",
    }
    assert verify_bundle(export_bundle(chained, anchors=[good])).ok

    incomplete = dict(good)
    del incomplete["token_b64"]
    result = verify_bundle(export_bundle(chained, anchors=[incomplete]))
    assert any(f.kind == "anchor_malformed" for f in result.breaks)
    assert "never verifies its signature" in result.breaks[0].detail


def test_a_pending_ots_anchor_warns_that_it_is_not_evidence(chained):
    bundle = export_bundle(
        chained, anchors=[{"kind": "ots", "ots_status": "pending", "ots_proof_path": "/x.ots"}]
    )
    result = verify_bundle(bundle)
    assert result.ok  # a warning, not a break
    pending = [f for f in result.findings if f.kind == "anchor_pending"]
    assert len(pending) == 1
    assert "not evidence" in pending[0].detail


def test_a_complete_ots_anchor_does_not_warn(chained):
    bundle = export_bundle(chained, anchors=[{"kind": "ots", "ots_status": "complete"}])
    assert not [f for f in verify_bundle(bundle).findings if f.kind == "anchor_pending"]


# ── selection, schema, IO ───────────────────────────────────────────────────

def test_selection_by_trace_ids(chained):
    with Session(chained) as sess:
        ids = [t.trace_id for t in sess.scalars(select(Trace))][:2]
    bundle = export_bundle(chained, trace_ids=ids)
    assert [t["trace_id"] for t in bundle["traces"]] == ids
    assert verify_bundle(bundle).ok


def test_selection_by_window(chained):
    bundle = export_bundle(chained, since=NOW + timedelta(days=365))
    assert bundle["traces"] == []
    assert verify_bundle(bundle).ok


def test_include_sources_false_omits_snapshots(chained):
    assert export_bundle(chained, include_sources=False)["source_snapshots"] == []


def test_wrong_schema_tag_is_refused():
    with pytest.raises(ValueError, match="not an evidence-bundle/v1 document"):
        verify_bundle({"schema": "something/v9", "content_mode": "full"})


def test_bad_content_mode_is_refused(chained):
    with pytest.raises(ValueError, match="content_mode"):
        export_bundle(chained, content_mode="partial")
    with pytest.raises(ValueError, match="content_mode"):
        verify_bundle({"schema": BUNDLE_SCHEMA, "content_mode": "partial"})


def test_write_and_load_round_trip(chained, tmp_path):
    path = write_bundle(export_bundle(chained), tmp_path / "sub" / "evidence.json")
    assert path.exists()
    assert verify_bundle(load_bundle(path)).ok


@pytest.mark.parametrize("mode", ["full", "hash_only"])
def test_bundle_conforms_to_the_published_schema(chained, mode):
    """The schema is the published contract; this keeps the emitter honest."""
    jsonschema = pytest.importorskip("jsonschema")
    if not SCHEMA_PATH.is_file():
        pytest.skip(f"schema not reachable from the package tree: {SCHEMA_PATH}")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    head = audit.export_anchor(chained)
    bundle = export_bundle(chained, content_mode=mode, anchors=[anchor_record(head)])
    payload = {k: v for k, v in bundle.items() if not k.startswith("_")}
    jsonschema.validate(payload, schema)


def test_schema_rejects_an_rfc3161_anchor_without_its_token():
    jsonschema = pytest.importorskip("jsonschema")
    if not SCHEMA_PATH.is_file():
        pytest.skip("schema not reachable")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    payload = {
        "schema": BUNDLE_SCHEMA,
        "generated_at": NOW.isoformat(),
        "generator": "traceguard test",
        "content_mode": "full",
        "chain": {"algo_version": 1, "entries": []},
        "traces": [],
        "anchors": [{"kind": "rfc3161", "tsa_url": "https://x", "digest_alg": "sha256"}],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


def test_empty_approvals_is_valid(chained):
    jsonschema = pytest.importorskip("jsonschema")
    if not SCHEMA_PATH.is_file():
        pytest.skip("schema not reachable")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    bundle = export_bundle(chained)
    assert bundle["approvals"] == []
    jsonschema.validate({k: v for k, v in bundle.items() if not k.startswith("_")}, schema)


# ── cost events travel with the bundle ──────────────────────────────────────

def test_a_cost_event_entry_verifies(chained):
    with Session(chained) as sess:
        trace_id = sess.scalars(select(Trace)).first().trace_id
    audit.record_cost_event(
        chained, trace_id=trace_id, event_type="correction",
        old_value=None, new_value="0.25", reason="test",
    )
    with chained.begin() as conn:
        conn.execute(update(Trace).where(Trace.trace_id == trace_id).values(cost_usd="0.25"))
    result = verify_bundle(export_bundle(chained))
    assert result.ok, [f.detail for f in result.breaks]


# ── CLI ─────────────────────────────────────────────────────────────────────

def test_cli_bundle_then_verify_bundle(tmp_path, capsys):
    import traceguard
    from traceguard.audit.__main__ import main

    url = f"sqlite:///{tmp_path/'t.db'}"
    eng = traceguard.make_engine(url)
    audit.enable(eng)
    tg = Tracer(engine=eng)
    with tg.span("p", "c", "llm_complete") as span:
        span.record_input({"q": "hi"})
    audit.detach(eng)

    out = tmp_path / "evidence.json"
    assert main(["--db", url, "bundle", "--out", str(out)]) == 0
    assert out.exists()
    assert main(["verify-bundle", str(out)]) == 0
    assert "VERIFIED (full)" in capsys.readouterr().out


def test_cli_verify_bundle_exits_1_on_tamper(tmp_path, capsys):
    import traceguard
    from traceguard.audit.__main__ import main

    url = f"sqlite:///{tmp_path/'t.db'}"
    eng = traceguard.make_engine(url)
    audit.enable(eng)
    tg = Tracer(engine=eng)
    with tg.span("p", "c", "llm_complete") as span:
        span.record_input({"q": "hi"})
    audit.detach(eng)

    out = tmp_path / "evidence.json"
    main(["--db", url, "bundle", "--out", str(out)])
    data = json.loads(out.read_text())
    data["traces"][0]["input_summary"] = "tampered"
    out.write_text(json.dumps(data))

    assert main(["verify-bundle", str(out)]) == 1
    assert "hash_mismatch" in capsys.readouterr().out


def test_cli_hash_only_says_what_it_did_not_check(tmp_path, capsys):
    import traceguard
    from traceguard.audit.__main__ import main

    url = f"sqlite:///{tmp_path/'t.db'}"
    eng = traceguard.make_engine(url)
    audit.enable(eng)
    audit.detach(eng)

    out = tmp_path / "evidence.json"
    assert main(["--db", url, "bundle", "--out", str(out), "--hash-only"]) == 0
    assert "not content" in capsys.readouterr().err
    assert main(["verify-bundle", str(out)]) == 0
    assert "LINKAGE OK (hash_only)" in capsys.readouterr().out


def test_cli_verify_bundle_needs_no_database(tmp_path, capsys, monkeypatch):
    """The format's whole point: a recipient with only the file can check it."""
    import traceguard
    from traceguard.audit.__main__ import main

    url = f"sqlite:///{tmp_path/'t.db'}"
    eng = traceguard.make_engine(url)
    audit.enable(eng)
    audit.detach(eng)
    out = tmp_path / "evidence.json"
    main(["--db", url, "bundle", "--out", str(out)])

    def _no_db(*args, **kwargs):
        raise AssertionError("verify-bundle must not open a database")

    monkeypatch.setattr("traceguard.audit.__main__.make_engine", _no_db)
    assert main(["verify-bundle", str(out)]) == 0
