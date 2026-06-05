"""Tests for E3 audit-flag semantics.

A "suspicion" flag means the data is flagged for human review but is not
declared wrong, so it must be quarantined from pass-rate / avg-score while
still being counted (suspicion_count). Also covers the forward-only
``ensure_schema`` migration that adds ``flag_type`` to legacy databases.
"""
import sqlalchemy as sa

from guardian.store.models import ensure_schema
from guardian.store.reader import TraceReader
from guardian.store.writer import TraceWriter


def test_suspicion_excluded_from_pass_rate(tmp_path):
    db = f"sqlite:///{tmp_path}/t.db"
    w = TraceWriter(db)
    for _ in range(9):
        w.write("p", "s", action="pass", passed=True, score=1.0, issues=[], attempt=1)
    # One advisory suspicion: passed=False, but the data is not an error.
    w.write(
        "p",
        "s",
        action="alert",
        passed=False,
        score=0.8,
        issues=["reverse-calc suspicion"],
        attempt=1,
        flag_type="suspicion",
    )

    stats = TraceReader(db).get_step_stats("p", "s", days=3650)
    assert stats["total"] == 10
    assert stats["suspicion_count"] == 1
    # pass_rate is over standard traces only: 9/9 == 1.0, NOT 9/10.
    assert stats["pass_rate"] == 1.0


def test_hard_fail_still_counts_against_pass_rate(tmp_path):
    db = f"sqlite:///{tmp_path}/t.db"
    w = TraceWriter(db)
    for _ in range(8):
        w.write("p", "s", action="pass", passed=True, score=1.0, issues=[], attempt=1)
    for _ in range(2):
        # Standard hard failures DO depress pass_rate.
        w.write("p", "s", action="abort", passed=False, score=0.2, issues=["bad"], attempt=1)

    stats = TraceReader(db).get_step_stats("p", "s", days=3650)
    assert stats["total"] == 10
    assert stats["suspicion_count"] == 0
    assert stats["pass_rate"] == 0.8  # 8/10 standard


def test_all_suspicion_yields_null_pass_rate(tmp_path):
    db = f"sqlite:///{tmp_path}/t.db"
    w = TraceWriter(db)
    for _ in range(3):
        w.write(
            "p", "s", action="alert", passed=False, score=0.8,
            issues=["s"], attempt=1, flag_type="suspicion",
        )
    stats = TraceReader(db).get_step_stats("p", "s", days=3650)
    assert stats["total"] == 3
    assert stats["suspicion_count"] == 3
    assert stats["pass_rate"] is None  # no standard traces to rate


def test_flag_type_in_trace_dict_and_default(tmp_path):
    db = f"sqlite:///{tmp_path}/t.db"
    w = TraceWriter(db)
    w.write("p", "s", action="pass", passed=True, score=1.0, issues=[], attempt=1)
    w.write(
        "p", "s", action="alert", passed=False, score=0.8,
        issues=[], attempt=1, flag_type="suspicion",
    )
    rows = TraceReader(db).query_traces(pipeline_name="p", step_name="s", days=3650)
    flags = sorted(r["flag_type"] for r in rows)
    assert flags == ["standard", "suspicion"]  # default is "standard"


def test_ensure_schema_adds_missing_column_idempotently(tmp_path):
    # Simulate a legacy DB whose eval_traces table predates flag_type.
    db = f"sqlite:///{tmp_path}/legacy.db"
    engine = sa.create_engine(db)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE eval_traces ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " pipeline_name VARCHAR(255) NOT NULL,"
                " step_name VARCHAR(255) NOT NULL,"
                " action VARCHAR(50) NOT NULL,"
                " passed BOOLEAN NOT NULL,"
                " score FLOAT NOT NULL,"
                " issues TEXT NOT NULL DEFAULT '[]',"
                " attempt INTEGER NOT NULL DEFAULT 1,"
                " output_preview TEXT,"
                " created_at DATETIME NOT NULL)"
            )
        )

    assert "flag_type" not in {
        c["name"] for c in sa.inspect(engine).get_columns("eval_traces")
    }

    ensure_schema(engine)
    ensure_schema(engine)  # second call must be a no-op

    assert "flag_type" in {
        c["name"] for c in sa.inspect(engine).get_columns("eval_traces")
    }

    # New writes still work and default to "standard".
    TraceWriter(db).write(
        "p", "s", action="pass", passed=True, score=1.0, issues=[], attempt=1
    )
    rows = TraceReader(db).query_traces(pipeline_name="p", step_name="s", days=3650)
    assert rows and all(r["flag_type"] == "standard" for r in rows)


def test_ensure_schema_noop_when_table_absent(tmp_path):
    # No eval_traces table yet -> ensure_schema must not raise.
    engine = sa.create_engine(f"sqlite:///{tmp_path}/empty.db")
    ensure_schema(engine)  # should simply return
