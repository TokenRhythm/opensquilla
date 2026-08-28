"""Compatibility alias for the TUI-owned slash adapter bridge."""

from __future__ import annotations

import sys

from opensquilla.cli.tui import slash_bridge as _target

sys.modules[__name__] = _target
