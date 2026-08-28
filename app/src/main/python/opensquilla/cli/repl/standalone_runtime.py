"""Compatibility alias for the TUI-owned standalone runtime module."""

from __future__ import annotations

import sys

from opensquilla.cli.tui import standalone_runtime as _target

sys.modules[__name__] = _target
