"""Tests for prompt YAML loading + rendering (SPEC §4.3)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from traceguard.registry import load_prompt


def _write_yaml(root, project, component, version, body, fmt="raw", introduced="2026-01-01T00:00:00+00:00"):
    d = root / project / component
    d.mkdir(parents=True, exist_ok=True)
    (d / f"v{version}.yaml").write_text(
        f"template_body: |\n  {body}\ntemplate_format: {fmt}\nintroduced_at: {introduced}\n",
        encoding="utf-8",
    )


def test_load_raw_template(tmp_path):
    _write_yaml(tmp_path, "demo", "extractor", 1, "hello world")
    tpl = load_prompt("demo/extractor/v1", prompts_root=tmp_path)
    assert tpl.prompt_template_id == "demo/extractor/v1"
    assert tpl.template_body.strip() == "hello world"
    assert tpl.template_format == "raw"
    assert tpl.introduced_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert len(tpl.prompt_template_hash) == 64


def test_fstring_render(tmp_path):
    d = tmp_path / "demo" / "extractor"
    d.mkdir(parents=True)
    (d / "v1.yaml").write_text(
        'template_body: "hello {name}"\ntemplate_format: fstring\nintroduced_at: 2026-01-01T00:00:00+00:00\n',
        encoding="utf-8",
    )
    tpl = load_prompt("demo/extractor/v1", prompts_root=tmp_path)
    assert tpl.render(name="world") == "hello world"


def test_raw_template_rejects_variables(tmp_path):
    _write_yaml(tmp_path, "demo", "extractor", 1, "hello")
    tpl = load_prompt("demo/extractor/v1", prompts_root=tmp_path)
    with pytest.raises(ValueError, match="raw template"):
        tpl.render(name="world")


def test_invalid_template_id_rejected(tmp_path):
    with pytest.raises(ValueError, match="invalid prompt_template_id"):
        load_prompt("BAD", prompts_root=tmp_path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_prompt("demo/extractor/v1", prompts_root=tmp_path)


def test_introduced_at_must_be_timezone_aware(tmp_path):
    d = tmp_path / "demo" / "extractor"
    d.mkdir(parents=True)
    (d / "v1.yaml").write_text(
        'template_body: "hi"\ntemplate_format: raw\nintroduced_at: 2026-01-01T00:00:00\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        load_prompt("demo/extractor/v1", prompts_root=tmp_path)


def test_same_body_same_hash(tmp_path):
    _write_yaml(tmp_path, "demo", "a", 1, "shared")
    _write_yaml(tmp_path, "demo", "b", 1, "shared")
    h1 = load_prompt("demo/a/v1", prompts_root=tmp_path).prompt_template_hash
    h2 = load_prompt("demo/b/v1", prompts_root=tmp_path).prompt_template_hash
    assert h1 == h2
