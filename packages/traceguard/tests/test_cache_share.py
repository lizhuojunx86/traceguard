"""Tests for the shareable cache-audit export (``--emit-share`` / ``--show-share``).

The centrepiece is a POISON test. A synthetic store is filled with recognisable
sentinel strings in every place a real store carries something private — prompt
text, file paths, session ids, model names, project/component labels, error
messages, and a custom key nobody planned for — and the export is asserted to
contain not one of them anywhere in its JSON tree, keys included.

Two further tests keep that one honest, because a leak test that cannot fail is
worse than no leak test: one removes the model-id whitelist and asserts the
poison test then FAILS, and one injects a new field into the payload and asserts
the scanner finds it. Together they say the export is clean *and* that this file
would notice if it stopped being.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from traceguard.routing_audit import cache_share
from traceguard.routing_audit.cache_audit import (
    BENCHMARK_SINCE,
    BENCHMARK_UNTIL,
    CC_SOURCE,
    FINGERPRINT_ALGORITHM,
    audit,
    main as cache_audit_main,
    parse_bound,
)
from traceguard.routing_audit.cache_share import (
    NO_MODEL,
    SCHEMA_VERSION,
    UNRECOGNIZED,
    ShareWindowError,
    build_share,
    render_share,
)
from traceguard.store.models import Trace, make_engine

# One base token, so a single substring search covers every poisoned surface,
# with a per-surface suffix so a failure names the field that leaked.
SENTINEL = "SENTINEL_LEAK_7f3a"

T0 = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
WINDOW_SINCE = datetime(2026, 6, 1, tzinfo=timezone.utc)
WINDOW_UNTIL = datetime(2026, 8, 1, tzinfo=timezone.utc)

OPUS = "claude-opus-4-8"
FABLE = "claude-fable-5"

# Two token mixes, shaped like the real corpus: an ordinary turn re-reads a big
# cached prefix and writes very little, while the first turn after an expiry has
# to re-establish the whole prefix. Without that asymmetry every cap in the
# sweep loses money, the peak band is None, and the band assertions below would
# pass by never running.
_ORDINARY_USAGE = {
    "input_tokens": 100,
    "output_tokens": 500,
    "cache_read_input_tokens": 40_000,
    "cache_creation_input_tokens": 1_500,
    "cache_creation_5m": 1_500,
    "cache_creation_1h": 0,
    "speed": "standard",
}
_POST_GAP_USAGE = {
    "input_tokens": 100,
    "output_tokens": 500,
    "cache_read_input_tokens": 40_000,
    "cache_creation_input_tokens": 350_000,
    "cache_creation_5m": 0,
    "cache_creation_1h": 350_000,
    "speed": "standard",
}


def _poisoned_usage(base: dict[str, Any] = _ORDINARY_USAGE) -> dict[str, Any]:
    """A usage block carrying an unplanned-for extra key AND an extra value."""
    usage = dict(base)
    usage[f"{SENTINEL}_usage_key"] = f"{SENTINEL}_usage_value"
    return usage


# ── the poisoned store ──────────────────────────────────────────────────────


class PoisonStore:
    """A store whose every free-text surface carries the sentinel."""

    def __init__(self, path: Path) -> None:
        self.url = f"sqlite:///{path}"
        make_engine(self.url, create_all=True)
        self._n = 0

    def add(
        self,
        *,
        model: str | None,
        ts: datetime,
        session_id: str,
        usage: dict[str, Any] | None,
        source: str | None = CC_SOURCE,
    ) -> None:
        self._n += 1
        parsed: dict[str, Any] = {
            "session_id": session_id,
            # Every one of these is something a real ingest has put in
            # output_parsed at some point, or could tomorrow.
            "cwd": f"/Users/{SENTINEL}_path/projects/secret-client",
            "file": f"/Users/{SENTINEL}_path/.claude/projects/x/{SENTINEL}_file.jsonl",
            "prompt": f"rewrite the {SENTINEL}_prompt merger memo",
            "summary": f"{SENTINEL}_summary",
            "git_branch": f"feature/{SENTINEL}_branch",
            f"{SENTINEL}_custom_key": f"{SENTINEL}_custom_value",
        }
        if source is not None:
            parsed["source"] = source
        if usage is not None:
            parsed["usage"] = usage
        with Session(make_engine(self.url, create_all=False)) as sess:
            sess.add(
                Trace(
                    project=f"{SENTINEL}_project",
                    component=f"{SENTINEL}_component",
                    operation="llm_complete",
                    correlation_id=f"{SENTINEL}_correlation",
                    input_hash=f"{SENTINEL}_hash_{self._n:04d}",
                    input_summary=f"the user asked about {SENTINEL}_prompt_body",
                    model_id=model,
                    prompt_template_id=f"{SENTINEL}_template",
                    prompt_template_hash=f"{SENTINEL}_template_hash",
                    parse_status="success",
                    output_parsed=parsed,
                    tokens_in=41_000,
                    error_class=f"{SENTINEL}_error_class",
                    error_message=f"{SENTINEL}_error_message",
                    invoked_at=ts,
                )
            )
            sess.commit()

    def audit(self, **kwargs):
        kwargs.setdefault("since", WINDOW_SINCE)
        kwargs.setdefault("until", WINDOW_UNTIL)
        return audit(self.url, **kwargs)


@pytest.fixture
def poisoned(tmp_path: Path) -> PoisonStore:
    """A store with enough shape to populate every section of the export.

    Twelve sessions, each with a short gap and a >1h gap, so the buckets, the
    cap sweep and all ten gap-length deciles have something in them; models
    include two public ids, one private-looking id, and a NULL.
    """
    store = PoisonStore(tmp_path / "poison.db")
    models = [OPUS, FABLE, f"{SENTINEL}-internal-gateway-model", None]
    for s in range(12):
        session = f"{SENTINEL}_session_{s:02d}"
        base = T0 + timedelta(days=s)
        # in-TTL gap, then an expiring gap that grows with s (so deciles differ)
        store.add(model=models[s % 4], ts=base, session_id=session, usage=_poisoned_usage())
        store.add(
            model=models[s % 4],
            ts=base + timedelta(minutes=3),
            session_id=session,
            usage=_poisoned_usage(),
        )
        store.add(
            model=models[(s + 1) % 4],  # some gaps come back on a different model
            ts=base + timedelta(minutes=3) + timedelta(hours=2 + s),
            session_id=session,
            usage=_poisoned_usage(_POST_GAP_USAGE),
        )
    # a non-Claude-Code row, which section 4 reads and the export must not
    store.add(
        model=OPUS,
        ts=T0,
        session_id=f"{SENTINEL}_direct",
        usage=_poisoned_usage(),
        source=f"{SENTINEL}_source",
    )
    return store


# ── the scanner ─────────────────────────────────────────────────────────────


def find_sentinels(node: Any, path: str = "$") -> list[str]:
    """Every location in a JSON tree whose KEY or VALUE contains the sentinel.

    Recursive on purpose. A new field will land inside a list of dicts (that is
    where every repeated row in this schema lives), so a scanner that only
    checked the top level would pass while leaking.
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if SENTINEL in str(key):
                found.append(f"{path}.{key} <- leaked in a KEY")
            found.extend(find_sentinels(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found.extend(find_sentinels(value, f"{path}[{i}]"))
    elif isinstance(node, str) and SENTINEL in node:
        found.append(f"{path} = {node!r}")
    return found


# ── 1. the poison test ──────────────────────────────────────────────────────


def test_emit_share_leaks_no_sentinel_from_prompts_paths_sessions_or_model_names(
    poisoned,
):
    """No poisoned string reaches the share file, from any surface, anywhere.

    The store carries the sentinel in prompt text, file paths, session ids,
    model ids, project/component labels, correlation ids, template ids, error
    messages, and two keys nobody designed for. The export must contain none of
    them — not as a value, not as a key, not nested inside a row.
    """
    payload = build_share(poisoned.audit())

    leaks = find_sentinels(payload)
    assert leaks == [], "share export leaked private strings:\n  " + "\n  ".join(leaks)

    # Belt and braces: the rendered bytes are what actually gets sent, and a
    # substring search over them cannot be fooled by a scanner bug.
    assert SENTINEL not in render_share(payload)


def test_share_contains_only_constant_public_or_window_strings(poisoned):
    """The invariant behind the poison test, asserted directly.

    Every string in the payload is a module constant, a whitelisted model id, an
    ISO form of a declared window bound, or the installed version. Stated this
    way the check also covers sentinels nobody thought to plant.
    """
    payload = build_share(poisoned.audit())
    allowed = (
        {NO_MODEL, UNRECOGNIZED, cache_share.tool_version()}
        | set(cache_share.PUBLIC_MODEL_IDS)
        | {payload["window"]["since"], payload["window"]["until"]}
        | {"WORTH IT", "NOT WORTH IT", "UNDECIDED"}
        | set(_BUCKET_NAMES)
        | {payload["corpus"]["fingerprint"], FINGERPRINT_ALGORITHM}
    )
    strays = [
        s
        for s in _strings(payload)
        if s not in allowed and not _MONEY_LITERAL.fullmatch(s)
    ]
    assert strays == [], f"unexpected free-form strings in the export: {strays}"


_BUCKET_NAMES = ("<5m", "5m-1h", "1-4h", ">4h")
# Clause (e) of the invariant: money is serialised as a decimal literal so a
# JSON float never puts 2044.7199999999998 in a file meant to be diffed.
_MONEY_LITERAL = re.compile(r"-?\d+\.\d{2}")


def _strings(node: Any) -> list[str]:
    """Every string VALUE in the tree (keys are schema, and are asserted apart)."""
    if isinstance(node, dict):
        return [s for v in node.values() for s in _strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return [node] if isinstance(node, str) else []


# ── 2. the reverse tests: prove the poison test can fail ────────────────────


def test_dropping_the_model_whitelist_makes_the_poison_test_fail(poisoned, monkeypatch):
    """Remove the defence and the leak must reappear.

    Without this, the poison test would keep passing if the whitelist were
    replaced by a pass-through — it would just be asserting that this store
    happened to have clean model ids. It does not: one of them is poisoned.
    """
    monkeypatch.setattr(cache_share, "_model_key", lambda model_id: model_id)

    leaks = find_sentinels(build_share(poisoned.audit()))
    assert leaks, (
        "removing the model-id whitelist did NOT produce a leak, so the poison "
        "test is not actually testing the whitelist"
    )
    assert any("model_id" in leak for leak in leaks)


def test_the_sentinel_scan_catches_a_new_field_that_forgot_to_filter(poisoned):
    """A field added to the schema without filtering must be caught.

    Simulates the realistic regression: someone adds a per-model field next
    year, wires it straight to a DB column, and does not think about this file.
    The new field lands inside a list of dicts, so this also pins the scanner's
    recursion.
    """
    payload = build_share(poisoned.audit())
    assert find_sentinels(payload) == []  # clean before

    payload["models"][0]["operator_note"] = f"raw passthrough {SENTINEL}_new_field"

    leaks = find_sentinels(payload)
    assert leaks, "the scan missed a new unfiltered field nested in models[]"
    assert "operator_note" in leaks[0]


def test_the_sentinel_scan_catches_a_leak_in_a_key_not_just_a_value(poisoned):
    """Keys are scanned too — a dict keyed by something from the DB leaks."""
    payload = build_share(poisoned.audit())
    payload["keep_alive"][f"{SENTINEL}_keyed_by_db"] = 1

    leaks = find_sentinels(payload)
    assert leaks and "KEY" in leaks[0]


# ── 3. tool_version comes from installed metadata ───────────────────────────


def test_share_tool_version_matches_the_installed_distribution(poisoned):
    """tool_version is the version pip actually has, not a string in this repo."""
    payload = build_share(poisoned.audit())
    assert payload["tool_version"] == installed_version("traceguard")


def test_share_tool_version_follows_package_metadata_not_a_hardcoded_string(
    poisoned, monkeypatch
):
    """The test that would actually catch a regression to a literal.

    Equality with ``importlib.metadata.version`` passes trivially while the two
    happen to agree, which they do on any correctly installed checkout. Moving
    the metadata and requiring the payload to move with it does not.
    """
    monkeypatch.setattr(cache_share, "_dist_version", lambda name: "9.9.9-fromsomewhereelse")

    payload = build_share(poisoned.audit())
    assert payload["tool_version"] == "9.9.9-fromsomewhereelse"
    assert payload["tool_version"] != "1.3.0"


def test_share_tool_version_never_reads_the_hand_written_module_constant():
    """``traceguard.__version__`` is hand-written; the export must not use it."""
    source = Path(cache_share.__file__).read_text(encoding="utf-8")
    assert "__version__" not in source
    assert "_dist_version" in source


# ── 4. the window must be closed ────────────────────────────────────────────


@pytest.mark.parametrize(
    "since, until",
    [
        (None, None),          # all time
        (WINDOW_SINCE, None),  # open on the right
        (None, WINDOW_UNTIL),  # open on the left
    ],
)
def test_an_open_window_is_refused_because_it_is_not_comparable(poisoned, since, until):
    """Half a window is not a window; the export refuses rather than shipping it."""
    result = audit(poisoned.url, since=since, until=until)
    with pytest.raises(ShareWindowError) as exc:
        build_share(result)
    assert "open window" in str(exc.value)
    assert "--benchmark" in str(exc.value)


def test_a_closed_window_exports_and_records_both_bounds(poisoned):
    payload = build_share(poisoned.audit())
    assert payload["window"]["since"] == WINDOW_SINCE.isoformat()
    assert payload["window"]["until"] == WINDOW_UNTIL.isoformat()
    assert payload["window"]["days"] == pytest.approx(61.0)


def test_benchmark_window_satisfies_the_closed_window_rule(poisoned):
    """``--benchmark`` is closed by construction, so it always exports."""
    result = audit(
        poisoned.url,
        since=parse_bound(BENCHMARK_SINCE, flag="--since", end_of_day=False),
        until=parse_bound(BENCHMARK_UNTIL, flag="--until", end_of_day=True),
    )
    assert build_share(result)["window"]["since"].startswith(BENCHMARK_SINCE)


# ── 5. the schema carries what it promises ──────────────────────────────────


def test_share_reports_the_band_and_demotes_the_argmax(poisoned):
    """The citable field is the band; the argmax is named so quoting it reads wrong."""
    keep_alive = build_share(poisoned.audit())["keep_alive"]

    assert "recommended_cap_band" in keep_alive
    assert "argmax_reference_only" in keep_alive
    band = keep_alive["recommended_cap_band"]
    assert band is not None, (
        "this fixture is meant to make capping profitable; a None band means the "
        "band assertions below never run"
    )
    assert "argmax" not in band  # the citable field does not mention the argmax
    # the band is a span, not a point
    assert band["high_minutes"] >= band["low_minutes"]
    assert band["grid_points"] >= 1
    assert {"censored_low", "censored_high"} <= set(band)
    assert keep_alive["argmax_reference_only"]["verdict"] == "WORTH IT"


def test_share_reports_net_at_both_ends_never_a_single_number(poisoned):
    """Net is pessimistic AND measured — a single value would hide the range."""
    net = build_share(poisoned.audit())["keep_alive"]["argmax_reference_only"]["net_usd"]
    assert set(net) == {"measured", "pessimistic", "rewrite_lower_bound"}
    # pessimistic treats every undecidable gap as cross-model, so it can only be
    # the same or worse.
    assert float(net["pessimistic"]) <= float(net["measured"])


def test_share_deciles_carry_their_own_gap_length_bounds(poisoned):
    """A decile index means nothing across organisations without its bounds."""
    deciles = build_share(poisoned.audit())["cross_model"]["by_gap_length_decile"]
    assert deciles, "expired gaps existed but no decile was emitted"
    for entry in deciles:
        assert entry["gap_minutes_min"] <= entry["gap_minutes_max"]
        assert entry["switched"] + entry["same_model"] + entry["undecidable"] == entry["gaps"]
    assert [d["decile"] for d in deciles] == sorted(d["decile"] for d in deciles)
    # sorted by length, so bounds are non-decreasing across deciles
    highs = [d["gap_minutes_max"] for d in deciles]
    assert highs == sorted(highs)


def test_share_makes_an_undecidable_heavy_submission_obvious(poisoned):
    """The undecidable share is high in the file, not buried in a footnote."""
    payload = build_share(poisoned.audit())
    quality = payload["data_quality"]
    assert "undecidable_share" in quality
    assert 0.0 <= quality["undecidable_share"] <= 1.0
    # it appears before the money, so nobody reads a dollar figure out of a
    # submission without having passed its weakness first
    keys = list(payload)
    assert keys.index("data_quality") < keys.index("models")
    assert keys.index("data_quality") < keys.index("keep_alive")


def test_share_folds_unrecognized_models_into_one_row_keeping_the_counts(poisoned):
    """Names are dropped; tokens and messages are not."""
    models = build_share(poisoned.audit())["models"]
    by_id = {m["model_id"]: m for m in models}

    assert UNRECOGNIZED in by_id, "the poisoned model id was not folded in"
    folded = by_id[UNRECOGNIZED]
    assert folded["messages"] > 0
    assert folded["prompt_tokens"] > 0
    assert folded["distinct_model_ids"] >= 1
    assert OPUS in by_id  # public ids keep their names
    assert all(SENTINEL not in mid for mid in by_id)


def test_share_buckets_inside_the_ttl_carry_null_money_not_zero(poisoned):
    """A bucket that expires nothing has no cost — null, not a misleading 0.00."""
    buckets = {b["bucket"]: b for b in build_share(poisoned.audit())["gap_buckets"]}
    assert buckets["<5m"]["expires"] is False
    assert buckets["<5m"]["rewrite_usd_upper"] is None
    assert buckets["<5m"]["ping_usd"] is None
    assert buckets["<5m"]["switch_rate"] is None
    assert buckets[">4h"]["expires"] is True


def test_share_money_is_a_two_decimal_string_never_a_float(poisoned):
    """Floats put 2044.7199999999998 in a file meant to be diffed for years."""
    payload = build_share(poisoned.audit())

    def check(node: Any, path: str = "$") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.endswith("_usd") and value is not None and not isinstance(
                    value, (dict, list)
                ):
                    assert isinstance(value, str), f"{path}.{key} is not a string"
                    assert value.count(".") == 1 and len(value.split(".")[1]) == 2, (
                        f"{path}.{key} = {value!r} is not 2dp"
                    )
                check(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                check(value, f"{path}[{i}]")

    check(payload)
    assert payload["keep_alive"]["argmax_reference_only"]["ping_usd"] is not None


def test_share_never_carries_the_db_url_or_any_path(poisoned):
    """The store's own path is a path like any other and stays out."""
    blob = render_share(build_share(poisoned.audit()))
    assert poisoned.url not in blob
    assert ".db" not in blob
    assert "sqlite" not in blob


# ── 5b. the corpus fingerprint ──────────────────────────────────────────────
#
# A closed window is necessary and not sufficient: the store grows inside a
# window that never moves, because ingest keeps finding transcript files whose
# messages are timestamped inside it. These pin the property that lets two
# submissions be told apart anyway.


def _fingerprint(store, **kwargs) -> str:
    return build_share(store.audit(**kwargs))["corpus"]["fingerprint"]


def test_fingerprint_is_stable_across_two_runs_of_the_same_corpus(poisoned):
    """Same store, same window, twice — byte-identical digest or it is useless."""
    assert _fingerprint(poisoned) == _fingerprint(poisoned)


def test_fingerprint_is_a_sha256_hex_digest(poisoned):
    payload = build_share(poisoned.audit())
    digest = payload["corpus"]["fingerprint"]
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    assert payload["corpus"]["fingerprint_algorithm"] == FINGERPRINT_ALGORITHM


def test_fingerprint_changes_when_a_trace_lands_inside_the_window(poisoned):
    """The whole point: a window that did not move cannot vouch for the corpus.

    This is the case that produced the flaw — 432 expired gaps over 168
    sessions became 439 over 174 in under a day on an unchanged --benchmark
    window, because ingest found transcripts it had not seen before.
    """
    before = _fingerprint(poisoned)
    poisoned.add(
        model=OPUS,
        ts=T0 + timedelta(days=3, hours=7),  # comfortably inside the window
        session_id=f"{SENTINEL}_late_arrival",
        usage=_poisoned_usage(),
    )
    after = _fingerprint(poisoned)

    assert after != before, (
        "a trace appeared inside the frozen window and the fingerprint did not "
        "move — it cannot distinguish two corpora, which is its only job"
    )


def test_fingerprint_ignores_a_trace_outside_the_window(poisoned):
    """It identifies what the window loaded, not what the store happens to hold."""
    before = _fingerprint(poisoned)
    poisoned.add(
        model=OPUS,
        ts=WINDOW_UNTIL + timedelta(days=9),
        session_id=f"{SENTINEL}_out_of_window",
        usage=_poisoned_usage(),
    )
    assert _fingerprint(poisoned) == before


def test_fingerprint_covers_traffic_no_section_scores(poisoned):
    """traces_in_window counts direct rows, so the fingerprint must see them too.

    A digest over only the analysed subset would call two corpora identical
    while a field in the same file disagreed.
    """
    before = _fingerprint(poisoned)
    poisoned.add(
        model=OPUS,
        ts=T0 + timedelta(days=4, hours=5),
        session_id=f"{SENTINEL}_direct_extra",
        usage=_poisoned_usage(),
        source=f"{SENTINEL}_some_other_harness",
    )
    assert _fingerprint(poisoned) != before


def test_fingerprint_leaks_no_session_id(poisoned):
    """Session ids go in; nothing recoverable comes out."""
    payload = build_share(poisoned.audit())
    assert find_sentinels(payload) == []
    assert SENTINEL not in payload["corpus"]["fingerprint"]


def test_fingerprint_does_not_depend_on_row_order(poisoned):
    """Sorted before hashing, so a different scan order is not a different corpus."""
    from traceguard.routing_audit.cache_audit import corpus_fingerprint, load_records

    records = load_records(poisoned.url, since=WINDOW_SINCE, until=WINDOW_UNTIL)
    assert corpus_fingerprint(records) == corpus_fingerprint(list(reversed(records)))


def test_schema_version_is_pinned(poisoned):
    assert build_share(poisoned.audit())["schema_version"] == SCHEMA_VERSION == 1


# ── 6. the CLI ──────────────────────────────────────────────────────────────


def test_show_share_prints_exactly_what_emit_share_would_write(poisoned, tmp_path, capsys):
    """The preview is the file. Anything less and reading it proves nothing."""
    out_path = tmp_path / "share.json"
    argv = [
        "--db", poisoned.url,
        "--since", "2026-06-01",
        "--until", "2026-08-01",
    ]

    assert cache_audit_main(argv + ["--emit-share", str(out_path)]) == 0
    capsys.readouterr()
    assert cache_audit_main(argv + ["--show-share"]) == 0
    shown = capsys.readouterr().out

    assert shown == out_path.read_text(encoding="utf-8")
    assert json.loads(shown)["schema_version"] == SCHEMA_VERSION


def test_show_share_replaces_the_report_and_emit_share_leaves_it_alone(
    poisoned, tmp_path, capsys
):
    """Sections 1-4 and 3b are untouched: --emit-share adds a file, not a column."""
    argv = ["--db", poisoned.url, "--since", "2026-06-01", "--until", "2026-08-01"]

    assert cache_audit_main(argv) == 0
    plain = capsys.readouterr().out

    assert cache_audit_main(argv + ["--emit-share", str(tmp_path / "s.json")]) == 0
    with_emit = capsys.readouterr()
    assert with_emit.out == plain, "--emit-share changed the report on stdout"
    assert "wrote" in with_emit.err  # the confirmation goes to stderr

    assert cache_audit_main(argv + ["--show-share"]) == 0
    assert "cache-efficiency audit" not in capsys.readouterr().out


def test_cli_refuses_an_open_window_with_a_reason_not_a_traceback(poisoned, capsys):
    """Exit 2 and an explanation of why an open window cannot be compared."""
    assert cache_audit_main(["--db", poisoned.url, "--show-share"]) == 2
    err = capsys.readouterr().err
    assert "open window" in err
    assert "--benchmark" in err


def test_emit_share_writes_a_trailing_newline(poisoned, tmp_path):
    """It is a text file people will cat, diff and commit."""
    out_path = tmp_path / "share.json"
    cache_audit_main(
        [
            "--db", poisoned.url,
            "--since", "2026-06-01",
            "--until", "2026-08-01",
            "--emit-share", str(out_path),
        ]
    )
    assert out_path.read_text(encoding="utf-8").endswith("}\n")


def test_share_export_is_read_only(poisoned, tmp_path):
    """The audit opens mode=ro; exporting must not have changed that."""
    before = Path(poisoned.url.replace("sqlite:///", "")).stat().st_mtime_ns
    cache_audit_main(
        [
            "--db", poisoned.url,
            "--since", "2026-06-01",
            "--until", "2026-08-01",
            "--emit-share", str(tmp_path / "s.json"),
        ]
    )
    assert Path(poisoned.url.replace("sqlite:///", "")).stat().st_mtime_ns == before
