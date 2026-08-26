"""Tests for the gateway probe harness. No network, no key, no spend."""
from __future__ import annotations

import pytest

from traceguard.routing_integrity.conformance import (
    DEFAULT_PROBES,
    Budget,
    BudgetExceeded,
    Probe,
    estimate_worst_case_usd,
    plan,
    run_probes,
)


class _Resp:
    def __init__(self, model: str | None) -> None:
        if model is not None:
            self.model = model


def _caller(models):
    """Return a call() that yields the given served models in order."""
    seq = iter(models)

    def call(model: str, prompt: str, max_tokens: int):
        return _Resp(next(seq))

    return call


# ── the guards ────────────────────────────────────────────────────────────


def test_too_many_calls_is_refused_before_spending() -> None:
    with pytest.raises(BudgetExceeded, match="max_calls"):
        plan(list(DEFAULT_PROBES), repeats=100, budget=Budget(max_calls=30))


def test_cost_ceiling_is_refused_before_spending() -> None:
    with pytest.raises(BudgetExceeded, match="max_estimated_usd"):
        plan(list(DEFAULT_PROBES), repeats=1, budget=Budget(max_estimated_usd=0.0001))


def test_a_plan_within_budget_reports_its_ceiling() -> None:
    n_calls, worst = plan(list(DEFAULT_PROBES), repeats=2, budget=Budget())
    assert n_calls == len(DEFAULT_PROBES) * 2
    assert 0 < worst < Budget().max_estimated_usd


def test_worst_case_scales_with_calls_and_tokens() -> None:
    b = Budget()
    assert estimate_worst_case_usd(2, b) == pytest.approx(2 * estimate_worst_case_usd(1, b))
    cheap = Budget(max_tokens=10)
    assert estimate_worst_case_usd(1, cheap) < estimate_worst_case_usd(1, b)


def test_run_probes_enforces_the_budget_too() -> None:
    """The guard must not live only in the CLI."""
    with pytest.raises(BudgetExceeded):
        run_probes(
            _caller(["m"] * 999),
            gateway="G",
            alias="g/auto",
            repeats=100,
            budget=Budget(max_calls=5),
        )


def test_max_tokens_reaches_the_api_call() -> None:
    seen = {}

    def call(model: str, prompt: str, max_tokens: int):
        seen["max_tokens"] = max_tokens
        return _Resp("m")

    run_probes(
        call, gateway="G", alias="g/auto",
        probes=[Probe("trivial", "hi")], budget=Budget(max_tokens=64),
    )
    assert seen["max_tokens"] == 64


# ── what the probe actually finds ─────────────────────────────────────────


def test_a_stable_alias_shows_one_model() -> None:
    probes = [Probe("trivial", "hi")]
    report = run_probes(
        _caller(["deepseek/v4", "deepseek/v4"]),
        gateway="G", alias="g/auto", probes=probes, repeats=2,
    )
    assert report.served_models == {"deepseek/v4"}
    assert report.unstable_prompts == {}


def test_the_headline_finding_is_detected() -> None:
    """Same prompt, same alias, two different models across repeats."""
    probes = [Probe("hard", "prove something")]
    report = run_probes(
        _caller(["deepseek/v4", "anthropic/claude-opus-5"]),
        gateway="G", alias="g/auto", probes=probes, repeats=2,
    )
    assert len(report.served_models) == 2
    unstable = report.unstable_prompts
    assert "prove something" in unstable
    assert unstable["prove something"] == {"deepseek/v4", "anthropic/claude-opus-5"}
    assert "different model" in report.render()


def test_a_silent_gateway_is_counted_separately_from_an_error() -> None:
    report = run_probes(
        _caller([None, "m"]),
        gateway="G", alias="g/auto",
        probes=[Probe("trivial", "a"), Probe("trivial", "b")],
    )
    assert report.silent_calls == 1
    assert "unverifiable by construction" in report.render()


def test_auth_failure_stops_the_run_immediately() -> None:
    calls = {"n": 0}

    def call(model: str, prompt: str, max_tokens: int):
        calls["n"] += 1
        raise RuntimeError("401 Unauthorized: invalid api key")

    report = run_probes(
        call, gateway="G", alias="g/auto", probes=list(DEFAULT_PROBES), repeats=2,
        budget=Budget(max_calls=30, max_estimated_usd=5.0),
    )
    # One attempt, then stop — never a retry loop against a paid endpoint.
    assert calls["n"] == 1
    assert len(report.observations) == 1
    assert report.observations[0].error is not None


def test_default_budget_stops_a_run_the_user_did_not_size() -> None:
    """The default ceiling is deliberately tight: 3 repeats of the full suite
    already exceeds $1, so scaling up has to be an explicit decision."""
    with pytest.raises(BudgetExceeded):
        plan(list(DEFAULT_PROBES), repeats=3, budget=Budget())
    plan(list(DEFAULT_PROBES), repeats=2, budget=Budget())  # 12 calls is fine


def test_a_transient_error_does_not_abort_the_whole_run() -> None:
    seq = iter([RuntimeError("temporary upstream hiccup"), _Resp("m")])

    def call(model: str, prompt: str, max_tokens: int):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    report = run_probes(
        call, gateway="G", alias="g/auto",
        probes=[Probe("trivial", "a"), Probe("trivial", "b")],
    )
    assert len(report.observations) == 2
    assert report.served_models == {"m"}


def test_probe_suite_spans_the_routers_grading_range() -> None:
    """An all-trivial suite would only ever see the cheap tier."""
    assert {p.tier for p in DEFAULT_PROBES} == {"trivial", "moderate", "hard"}


def test_report_survives_a_run_with_no_successful_calls() -> None:
    def call(model: str, prompt: str, max_tokens: int):
        raise RuntimeError("temporary")

    report = run_probes(call, gateway="G", alias="g/auto", probes=[Probe("t", "a")])
    assert report.served_models == set()
    assert "0 call(s) named no model" not in report.render()
    assert "failed" in report.render()


def test_missing_openai_extra_explains_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing optional extra must read as an instruction, not a traceback."""
    import builtins

    from traceguard.routing_integrity import conformance

    real_import = builtins.__import__

    def no_openai(name, *args, **kwargs):
        if name == "openai":
            raise ImportError("No module named 'openai'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_openai)
    with pytest.raises(conformance.MissingExtra, match="uv sync --extra openai"):
        conformance._build_caller("orcarouter", "sqlite:///:memory:")


def test_cli_exits_cleanly_when_the_extra_is_absent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from traceguard.routing_integrity import conformance

    monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(
        conformance, "_build_caller",
        lambda *a, **k: (_ for _ in ()).throw(conformance.MissingExtra("nope, install it")),
    )
    assert conformance.main(["--gateway", "orcarouter", "--repeats", "1"]) == 2
    assert "nope, install it" in capsys.readouterr().out


def test_build_caller_wires_a_real_wrapped_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one place that touches real SDK signatures, and the only one the
    injected-caller tests above cannot see.

    Every other test here passes its own ``call()``, so ``_build_caller`` ran
    unexercised and shipped calling ``wrap_openai`` without its required
    ``project`` / ``component``. A stub ``openai`` module lets the real
    ``wrap_openai`` run here without the extra installed — which matters
    because CI does not install it, so ``importorskip`` would protect nothing.
    """
    import sys
    import types

    from traceguard.routing_integrity import conformance

    sent: dict[str, object] = {}

    class _StubOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.init_kwargs = kwargs

            def create(**call_kwargs: object) -> object:
                sent.update(call_kwargs)
                return types.SimpleNamespace(model="served/model-x", id="r1",
                                             choices=[], usage=None)

            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=create)
            )

    stub = types.ModuleType("openai")
    stub.OpenAI = _StubOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", stub)
    monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test")

    caller = conformance._build_caller("orcarouter", "sqlite:///:memory:")
    response = caller("orcarouter/auto", "hi", 64)

    assert getattr(response, "model", None) == "served/model-x"
    assert sent["model"] == "orcarouter/auto"
    assert sent["max_tokens"] == 64


def test_build_caller_result_drives_run_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through the real wiring, still with no network."""
    import sys
    import types

    from traceguard.routing_integrity import conformance

    class _StubOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(
                    create=lambda **k: types.SimpleNamespace(
                        model="served/model-x", id="r", choices=[], usage=None
                    )
                )
            )

    stub = types.ModuleType("openai")
    stub.OpenAI = _StubOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", stub)
    monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test")

    report = run_probes(
        conformance._build_caller("orcarouter", "sqlite:///:memory:"),
        gateway="OrcaRouter", alias="orcarouter/auto",
        probes=[Probe("trivial", "hi")], repeats=2,
    )
    assert report.served_models == {"served/model-x"}


def test_console_scripts_point_at_real_entry_points() -> None:
    """A typo in pyproject only surfaces at install time, which is too late.

    Both CLIs exist because `python -m traceguard...` needs the package
    importable from the working directory, and this repo's editable install
    does not always provide that.
    """
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    scripts = tomllib.loads((root / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts, "the console scripts disappeared"

    for name, target in scripts.items():
        module_path, _, func_name = target.partition(":")
        module = __import__(module_path, fromlist=[func_name])
        func = getattr(module, func_name, None)
        assert callable(func), f"{name} -> {target} is not callable"
        # Every entry point must accept argv so it is testable without sys.argv.
        import inspect

        assert len(inspect.signature(func).parameters) >= 1, name
