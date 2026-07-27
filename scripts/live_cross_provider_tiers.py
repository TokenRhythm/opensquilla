#!/usr/bin/env python3
"""Compatibility entry point for the safe mixed-provider Gateway harness.

The former direct cross-provider smoke loaded ambient ``.env`` credentials and
performed provider calls in its own process.  This entry point now delegates to
the registry-backed, child-isolated harness so credentials are parsed as inert
data, premium models are rejected, raw Gateway artifacts are temporary, and the
final report is credential-scanned before it is written.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.live_mixed_provider_gateway import (  # noqa: E402
    main as _run_safe_mixed_provider_matrix,
)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the unified safe Router and Ensemble provider matrix."""

    return _run_safe_mixed_provider_matrix(argv)


if __name__ == "__main__":
    raise SystemExit(main())
