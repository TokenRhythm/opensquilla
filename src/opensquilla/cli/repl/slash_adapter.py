"""Compatibility alias for the TUI-owned gateway slash adapter."""

from __future__ import annotations

import sys

from opensquilla.cli.tui import slash_adapter as _target

sys.modules[__name__] = _target
