"""Compatibility alias for the TUI-owned input bridge."""

from __future__ import annotations

import sys

from opensquilla.cli.tui import input_bridge as _target

sys.modules[__name__] = _target
