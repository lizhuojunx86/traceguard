"""python -m traceguard.audit: the v2 subcommands (anchor sinks, --anchor-file, reconcile)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy.orm import Session

from traceguard import audit
from traceguard.audit.__main__ import main
from traceguard.audit.reconcile import ADMIN_KEY_ENV
from traceguard.store.models import Trace, make_engine

UTC = timezone.utc


@pytest.fixture
def db_url(tmp_path: Path) -> Iterator[str]:
    url = f"sqlite:///{tmp_path / 'cli.db'}"
    engine = make_engine(url, create_all=True)
    with Session(engine) as sess:
        sess.add(
            Trace(
                project="p",
                component="c",
                operation="llm_complete",
                input_hash="h",
                parse_status="success",
                model_id="m",
                tokens_in=1000,
                tokens_out=100,
                invoked_at=datetime(2026, 8, 1, 12, tzinfo=UTC),
            )
        )
        sess.commit()
    engine.dispose()
    yield url
    audit.set_strict(False)


def _usage_file(tmp_path: Path, tokens_in: int, tokens_out: int, model: str = "m") -> Path:
    page = {
        "data": [
            {
                "starting_at": "2026-08-01T00:00:00Z",
                "ending_at": "2026-08-02T00:00:00Z",
                "results": [
                    {
                        "model": model,
                        "uncached_input_tokens": tokens_in,
                        "output_tokens": tokens_out,
                    }
                ],
            }
        ],
        "has_more": False,
        "next_page": None,
    }
    path = tmp_path / "usage.json"
    path.write_text(json.dumps(page))
    return path


# ── anchor --sink / verify --anchor-file ──────────────────────────────────


def test_anchor_to_file_sink_then_verify_against_it(db_url: str, tmp_path: Path, capsys) -> None:
    anchors = tmp_path / "anchors.jsonl"
    assert main(["--db", db_url, "enable"]) == 0
    capsys.readouterr()
    assert main(["--db", db_url, "anchor", "--sink", f"file:{anchors}"]) == 0
    out, err = capsys.readouterr()
    assert audit.ChainAnchor.from_json(out.strip()).seq == 1
    assert "stored to 1 sink(s)" in err
    assert len(anchors.read_text().splitlines()) == 1

    assert main(["--db", db_url, "verify", "--anchor-file", str(anchors)]) == 0
    capsys.readouterr()

    engine = make_engine(db_url)  # truncate the chain: only the anchor can see it
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM audit_chain_entries")
    assert main(["--db", db_url, "verify"]) == 0
    capsys.readouterr()
    assert main(["--db", db_url, "verify", "--anchor-file", str(anchors)]) == 1
    assert "anchor_mismatch" in capsys.readouterr().out


def test_verify_anchor_file_without_anchors_is_usage_error(
    db_url: str, tmp_path: Path, capsys
) -> None:
    assert main(["--db", db_url, "verify", "--anchor-file", str(tmp_path / "none.jsonl")]) == 2
    assert "no anchor found" in capsys.readouterr().err


def test_anchor_bad_sink_spec_is_usage_error(db_url: str, capsys) -> None:
    assert main(["--db", db_url, "anchor", "--sink", "s3:bucket"]) == 2
    assert "unknown sink" in capsys.readouterr().err


def test_anchor_every_with_rounds(db_url: str, tmp_path: Path, capsys) -> None:
    anchors = tmp_path / "anchors.jsonl"
    assert main(["--db", db_url, "enable"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--db",
                db_url,
                "anchor",
                "--sink",
                f"file:{anchors}",
                "--every",
                "0.01",
                "--rounds",
                "3",
            ]
        )
        == 0
    )
    out, err = capsys.readouterr()
    assert len(out.strip().splitlines()) == 3
    assert len(anchors.read_text().splitlines()) == 3
    assert "anchored 3 time(s)" in err


def test_anchor_every_reports_sink_failures_with_exit_1(
    db_url: str, tmp_path: Path, capsys
) -> None:
    unwritable = tmp_path / "dir-not-file"
    unwritable.mkdir()
    assert (
        main(
            [
                "--db",
                db_url,
                "anchor",
                "--sink",
                f"file:{unwritable}",
                "--every",
                "0.01",
                "--rounds",
                "1",
            ]
        )
        == 1
    )
    assert "1 failure(s)" in capsys.readouterr().err


def test_anchor_without_sinks_just_prints(db_url: str, capsys) -> None:
    assert main(["--db", db_url, "enable", "--no-backfill"]) == 0
    capsys.readouterr()
    assert main(["--db", db_url, "anchor"]) == 0
    out, err = capsys.readouterr()
    assert audit.ChainAnchor.from_json(out.strip()).seq == 0  # empty chain anchors at genesis
    assert "stored to" not in err


# ── reconcile ─────────────────────────────────────────────────────────────

WINDOW = "2026-08-01T00:00:00Z,2026-08-02T00:00:00Z"


def test_reconcile_json_source_clean(db_url: str, tmp_path: Path, capsys) -> None:
    usage = _usage_file(tmp_path, 1000, 100)
    assert main(["--db", db_url, "reconcile", "--source", f"json:{usage}", "--window", WINDOW]) == 0
    out = capsys.readouterr().out
    assert "reconcile OK" in out and "m: calls=1 tokens_in traces=1000 provider=1000" in out


def test_reconcile_json_source_mismatch_exits_1(db_url: str, tmp_path: Path, capsys) -> None:
    usage = _usage_file(tmp_path, 2000, 100)
    assert main(["--db", db_url, "reconcile", "--source", f"json:{usage}", "--window", WINDOW]) == 1
    out = capsys.readouterr().out
    assert "CAPTURE MISMATCH" in out and "[WARN] capture_mismatch" in out


def test_reconcile_model_map_and_tolerance_flags(db_url: str, tmp_path: Path, capsys) -> None:
    usage = _usage_file(tmp_path, 1100, 100, model="provider-name")
    args = ["--db", db_url, "reconcile", "--source", f"json:{usage}", "--window", WINDOW]
    assert main(args + ["--model-map", "m=provider-name", "--tolerance", "0.2"]) == 0
    capsys.readouterr()
    assert main(args + ["--model-map", "m=provider-name"]) == 1  # 10% > default 5%
    capsys.readouterr()
    assert main(args + ["--model-map", "broken"]) == 2
    assert "TRACE=PROVIDER" in capsys.readouterr().err


def test_reconcile_unaligned_window_is_snapped(db_url: str, tmp_path: Path, capsys) -> None:
    usage = _usage_file(tmp_path, 1000, 100)
    window = "2026-08-01T06:30:00Z,2026-08-01T18:00:00Z"  # trace at 12:00Z; day bucket
    assert main(["--db", db_url, "reconcile", "--source", f"json:{usage}", "--window", window]) == 0
    assert "2026-08-01T00:00:00+00:00 → 2026-08-02T00:00:00+00:00" in capsys.readouterr().out


def test_reconcile_bad_source_and_window_are_usage_errors(db_url: str, capsys) -> None:
    assert main(["--db", db_url, "reconcile", "--source", "nope", "--window", WINDOW]) == 2
    assert "unknown --source" in capsys.readouterr().err
    assert main(["--db", db_url, "reconcile", "--source", "json:x", "--window", "garbage"]) == 2
    assert "START,END" in capsys.readouterr().err


def test_reconcile_anthropic_source_needs_admin_key(db_url: str, monkeypatch, capsys) -> None:
    monkeypatch.delenv(ADMIN_KEY_ENV, raising=False)
    assert (
        main(["--db", db_url, "reconcile", "--source", "anthropic-usage", "--window", WINDOW]) == 2
    )
    assert ADMIN_KEY_ENV in capsys.readouterr().err
