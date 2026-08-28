"""Compatibility alias for TUI input attachment bridge."""

from __future__ import annotations

import sys

from opensquilla.cli.tui.adapters import input_bridge as _target

sys.modules[__name__] = _target
