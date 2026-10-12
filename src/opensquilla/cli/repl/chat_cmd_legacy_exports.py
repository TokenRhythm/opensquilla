"""Compatibility alias for the TUI-owned chat command export resolver."""

from __future__ import annotations

import sys

from opensquilla.cli.tui import chat_cmd_exports as _target

sys.modules[__name__] = _target
