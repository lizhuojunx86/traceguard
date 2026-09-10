"""Minimal proportion statistics for the drift report.

A deliberate ~30-line reimplementation of the Wilson score interval rather
than an import from ``analysis/eps_revision.py``: that script is a
repo-root analysis tool outside the package, and the SDK must not grow a
dependency on it (nor on scipy). ``analysis/eps_revision.py`` stays the
reference the numbers are checked against, not a runtime import.

Wilson rather than the normal approximation for the same reason it was chosen
there: the rates involved are often well under 20% and per-source n is small,
where the naive interval runs off the end of [0, 1].
"""
from __future__ import annotations

import math

#: Two-sided normal quantile at 95%. Same constant as analysis/eps_revision.py,
#: to the same precision, so the two agree digit for digit on shared inputs.
Z_95 = 1.959963984540054


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for ``successes`` out of ``n``.

    ``n == 0`` returns ``(0.0, 0.0)`` — with no observations there is no
    interval, and inventing a wide one would read as a measurement.
    """
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    halfwidth = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - halfwidth), min(1.0, centre + halfwidth))
