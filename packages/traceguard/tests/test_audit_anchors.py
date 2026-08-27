"""Anchor sinks + periodic anchoring (audit v2): every sink stores what
export_anchor produced, failures are loud, and the scheduler keeps its cadence."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard import audit
from traceguard.audit.anchors import (
    AnchorScheduler,
    AnchorSinkError,
    FileAnchorSink,
    GitNoteAnchorSink,
    WebhookAnchorSink,
    anchor_to,
    parse_sink_spec,
)
from traceguard.store.models import Trace, make_engine


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:", create_all=True)
    audit.enable(eng, backfill=False)
    yield eng
    audit.detach(eng)
    audit.set_strict(False)


def _add_trace(engine: Engine) -> None:
    with Session(engine) as sess:
        sess.add(
            Trace(
                project="p",
                component="c",
                operation="llm_complete",
                input_hash="h" * 64,
                parse_status="success",
                invoked_at=datetime.now(timezone.utc),
            )
        )
        sess.commit()


class _FailingSink:
    name = "failing"

    def store(self, anchor) -> None:
        raise OSError("disk on fire")


# ── file sink ─────────────────────────────────────────────────────────────


def test_file_sink_appends_json_lines_and_reads_back_latest(engine, tmp_path: Path) -> None:
    sink = FileAnchorSink(tmp_path / "nested" / "anchors.jsonl")
    _add_trace(engine)
    first = anchor_to(engine, [sink])
    _add_trace(engine)
    second = anchor_to(engine, [sink])
    lines = sink.path.read_text().splitlines()
    assert len(lines) == 2
    assert audit.ChainAnchor.from_json(lines[0]) == first
    assert sink.latest() == second
    assert [a.seq for a in sink.history()] == [1, 2]


def test_file_sink_skips_garbage_lines(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    good = audit.ChainAnchor(seq=3, row_hash="ab", algo_version=1, entry_count=3, exported_at="t")
    path.write_text("not json\n" + good.to_json() + '\n\n{"seq": 1}\n')
    sink = FileAnchorSink(path)
    assert sink.latest() == good
    assert list(sink.history()) == [good]


def test_file_sink_latest_is_none_when_missing(tmp_path: Path) -> None:
    assert FileAnchorSink(tmp_path / "absent.jsonl").latest() is None


# ── webhook sink ──────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return b"{}"


def _fake_opener(status: int, calls: list):
    def opener(req, timeout):
        calls.append((req, timeout))
        return _FakeResponse(status)

    return opener


def test_webhook_sink_posts_the_anchor_json(engine) -> None:
    calls: list = []
    sink = WebhookAnchorSink(
        "https://example.invalid/anchors",
        headers={"Authorization": "Bearer t"},
        timeout=3.0,
        opener=_fake_opener(200, calls),
    )
    anchor = anchor_to(engine, [sink])
    ((req, timeout),) = calls
    assert req.get_method() == "POST"
    assert req.full_url == "https://example.invalid/anchors"
    assert req.get_header("Content-type") == "application/json"
    assert req.get_header("Authorization") == "Bearer t"
    assert timeout == 3.0
    assert json.loads(req.data.decode("ascii")) == json.loads(anchor.to_json())


@pytest.mark.parametrize("status", [301, 400, 500])
def test_webhook_sink_non_2xx_is_a_failure(engine, status) -> None:
    sink = WebhookAnchorSink("https://example.invalid/a", opener=_fake_opener(status, []))
    with pytest.raises(AnchorSinkError, match=f"HTTP {status}"):
        anchor_to(engine, [sink])


def test_webhook_sink_transport_error_is_a_failure(engine) -> None:
    def broken(req, timeout):
        raise ConnectionError("no route")

    sink = WebhookAnchorSink("https://example.invalid/a", opener=broken)
    with pytest.raises(AnchorSinkError, match="no route"):
        anchor_to(engine, [sink])


# ── git-note sink ─────────────────────────────────────────────────────────


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = tmp_path / "repo"
    repo.mkdir()
    env_cmds = [
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.invalid"],
        ["git", "config", "user.name", "t"],
        ["git", "commit", "-q", "--allow-empty", "-m", "root"],
    ]
    for cmd in env_cmds:
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    return repo


def test_git_note_sink_appends_notes_on_head(engine, git_repo: Path) -> None:
    sink = GitNoteAnchorSink(git_repo)
    first = anchor_to(engine, [sink])
    _add_trace(engine)
    second = anchor_to(engine, [sink])
    shown = subprocess.run(
        ["git", "notes", "--ref", "refs/notes/traceguard-audit", "show", "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert first.to_json() in shown and second.to_json() in shown
    assert sink.latest() == second


def test_git_note_sink_latest_is_none_without_notes(git_repo: Path) -> None:
    assert GitNoteAnchorSink(git_repo).latest() is None


def test_git_note_sink_outside_a_repo_is_a_failure(engine, tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    sink = GitNoteAnchorSink(tmp_path / "not-a-repo")
    with pytest.raises(AnchorSinkError, match="git notes append failed"):
        anchor_to(engine, [sink])


# ── anchor_to: fail-closed after trying every sink ────────────────────────


def test_anchor_to_tries_every_sink_then_raises(engine, tmp_path: Path) -> None:
    good = FileAnchorSink(tmp_path / "a.jsonl")
    with pytest.raises(AnchorSinkError) as excinfo:
        anchor_to(engine, [_FailingSink(), good])
    assert "failing: disk on fire" in str(excinfo.value)
    assert "1 sink(s)" in str(excinfo.value)
    assert good.latest() is not None  # the good sink still got it


def test_anchor_to_with_no_sinks_just_exports(engine) -> None:
    _add_trace(engine)
    exported = audit.export_anchor(engine)
    anchored = anchor_to(engine, [])
    assert (anchored.seq, anchored.row_hash, anchored.entry_count) == (
        exported.seq,
        exported.row_hash,
        exported.entry_count,
    )


# ── scheduler ─────────────────────────────────────────────────────────────


def test_scheduler_run_once_stores_and_counts(engine, tmp_path: Path) -> None:
    sink = FileAnchorSink(tmp_path / "a.jsonl")
    sched = AnchorScheduler(engine, [sink], interval_s=60)
    anchor = sched.run_once()
    assert anchor is not None and sched.anchors_stored == 1 and sched.failures == 0
    assert sched.last_anchor == anchor == sink.latest()


def test_scheduler_logs_and_continues_on_sink_failure(engine, tmp_path: Path, caplog) -> None:
    good = FileAnchorSink(tmp_path / "a.jsonl")
    sched = AnchorScheduler(engine, [_FailingSink(), good], interval_s=60)
    with caplog.at_level("ERROR", logger="traceguard.audit.anchors"):
        assert sched.run_once() is None
    assert sched.failures == 1 and sched.anchors_stored == 0
    assert "disk on fire" in caplog.text
    assert good.latest() is not None  # cadence and the healthy sink are unaffected


@pytest.fixture
def file_engine(tmp_path: Path) -> Iterator[Engine]:
    # A file-backed DB: an in-memory SQLite is per-connection, and the
    # scheduler thread would see an empty database of its own.
    eng = make_engine(f"sqlite:///{tmp_path / 'sched.db'}", create_all=True)
    audit.enable(eng, backfill=False)
    yield eng
    audit.detach(eng)
    audit.set_strict(False)
    eng.dispose()


def test_scheduler_thread_anchors_on_interval(file_engine, tmp_path: Path) -> None:
    engine = file_engine
    sink = FileAnchorSink(tmp_path / "a.jsonl")
    sched = AnchorScheduler(engine, [sink], interval_s=0.01)
    sched.start()
    try:
        deadline = time.monotonic() + 5
        while sched.anchors_stored < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        sched.stop(timeout=5)
    assert sched.anchors_stored >= 3
    assert not sched.running
    assert len(list(sink.history())) == sched.anchors_stored


def test_scheduler_rejects_non_positive_interval(engine) -> None:
    with pytest.raises(ValueError):
        AnchorScheduler(engine, [], interval_s=0)


# ── CLI spec parsing ──────────────────────────────────────────────────────


def test_parse_sink_spec_variants(tmp_path: Path) -> None:
    f = parse_sink_spec(f"file:{tmp_path / 'x.jsonl'}")
    assert isinstance(f, FileAnchorSink) and f.path == tmp_path / "x.jsonl"
    g = parse_sink_spec("git-note")
    assert isinstance(g, GitNoteAnchorSink) and g.repo == Path(".")
    g2 = parse_sink_spec(f"git-note:{tmp_path}")
    assert g2.repo == tmp_path
    w = parse_sink_spec("webhook:https://example.invalid/hook?x=1")
    assert isinstance(w, WebhookAnchorSink) and w.url == "https://example.invalid/hook?x=1"


@pytest.mark.parametrize("spec", ["file:", "webhook:", "s3:bucket", "nonsense"])
def test_parse_sink_spec_rejects_bad_specs(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_sink_spec(spec)
