"""Tests for the contract-external gateway presets."""
from __future__ import annotations

import pytest

from traceguard.gateways import GATEWAYS, client_kwargs, is_alias_model


def test_gateways_are_not_on_the_public_surface() -> None:
    """The frozen top-level API must not grow a gateway symbol."""
    import traceguard

    assert not hasattr(traceguard, "GATEWAYS")
    assert not hasattr(traceguard, "client_kwargs")
    assert "gateways" not in getattr(traceguard, "__all__", ())


@pytest.mark.parametrize("name", sorted(GATEWAYS))
def test_every_preset_is_well_formed(name: str) -> None:
    entry = GATEWAYS[name]
    assert entry.base_url.startswith("https://")
    # A trailing slash would double up when the SDK appends its paths.
    assert not entry.base_url.endswith("/")
    assert entry.env_key.isupper()
    assert entry.docs.startswith("https://")


def test_client_kwargs_uses_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-from-env")
    kwargs = client_kwargs("orcarouter")
    assert kwargs["api_key"] == "sk-from-env"
    assert kwargs["base_url"] == "https://api.orcarouter.ai/v1"


def test_explicit_key_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-from-env")
    assert client_kwargs("orcarouter", api_key="sk-explicit")["api_key"] == "sk-explicit"


def test_openrouter_sends_attribution_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-x")
    headers = client_kwargs("openrouter")["default_headers"]
    assert isinstance(headers, dict)
    # HTTP-Referer is the field OpenRouter keys the app entry on; without it
    # the title alone creates nothing.
    assert headers["HTTP-Referer"] == "https://github.com/lizhuojunx86/traceguard"
    assert headers["X-OpenRouter-Title"] == "TraceGuard"


def test_extra_headers_merge_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-x")
    headers = client_kwargs(
        "openrouter", extra_headers={"X-OpenRouter-Title": "Mine", "X-Extra": "1"}
    )["default_headers"]
    assert isinstance(headers, dict)
    assert headers["X-OpenRouter-Title"] == "Mine"
    assert headers["X-Extra"] == "1"


def test_missing_key_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ORCAROUTER_API_KEY"):
        client_kwargs("orcarouter")


def test_unknown_gateway_lists_the_known_ones() -> None:
    with pytest.raises(KeyError, match="openrouter"):
        client_kwargs("nope", api_key="sk-x")


@pytest.mark.parametrize(
    "model",
    ["orcarouter/auto", "openrouter/auto", "OrcaRouter/Auto", "some-provider:auto"],
)
def test_routing_aliases_are_detected(model: str) -> None:
    assert is_alias_model(model)


@pytest.mark.parametrize(
    "model",
    ["anthropic/claude-opus-4.7", "deepseek/deepseek-v4-pro-0813", "gpt-5.2", "", None],
)
def test_concrete_model_ids_are_not_aliases(model: str | None) -> None:
    assert not is_alias_model(model)


def test_every_declared_auto_alias_is_recognised() -> None:
    """If a preset documents an alias, the guard must catch it."""
    for entry in GATEWAYS.values():
        if entry.auto_alias:
            assert is_alias_model(entry.auto_alias), entry.name
