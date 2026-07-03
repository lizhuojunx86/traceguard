"""Alias so ``python -m traceguard.routing_audit`` works like ``...routing_audit.ingest``."""
from __future__ import annotations

import sys

from traceguard.routing_audit.ingest import main

if __name__ == "__main__":
    sys.exit(main())
