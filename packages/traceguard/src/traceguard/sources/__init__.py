"""traceguard.sources — point-in-time correctness for *retrieved data*.

**Experimental** (SPEC v1.2 §6.6). Off the frozen 29-symbol public surface,
like ``exporters.otel`` and ``contamination``: import from THIS submodule
path, not from ``traceguard``. Unlike ``traceguard.audit``, its API surface is
deliberately NOT in the contract-guard job yet — the fields have to survive
real use before they are worth freezing (revision decision D9; revisit after
two minors).

The gap it closes. SPEC §5's invariants cover the *model* (invariant 2), the
*prompt and reference tables* (invariant 3), and *feature ordering*
(invariant 1). They do not cover the data the pipeline fetched. A backtest can
therefore use the right model, the right prompt and the right
``feature_as_of``, be fed a vendor value that was rewritten into existence
weeks later, and pass all four invariants while being wrong. Published
measurement of exactly that: 41.4% of vendor ``epsActual`` values differ
between first sight and today, and 15.3% flip a binary entry decision
(``analysis/eps_revision.py`` recomputes both offline).

What a ``source_snapshot`` proves, and what it does not:

- **Proves**: which bytes the host handed to ``record_source`` at
  ``retrieved_at`` (by digest), and how their claimed publication time relates
  to ``feature_as_of`` (the ``verdict``).
- **Does not prove**: that the host actually fetched those bytes from
  ``source_uri`` — traceguard never made the request; nor that
  ``published_at`` is true — that is what the *source* says about itself.

**No retrieved content is stored.** Digests and metadata only; that is the
contract intent, not a mode you can turn off. Archiving the bytes is the
consumer's own business, joined back on ``content_hash``.

Importing this module has no side effects — no tables, no listeners. Call
:func:`enable` once per DB. A snapshot write that fails is fail-open
(SPEC §4.1): it never breaks the trace write or the host call.

Quickstart::

    import traceguard
    from traceguard import sources

    engine = traceguard.make_engine("sqlite:///traces.db")
    sources.enable(engine)
    traceguard.tracer.configure(engine)

    with traceguard.tracer.span("proj", "comp", "llm_complete",
                                feature_as_of=as_of) as span:
        resp = httpx.get(url)
        span.record_source(sources.from_http_response(resp), strict=False)
        ...
"""
from __future__ import annotations

from sqlalchemy.engine import Engine

from traceguard.sources.models import (
    SOURCE_KINDS,
    SourceSnapshotRow,
    ensure_source_tables,
    source_tables_exist,
)
from traceguard.sources.record import (
    STR_ENCODING,
    SourceSnapshot,
    content_digest,
    from_http_response,
    from_mcp_result,
)
from traceguard.sources.validate import (
    REFERENCE_KIND,
    SourceVerdict,
    validate_source_snapshot,
)


def enable(engine: Engine) -> None:
    """Create ``source_snapshots`` for the DB behind ``engine`` (idempotent).

    Explicit, mirroring ``traceguard.audit.enable``: importing this package
    must never add a table to somebody's database as a side effect. Until this
    is called, ``Span.record_source`` validates as usual (so a strict call site
    still refuses an anachronistic source) but the row write finds no table and
    fails open with a warning.
    """
    ensure_source_tables(engine)


__all__ = [
    # activation
    "enable",
    "ensure_source_tables",
    "source_tables_exist",
    # building a snapshot
    "SourceSnapshot",
    "content_digest",
    "from_http_response",
    "from_mcp_result",
    "SOURCE_KINDS",
    "STR_ENCODING",
    # invariant 3 on retrieved data
    "SourceVerdict",
    "validate_source_snapshot",
    "REFERENCE_KIND",
    # ORM
    "SourceSnapshotRow",
]
