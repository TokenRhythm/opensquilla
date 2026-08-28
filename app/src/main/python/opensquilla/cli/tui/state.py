"""Compatibility alias for the TUI backend state module."""

from __future__ import annotations

import sys

from opensquilla.cli.tui.backend import state as _target

sys.modules[__name__] = _target
