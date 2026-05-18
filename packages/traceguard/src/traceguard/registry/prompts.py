"""Prompt template registry — Phase 0 backend is YAML files in a directory.

Per ROADMAP §3.1, prompt_registry in Phase 0 is filesystem-backed (git-tracked)
rather than DB-backed. The contract is the same set of MUST fields from
SPEC §3.3; only the backend differs.

Directory layout (relative to prompts_root, default ``prompts/`` at CWD):

    prompts/
    └── <project>/
        └── <component>/
            └── v1.yaml
            └── v2.yaml

Each YAML file:

    template_body: |
      ... prompt content with {variables} ...
    template_format: jinja2 | fstring | raw
    introduced_at: 2026-05-18T00:00:00+00:00
    expected_output_schema: null     # optional, JSON-Schema-style dict
    superseded_at: null
    superseded_by: null
    notes: null

``prompt_template_id`` is derived from the path: ``<project>/<component>/v<N>``.
``prompt_template_hash`` is SHA-256 of ``template_body`` as bytes.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


_TEMPLATE_ID_RE = re.compile(r"^(?P<project>[a-z0-9_-]+)/(?P<component>[a-z0-9_-]+)/v\d+$")


@dataclass(frozen=True)
class PromptTemplate:
    prompt_template_id: str
    prompt_template_hash: str
    template_body: str
    template_format: str
    introduced_at: datetime
    expected_output_schema: dict[str, Any] | None = None
    superseded_at: datetime | None = None
    superseded_by: str | None = None
    notes: str | None = None

    def render(self, **variables: Any) -> str:
        """Render the template with ``variables`` injected."""
        if self.template_format == "raw":
            if variables:
                raise ValueError("raw template does not accept variables")
            return self.template_body
        if self.template_format == "fstring":
            return self.template_body.format(**variables)
        if self.template_format == "jinja2":
            try:
                import jinja2
            except ImportError as exc:  # pragma: no cover - optional dep
                raise ImportError(
                    "template_format=jinja2 requires the 'jinja2' package; "
                    "install with `pip install jinja2`"
                ) from exc
            return jinja2.Template(self.template_body).render(**variables)
        raise ValueError(
            f"unknown template_format={self.template_format!r}; "
            "expected one of jinja2 | fstring | raw"
        )


def _prompts_root(explicit: str | os.PathLike[str] | None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("TRACEGUARD_PROMPTS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd() / "prompts"


def load_prompt(
    template_id: str,
    *,
    prompts_root: str | os.PathLike[str] | None = None,
) -> PromptTemplate:
    """Load a prompt template by its id (``<project>/<component>/v<N>``)."""
    if not _TEMPLATE_ID_RE.match(template_id):
        raise ValueError(
            f"invalid prompt_template_id {template_id!r}; "
            "expected '<project>/<component>/v<N>' with snake-case names"
        )
    root = _prompts_root(prompts_root)
    path = root / f"{template_id}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"prompt template not found: {path}")
    with path.open("r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}

    body = data.get("template_body")
    if not isinstance(body, str):
        raise ValueError(f"prompt template {template_id!r} missing string 'template_body'")
    fmt = data.get("template_format", "raw")
    if fmt not in {"jinja2", "fstring", "raw"}:
        raise ValueError(
            f"prompt template {template_id!r}: invalid template_format={fmt!r}; "
            "expected jinja2 | fstring | raw"
        )
    introduced_raw = data.get("introduced_at")
    if introduced_raw is None:
        raise ValueError(f"prompt template {template_id!r} missing required 'introduced_at'")
    introduced_at = (
        introduced_raw if isinstance(introduced_raw, datetime) else datetime.fromisoformat(str(introduced_raw))
    )
    if introduced_at.tzinfo is None:
        raise ValueError(
            f"prompt template {template_id!r}: introduced_at must be timezone-aware"
        )

    superseded_raw = data.get("superseded_at")
    superseded_at: datetime | None
    if superseded_raw is None:
        superseded_at = None
    else:
        superseded_at = (
            superseded_raw
            if isinstance(superseded_raw, datetime)
            else datetime.fromisoformat(str(superseded_raw))
        )

    return PromptTemplate(
        prompt_template_id=template_id,
        prompt_template_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        template_body=body,
        template_format=fmt,
        introduced_at=introduced_at,
        expected_output_schema=data.get("expected_output_schema"),
        superseded_at=superseded_at,
        superseded_by=data.get("superseded_by"),
        notes=data.get("notes"),
    )
