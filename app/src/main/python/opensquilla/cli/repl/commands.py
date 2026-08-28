"""Compatibility alias for the TUI-owned slash-command helpers."""

from __future__ import annotations

import sys

from opensquilla.cli.tui import commands as _target

sys.modules[__name__] = _target
