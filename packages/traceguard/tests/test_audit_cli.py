"""python -m traceguard.audit CLI (mirrors the routing_audit CLI test style)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy.orm import Session

from traceguard import audit
from traceguard.audit.__main__ import main
from traceguard.store.models import Trace, make_engine


@pytest.fixture
def db_url(tmp_path: Path) -> Iterator[str]:
    url = f"sqlite:///{tmp_path / 'cli.db'}"
    engine = make_engine(url, create_all=True)
    with Session(engine) as sess:
        sess.add(
            Trace(
                project="p", component="c", operation="parse", input_hash="h",
                parse_status="success", invoked_at=datetime.now(timezone.utc),
            )
        )
        sess.commit()
    yield url
    audit.set_strict(False)


def test_enable_verify_anchor_roundtrip(db_url: str, capsys) -> None:
    assert main(["--db", db_url, "enable"]) == 0
    assert "backfilled 1" in capsys.readouterr().out

    assert main(["--db", db_url, "verify"]) == 0
    assert "chain OK" in capsys.readouterr().out

    assert main(["--db", db_url, "anchor"]) == 0
    anchor_json = capsys.readouterr().out.strip()
    assert audit.ChainAnchor.from_json(anchor_json).seq == 1


def test_verify_exits_1_on_tamper_and_anchor_catches_truncation(db_url: str, capsys) -> None:
    assert main(["--db", db_url, "enable"]) == 0
    capsys.readouterr()
    assert main(["--db", db_url, "anchor"]) == 0
    anchor_json = capsys.readouterr().out.strip()

    engine = make_engine(db_url)
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE traces SET input_hash='forged' WHERE trace_id=1")
    assert main(["--db", db_url, "verify"]) == 1
    out = capsys.readouterr().out
    assert "hash_mismatch" in out and "[BREAK]" in out

    with engine.begin() as conn:  # truncate the whole chain + fix the trace back
        conn.exec_driver_sql("UPDATE traces SET input_hash='h' WHERE trace_id=1")
        conn.exec_driver_sql("DELETE FROM audit_chain_entries")
    assert main(["--db", db_url, "verify"]) == 0  # plain walk cannot see it
    capsys.readouterr()
    assert main(["--db", db_url, "verify", "--anchor", anchor_json]) == 1
    assert "anchor_mismatch" in capsys.readouterr().out


def test_enable_flags_and_disable(db_url: str, capsys) -> None:
    assert main(["--db", db_url, "enable", "--chain-only", "--no-backfill"]) == 0
    out = capsys.readouterr().out
    assert "append_only=False" in out and "backfilled 0" in out

    assert main(["--db", db_url, "disable"]) == 0
    assert "audit disabled" in capsys.readouterr().out
