"""Out-of-band reconciliation (audit v2, L1): provider usage vs self-reported traces.

The comparisons are deliberately boring arithmetic; what these tests pin is
the CONVENTIONS — the tokens_in sum, UTC bucket alignment, window filtering,
direction wording — because a convention mismatch is a false positive that
would train people to ignore capture_mismatch.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy.orm import Session

from traceguard.audit.reconcile import (
    CAPTURE_MISMATCH,
    UsageBucket,
    align_window,
    fetch_anthropic_usage,
    load_usage_report,
    parse_window,
    reconcile,
    traces_usage,
    usage_from_report,
)
from traceguard.store.models import Trace

UTC = timezone.utc
START = datetime(2026, 8, 1, tzinfo=UTC)
END = datetime(2026, 8, 3, tzinfo=UTC)
DAY = timedelta(days=1)


def _add(engine, *, model, tokens_in, tokens_out, at=None, project="p", operation="llm_complete"):
    with Session(engine) as sess:
        sess.add(
            Trace(
                project=project,
                component="c",
                operation=operation,
                input_hash="h" * 64,
                parse_status="success",
                model_id=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                invoked_at=at or (START + timedelta(hours=6)),
            )
        )
        sess.commit()


def _bucket(model, tokens_in, tokens_out, start=START, width=DAY):
    return UsageBucket(
        starting_at=start,
        ending_at=start + width,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


def _kinds(result):
    return [f.kind for f in result.findings]


# ── conventions ───────────────────────────────────────────────────────────


def test_align_window_snaps_outward_to_utc_bucket_edges() -> None:
    s = datetime(2026, 8, 1, 13, 45, 10, tzinfo=timezone(timedelta(hours=8)))  # 05:45:10Z
    e = datetime(2026, 8, 2, 0, 0, 1, tzinfo=UTC)
    assert align_window(s, e, "1d") == (
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 3, tzinfo=UTC),
    )
    assert align_window(s, e, "1h") == (
        datetime(2026, 8, 1, 5, tzinfo=UTC),
        datetime(2026, 8, 2, 1, tzinfo=UTC),
    )
    assert align_window(s, e, "1m") == (
        datetime(2026, 8, 1, 5, 45, tzinfo=UTC),
        datetime(2026, 8, 2, 0, 1, tzinfo=UTC),
    )
    # already aligned: unchanged
    assert align_window(START, END, "1d") == (START, END)


def test_align_window_rejects_naive_and_bad_width() -> None:
    with pytest.raises(ValueError):
        align_window(datetime(2026, 8, 1), END, "1d")
    with pytest.raises(ValueError):
        align_window(START, END, "1w")


def test_parse_window() -> None:
    assert parse_window("2026-08-01T00:00:00Z, 2026-08-03T00:00:00+00:00") == (START, END)
    with pytest.raises(ValueError):
        parse_window("2026-08-03T00:00:00Z,2026-08-01T00:00:00Z")
    with pytest.raises(ValueError):
        parse_window("2026-08-01")


def test_usage_from_report_sums_all_input_kinds_like_wrap_anthropic() -> None:
    page = {
        "data": [
            {
                "starting_at": "2026-08-01T00:00:00Z",
                "ending_at": "2026-08-02T00:00:00Z",
                "results": [
                    {
                        "model": "claude-opus-5",
                        "uncached_input_tokens": 1500,
                        "cache_read_input_tokens": 200,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 500,
                            "ephemeral_1h_input_tokens": 1000,
                        },
                        "output_tokens": 500,
                        "server_tool_use": {"web_search_requests": 10},
                    },
                    {"model": "claude-sonnet-5", "uncached_input_tokens": 7, "output_tokens": 3},
                ],
            },
            {
                "starting_at": "2026-08-02T00:00:00Z",
                "ending_at": "2026-08-03T00:00:00Z",
                "results": [],
            },
        ],
        "has_more": False,
        "next_page": None,
    }
    buckets = usage_from_report([page])
    assert buckets == [
        _bucket("claude-opus-5", 1500 + 200 + 500 + 1000, 500),
        _bucket("claude-sonnet-5", 7, 3),
    ]


def test_load_usage_report_accepts_page_list_and_jsonl(tmp_path: Path) -> None:
    page = {
        "data": [
            {
                "starting_at": "2026-08-01T00:00:00Z",
                "ending_at": "2026-08-02T00:00:00Z",
                "results": [{"model": "m", "uncached_input_tokens": 1, "output_tokens": 2}],
            }
        ]
    }
    single = tmp_path / "single.json"
    single.write_text(json.dumps(page))
    as_list = tmp_path / "list.json"
    as_list.write_text(json.dumps([page, page]))
    jsonl = tmp_path / "pages.jsonl"
    jsonl.write_text(json.dumps(page) + "\n" + json.dumps(page) + "\n")
    assert load_usage_report(single) == [_bucket("m", 1, 2)]
    assert load_usage_report(as_list) == [_bucket("m", 1, 2)] * 2
    assert load_usage_report(jsonl) == [_bucket("m", 1, 2)] * 2
    empty = tmp_path / "empty.json"
    empty.write_text("")
    assert load_usage_report(empty) == []


# ── reconcile ─────────────────────────────────────────────────────────────


def test_matching_totals_are_ok(engine) -> None:
    _add(engine, model="claude-opus-5", tokens_in=1000, tokens_out=100)
    _add(
        engine,
        model="claude-opus-5",
        tokens_in=1000,
        tokens_out=100,
        at=START + DAY + timedelta(hours=1),
    )
    provider = [
        _bucket("claude-opus-5", 1000, 100),
        _bucket("claude-opus-5", 1000, 100, start=START + DAY),
    ]
    result = reconcile(engine, starting_at=START, ending_at=END, provider=provider)
    assert result.ok and result.findings == []
    cmp = result.comparisons["claude-opus-5"]
    assert (cmp.traces.calls, cmp.traces.tokens_in, cmp.provider.tokens_in) == (2, 2000, 2000)
    assert result.total.traces.tokens_out == result.total.provider.tokens_out == 200
    assert "OK" in result.summary()


def test_provider_exceeding_traces_names_uninstrumented_traffic(engine) -> None:
    _add(engine, model="m", tokens_in=1000, tokens_out=100)
    result = reconcile(engine, starting_at=START, ending_at=END, provider=[_bucket("m", 1500, 100)])
    assert not result.ok
    assert _kinds(result) == [
        CAPTURE_MISMATCH,
        CAPTURE_MISMATCH,
    ]  # per-model + total, tokens_in only
    detail = result.findings[0].detail
    assert "model m tokens_in: traces=1000 provider=1500" in detail
    assert "provider exceeds traces" in detail and "never recorded" in detail
    assert all(f.severity == "WARN" for f in result.findings)
    assert "CAPTURE MISMATCH" in result.summary()


def test_traces_exceeding_provider_names_spoofed_self_reports(engine) -> None:
    _add(engine, model="m", tokens_in=1000, tokens_out=300)
    result = reconcile(engine, starting_at=START, ending_at=END, provider=[_bucket("m", 1000, 100)])
    assert not result.ok
    detail = result.findings[0].detail
    assert "tokens_out: traces=300 provider=100" in detail
    assert "traces exceed provider" in detail and "never served" in detail


def test_within_tolerance_is_ok(engine) -> None:
    _add(engine, model="m", tokens_in=1020, tokens_out=100)  # +2% vs provider
    result = reconcile(engine, starting_at=START, ending_at=END, provider=[_bucket("m", 1000, 100)])
    assert result.ok
    result = reconcile(
        engine, starting_at=START, ending_at=END, provider=[_bucket("m", 1000, 100)], tolerance=0.01
    )
    assert not result.ok


def test_absolute_floor_suppresses_small_differences(engine) -> None:
    _add(engine, model="m", tokens_in=12, tokens_out=1)  # tiny numbers, 20% off
    provider = [_bucket("m", 10, 1)]
    assert not reconcile(engine, starting_at=START, ending_at=END, provider=provider).ok
    assert reconcile(
        engine, starting_at=START, ending_at=END, provider=provider, absolute_floor=5
    ).ok


def test_model_only_in_traces_is_a_mismatch(engine) -> None:
    _add(engine, model="ghost", tokens_in=50, tokens_out=5)
    _add(engine, model="m", tokens_in=100, tokens_out=10)
    result = reconcile(
        engine, starting_at=START, ending_at=END, provider=[_bucket("m", 100, 10)], tolerance=0.5
    )
    ghost = [f for f in result.findings if "model ghost" in f.detail]
    assert ghost and "NO usage" in ghost[0].detail and "1 trace(s)" in ghost[0].detail


def test_model_only_in_provider_is_a_mismatch(engine) -> None:
    _add(engine, model="m", tokens_in=100, tokens_out=10)
    provider = [_bucket("m", 100, 10), _bucket("other", 40, 4)]
    result = reconcile(engine, starting_at=START, ending_at=END, provider=provider, tolerance=0.5)
    other = [f for f in result.findings if "model other" in f.detail]
    assert other and "NO rows" in other[0].detail


def test_model_map_reconciles_naming_differences(engine) -> None:
    _add(engine, model="claude-opus-5-20260101", tokens_in=100, tokens_out=10)
    provider = [_bucket("claude-opus-5", 100, 10)]
    assert not reconcile(engine, starting_at=START, ending_at=END, provider=provider).ok
    mapped = reconcile(
        engine,
        starting_at=START,
        ending_at=END,
        provider=provider,
        model_map={"claude-opus-5-20260101": "claude-opus-5"},
    )
    assert mapped.ok and set(mapped.comparisons) == {"claude-opus-5"}


def test_buckets_outside_window_are_ignored_and_counted(engine) -> None:
    _add(engine, model="m", tokens_in=100, tokens_out=10)
    provider = [
        _bucket("m", 100, 10),
        _bucket("m", 999, 99, start=START - DAY),
        _bucket("m", 999, 99, start=END),
    ]
    result = reconcile(engine, starting_at=START, ending_at=END, provider=provider)
    assert result.ok and result.buckets_outside_window == 2


def test_traces_outside_window_project_and_operation_are_excluded(engine) -> None:
    _add(engine, model="m", tokens_in=100, tokens_out=10)
    _add(engine, model="m", tokens_in=500, tokens_out=50, at=END)  # exclusive end
    _add(engine, model="m", tokens_in=500, tokens_out=50, project="other")
    _add(engine, model="m", tokens_in=500, tokens_out=50, operation="embedding")
    usage = traces_usage(engine, START, END, project="p")
    assert usage["m"].calls == 1 and usage["m"].tokens_in == 100
    result = reconcile(
        engine, starting_at=START, ending_at=END, provider=[_bucket("m", 100, 10)], project="p"
    )
    assert result.ok


def test_traces_without_model_id_only_count_toward_totals(engine) -> None:
    _add(engine, model=None, tokens_in=100, tokens_out=10)
    result = reconcile(
        engine, starting_at=START, ending_at=END, provider=[_bucket("m", 100, 10)], tolerance=0.5
    )
    assert None in result.comparisons
    assert result.total.traces.tokens_in == result.total.provider.tokens_in == 100
    # the provider's model has no traces -> that mismatch is still reported
    assert any("model m" in f.detail and "NO rows" in f.detail for f in result.findings)


def test_reconcile_validates_window(engine) -> None:
    with pytest.raises(ValueError):
        reconcile(engine, starting_at=END, ending_at=START, provider=[])
    with pytest.raises(ValueError):
        reconcile(engine, starting_at=datetime(2026, 8, 1), ending_at=END, provider=[])


# ── fetch_anthropic_usage (injected opener, no network) ───────────────────


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


def test_fetch_anthropic_usage_paginates_and_filters() -> None:
    calls = []
    pages = [
        {
            "data": [
                {
                    "starting_at": "2026-08-01T00:00:00Z",
                    "ending_at": "2026-08-02T00:00:00Z",
                    "results": [{"model": "m", "uncached_input_tokens": 10, "output_tokens": 1}],
                }
            ],
            "has_more": True,
            "next_page": "page_2",
        },
        {
            "data": [
                {
                    "starting_at": "2026-08-02T00:00:00Z",
                    "ending_at": "2026-08-03T00:00:00Z",
                    "results": [{"model": "m", "uncached_input_tokens": 20, "output_tokens": 2}],
                }
            ],
            "has_more": False,
            "next_page": None,
        },
    ]

    def opener(req, timeout):
        calls.append(req)
        return _Resp(pages[len(calls) - 1])

    buckets = fetch_anthropic_usage(
        datetime(2026, 8, 1, 5, tzinfo=UTC),
        datetime(2026, 8, 2, 1, tzinfo=UTC),  # unaligned on purpose
        admin_key="sk-ant-admin-test",
        api_key_ids=["apikey_1", "apikey_2"],
        workspace_ids=["wrkspc_1"],
        models=["m"],
        opener=opener,
    )
    assert buckets == [_bucket("m", 10, 1), _bucket("m", 20, 2, start=START + DAY)]
    assert len(calls) == 2
    first = urlparse(calls[0].full_url)
    assert first.path == "/v1/organizations/usage_report/messages"
    q = parse_qs(first.query)
    assert q["starting_at"] == ["2026-08-01T00:00:00Z"] and q["ending_at"] == [
        "2026-08-03T00:00:00Z"
    ]
    assert q["bucket_width"] == ["1d"] and q["group_by[]"] == ["model"] and q["limit"] == ["31"]
    assert q["api_key_ids[]"] == ["apikey_1", "apikey_2"] and q["workspace_ids[]"] == ["wrkspc_1"]
    assert q["models[]"] == ["m"] and "page" not in q
    assert calls[0].get_header("X-api-key") == "sk-ant-admin-test"
    assert calls[0].get_header("Anthropic-version") == "2023-06-01"
    assert parse_qs(urlparse(calls[1].full_url).query)["page"] == ["page_2"]


def test_fetch_anthropic_usage_requires_a_key() -> None:
    with pytest.raises(ValueError, match="admin_key"):
        fetch_anthropic_usage(START, END, admin_key="", opener=lambda *a, **k: None)
