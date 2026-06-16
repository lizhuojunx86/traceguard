"""Optional trace exporters.

These are additive: the SQLite/SQLAlchemy store remains the source of truth.
Each exporter lives in its own module and pulls in heavier optional deps only
when imported, so importing this package never requires an extra.

OpenTelemetry / OpenInference export (extra ``traceguard[otel]``)::

    from traceguard.exporters.otel import export_trace
"""

__all__: list[str] = []
