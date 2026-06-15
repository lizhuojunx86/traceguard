"""Tests for the training-contamination groundwork (look-ahead kind 1)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.contamination import (
    CONTAMINATION_KEY,
    ClaimVerdict,
    ClaimVerifier,
    ContaminationScore,
    RegimeDecay,
    attach_contamination_score,
    min_k_prob,
    performance_decay_across_regimes,
)
from traceguard.sdk.tracer import Tracer
from traceguard.store.models import Trace

UTC = timezone.utc


# ── min_k_prob ────────────────────────────────────────────────────────────
def test_min_k_prob_scores_memorized_higher_than_natural():
    memorized = [-0.01, -0.02, -0.01, -0.03, -0.02]
    natural = [-2.1, -3.4, -1.8, -4.0, -2.7]
    assert min_k_prob(memorized, k=0.4) > min_k_prob(natural, k=0.4)


def test_min_k_prob_averages_lowest_fraction():
    # lowest 50% of these four are -5.0 and -6.0 -> mean -5.5
    assert min_k_prob([-0.1, -0.2, -5.0, -6.0], k=0.5) == pytest.approx(-5.5)


def test_min_k_prob_validates_inputs():
    with pytest.raises(ValueError):
        min_k_prob([])
    with pytest.raises(ValueError):
        min_k_prob([-1.0], k=0.0)
    with pytest.raises(ValueError):
        min_k_prob([-1.0], k=1.5)


# ── performance_decay_across_regimes ──────────────────────────────────────
def test_performance_decay_flags_pre_cutoff_advantage():
    res = performance_decay_across_regimes(
        {"pre": [0.9, 0.92, 0.88], "post": [0.6, 0.58, 0.62]},
        baseline_regime="pre",
        comparison_regime="post",
        threshold=0.1,
    )
    assert isinstance(res, RegimeDecay)
    assert res.decay == pytest.approx(0.3, abs=0.01)
    assert res.flagged is True
    assert set(res.regime_means) == {"pre", "post"}


def test_performance_decay_not_flagged_when_stable():
    res = performance_decay_across_regimes(
        {"pre": [0.70, 0.71], "post": [0.69, 0.70]},
        baseline_regime="pre",
        comparison_regime="post",
        threshold=0.1,
    )
    assert res.flagged is False


def test_performance_decay_missing_regime_raises():
    with pytest.raises(ValueError):
        performance_decay_across_regimes(
            {"pre": [0.9]}, baseline_regime="pre", comparison_regime="post"
        )


# ── attach to trace via output_parsed (no schema change) ──────────────────
def test_attach_contamination_score_uses_output_parsed(engine):
    tracer = Tracer(engine)
    with tracer.span("p", "c", "llm_complete") as span:
        span.record_input({"x": 1})
        span.record_output(parsed={"answer": "42"}, parse_status="success")
    with Session(engine) as sess:
        trace_id = sess.execute(select(Trace.trace_id)).scalar_one()

    score = ContaminationScore(
        method="min_k_prob", value=-0.02, flagged=True, detail={"k": 0.2}
    )
    data = attach_contamination_score(trace_id, score, engine=engine)

    assert data["answer"] == "42"  # original business output preserved
    assert data[CONTAMINATION_KEY][0]["method"] == "min_k_prob"
    assert data[CONTAMINATION_KEY][0]["detail"] == {"k": 0.2}

    # Appends rather than overwrites, and persists across sessions.
    attach_contamination_score(
        trace_id, ContaminationScore("regime_decay", 0.3, True), engine=engine
    )
    with Session(engine) as sess:
        row = sess.get(Trace, trace_id)
        assert len(row.output_parsed[CONTAMINATION_KEY]) == 2
        assert row.parse_status == "success"  # untouched


def test_attach_contamination_score_missing_trace_raises(engine):
    with pytest.raises(LookupError):
        attach_contamination_score(
            999, ContaminationScore("m", 0.0, False), engine=engine
        )


# ── claim verification protocol ───────────────────────────────────────────
def test_claim_verifier_protocol_is_satisfiable():
    class StubVerifier:
        def verify(self, claim: str, *, as_of: datetime) -> ClaimVerdict:
            return ClaimVerdict(claim=claim, supported_as_of=None, is_contaminated=True)

    verifier = StubVerifier()
    assert isinstance(verifier, ClaimVerifier)  # runtime_checkable
    verdict = verifier.verify("Acme beat estimates", as_of=datetime(2023, 1, 1, tzinfo=UTC))
    assert verdict.is_contaminated is True
    assert verdict.supported_as_of is None


def test_attach_contamination_score_refuses_non_dict_output(engine):
    # A list/scalar output_parsed is legal; attaching must NOT silently clobber it.
    tracer = Tracer(engine)
    with tracer.span("p", "c", "llm_complete") as span:
        span.record_input({"x": 1})
        span.record_output(parsed=["a", "b"], parse_status="success")
    with Session(engine) as sess:
        trace_id = sess.execute(select(Trace.trace_id)).scalar_one()

    with pytest.raises(TypeError):
        attach_contamination_score(
            trace_id, ContaminationScore("min_k_prob", -0.02, True), engine=engine
        )
    with Session(engine) as sess:
        assert sess.get(Trace, trace_id).output_parsed == ["a", "b"]  # untouched


def test_performance_decay_reports_only_compared_regimes():
    res = performance_decay_across_regimes(
        {"pre": [0.9], "post": [0.6], "mid": [0.7]},
        baseline_regime="pre",
        comparison_regime="post",
    )
    assert set(res.regime_means) == {"pre", "post"}  # 'mid' excluded
