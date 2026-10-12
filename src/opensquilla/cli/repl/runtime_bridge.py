"""Compatibility alias for the TUI-owned runtime bridge module."""

from __future__ import annotations

import sys

from opensquilla.cli.tui import runtime_bridge as _target

sys.modules[__name__] = _target
