"""External anchor sinks and periodic anchoring (audit v2 scheduling layer).

Boundary statement 1 in docs/audit.md: the chain is not a MAC, so a full-chain
rewrite or a tail truncation is undetectable *except* against an anchor stored
where the DB writer cannot reach — and "anchoring frequency = exposure window".
v1 only *exported* anchors; this module is the layer on top of the unchanged
:func:`~traceguard.audit.export_anchor` contract that puts them somewhere and
does so on a cadence.

What each sink honestly gives you:

- :class:`FileAnchorSink` — a JSON-lines file. Out of the DB, not out of the
  host: whoever can edit the DB file can usually edit a sibling file. It means
  something on a different host / filesystem / append-only medium.
- :class:`GitNoteAnchorSink` — ``git notes --ref refs/notes/traceguard-audit
  append`` on a commit. Out of the DB; as tamper-evident as the repository's
  own history, which is only as good as a remote the DB writer cannot
  force-push.
- :class:`WebhookAnchorSink` — HTTP POST of the anchor JSON to a URL you run
  (ticketing, log pipeline, timestamping service). What the receiver does with
  it is the actual guarantee; the sink only delivers.

Failure semantics: :func:`anchor_to` tries EVERY sink and then raises
:class:`AnchorSinkError` if any failed — an anchor that silently never landed
is a false sense of coverage (SPEC B3.4: a false negative is the dangerous
one). :class:`AnchorScheduler` logs at ERROR and keeps its cadence, so one
unreachable webhook does not stop the file sink from anchoring.

Zero new dependencies: stdlib ``urllib`` / ``subprocess`` / ``threading``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol, runtime_checkable
from urllib import request as _urlrequest

from sqlalchemy.engine import Engine

from traceguard.audit.verify import ChainAnchor, export_anchor

_log = logging.getLogger("traceguard.audit.anchors")

DEFAULT_GIT_NOTES_REF = "refs/notes/traceguard-audit"


class AnchorSinkError(RuntimeError):
    """One or more sinks failed to store an anchor (see the message for which)."""


@runtime_checkable
class AnchorSink(Protocol):
    """Where an exported anchor goes. Implement ``store``; ``name`` labels logs."""

    name: str

    def store(self, anchor: ChainAnchor) -> None: ...


class FileAnchorSink:
    """Append each anchor as one JSON line to ``path`` (created if missing).

    ``latest()`` reads the newest anchor back, which is what
    ``python -m traceguard.audit verify --anchor-file`` uses to close the loop
    without copy-pasting JSON.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.name = f"file:{self.path}"

    def store(self, anchor: ChainAnchor) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = anchor.to_json() + "\n"
        with open(self.path, "a", encoding="ascii") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def history(self) -> Iterator[ChainAnchor]:
        """Every parseable anchor line, oldest first (unparseable lines skipped)."""
        if not self.path.exists():
            return
        with open(self.path, encoding="ascii") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield ChainAnchor.from_json(raw)
                except (ValueError, KeyError, TypeError):
                    continue

    def latest(self) -> ChainAnchor | None:
        last: ChainAnchor | None = None
        for anchor in self.history():
            last = anchor
        return last


class GitNoteAnchorSink:
    """Append each anchor to a git note on ``target`` under ``ref``.

    Notes live in the repository object store, outside the audited DB; push
    ``ref`` to a remote (``git push origin refs/notes/traceguard-audit``) to
    move the trust root off the host as well. ``target`` defaults to ``HEAD``
    — anchoring onto whatever commit the repo is at, which also records *when*
    (in code-history terms) the chain looked like this.
    """

    def __init__(
        self,
        repo: str | os.PathLike[str] = ".",
        *,
        ref: str = DEFAULT_GIT_NOTES_REF,
        target: str = "HEAD",
        git: str = "git",
    ) -> None:
        self.repo = Path(repo)
        self.ref = ref
        self.target = target
        self.git = git
        self.name = f"git-note:{self.repo}@{self.target}"

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.git, "-C", str(self.repo), "notes", "--ref", self.ref, *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def store(self, anchor: ChainAnchor) -> None:
        proc = self._run("append", "-m", anchor.to_json(), self.target)
        if proc.returncode != 0:
            raise AnchorSinkError(
                f"git notes append failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
            )

    def latest(self) -> ChainAnchor | None:
        proc = self._run("show", self.target)
        if proc.returncode != 0:
            return None  # no note on the target yet
        last: ChainAnchor | None = None
        for raw in proc.stdout.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                last = ChainAnchor.from_json(raw)
            except (ValueError, KeyError, TypeError):
                continue
        return last


Opener = Callable[..., Any]


class WebhookAnchorSink:
    """POST the anchor JSON (``application/json``) to ``url``.

    A 2xx response is success; anything else (or a transport error) raises
    :class:`AnchorSinkError`. ``opener`` is ``urllib.request.urlopen`` unless
    injected (tests); ``headers`` is where an auth token goes — never put it
    in the URL, URLs end up in logs.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        opener: Opener | None = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self._opener = opener or _urlrequest.urlopen
        self.name = f"webhook:{url}"

    def store(self, anchor: ChainAnchor) -> None:
        body = anchor.to_json().encode("ascii")
        req = _urlrequest.Request(
            self.url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", **self.headers},
        )
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", None)
                if status is None:
                    status = resp.getcode()
        except AnchorSinkError:
            raise
        except Exception as exc:  # noqa: BLE001 - any transport failure is a sink failure
            raise AnchorSinkError(f"webhook POST to {self.url} failed: {exc}") from exc
        if not 200 <= int(status) < 300:
            raise AnchorSinkError(f"webhook POST to {self.url} returned HTTP {status}")


def anchor_to(engine: Engine, sinks: Iterable[AnchorSink]) -> ChainAnchor:
    """Export the chain head once and store it in every sink.

    Every sink is attempted even after one fails; then a single
    :class:`AnchorSinkError` names all the failures. The anchor is returned
    either way when at least the export succeeded, so a caller that catches
    the error still has the value to print / retry.
    """
    anchor = export_anchor(engine)
    failures: list[str] = []
    for sink in sinks:
        try:
            sink.store(anchor)
        except Exception as exc:  # noqa: BLE001 - collect, then raise once
            failures.append(f"{getattr(sink, 'name', type(sink).__name__)}: {exc}")
    if failures:
        raise AnchorSinkError(
            f"anchor seq={anchor.seq} was NOT stored by {len(failures)} sink(s): "
            + "; ".join(failures)
        )
    return anchor


class AnchorScheduler:
    """Anchor to ``sinks`` every ``interval_s`` seconds on a daemon thread.

    The interval IS the exposure window (boundary statement 1): entries
    appended since the last tick can still be truncated silently. Sink
    failures are logged at ERROR (and counted in ``failures``) but never stop
    the cadence; ``run_once()`` is the same tick, synchronously, for callers
    that schedule themselves (cron, launchd, a test).
    """

    def __init__(self, engine: Engine, sinks: Iterable[AnchorSink], interval_s: float) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        self._engine = engine
        self._sinks = list(sinks)
        self.interval_s = float(interval_s)
        self.anchors_stored = 0
        self.failures = 0
        self.last_anchor: ChainAnchor | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> ChainAnchor | None:
        try:
            anchor = anchor_to(self._engine, self._sinks)
        except AnchorSinkError as exc:
            self.failures += 1
            _log.error("periodic anchoring failed: %s", exc)
            return None
        except Exception:  # noqa: BLE001 - the export itself failed (DB unreachable)
            self.failures += 1
            _log.error("periodic anchoring failed before export", exc_info=True)
            return None
        self.anchors_stored += 1
        self.last_anchor = anchor
        return anchor

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="traceguard-audit-anchor", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def parse_sink_spec(spec: str) -> AnchorSink:
    """Turn a CLI ``--sink`` value into a sink.

    ``file:PATH`` | ``git-note[:REPO]`` (default ``.``) | ``webhook:URL``.
    Webhook headers cannot be given on the command line on purpose (they hold
    tokens); set them in code or put the token in the receiver's allowlist.
    """
    kind, sep, rest = spec.partition(":")
    kind = kind.strip().lower()
    if kind == "file":
        if not rest:
            raise ValueError("file sink needs a path: file:PATH")
        return FileAnchorSink(rest)
    if kind in ("git-note", "git_note", "gitnote"):
        return GitNoteAnchorSink(rest or ".")
    if kind == "webhook":
        if not rest:
            raise ValueError("webhook sink needs a URL: webhook:https://...")
        return WebhookAnchorSink(rest)
    raise ValueError(f"unknown sink {spec!r}; expected file:PATH | git-note[:REPO] | webhook:URL")


__all__ = [
    "AnchorSink",
    "AnchorSinkError",
    "FileAnchorSink",
    "GitNoteAnchorSink",
    "WebhookAnchorSink",
    "anchor_to",
    "AnchorScheduler",
    "parse_sink_spec",
    "DEFAULT_GIT_NOTES_REF",
]
