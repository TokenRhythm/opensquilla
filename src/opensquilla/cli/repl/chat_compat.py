"""Compatibility alias for the TUI-owned chat command export helpers."""

from __future__ import annotations

import sys

from opensquilla.cli.tui import chat_compat as _target

sys.modules[__name__] = _target
