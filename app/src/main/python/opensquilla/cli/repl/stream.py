"""Compatibility alias for neutral TUI turn-stream helpers."""

from __future__ import annotations

import sys

from opensquilla.cli.tui import turn_bridge as _target

sys.modules[__name__] = _target
