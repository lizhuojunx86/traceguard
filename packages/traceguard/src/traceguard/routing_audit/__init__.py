"""Contract-external opt-in extension: routing audit (SPEC §6.6 style).

Purely additive, like ``exporters`` / ``contamination`` / ``loop``: nothing
here is re-exported from the top-level ``traceguard`` package, no MUST field
is added or changed, and the normalize algorithm is untouched. Import by
submodule path only::

    from traceguard.routing_audit.ingest_claude_code import ingest

First capability: backfill local Claude Code session history
(``~/.claude/projects/**/*.jsonl``) into the ``traces`` table so that
model-routing questions ("which project / which role / which model tier /
how much did it cost") can be answered from real usage data. See
``ingest_claude_code`` for the observed-schema notes and privacy rules,
``pricing`` for the list-price table, and ``models`` for the
contract-external ``routing_audit_ingest_log`` table (idempotency +
per-batch rollback).

CLI::

    python -m traceguard.routing_audit.ingest --help
"""

__all__: list[str] = []
