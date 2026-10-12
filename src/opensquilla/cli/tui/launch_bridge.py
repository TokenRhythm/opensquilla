"""Compatibility alias for TUI launch composition."""

from __future__ import annotations

import sys

from opensquilla.cli.tui.adapters import launch_bridge as _target

sys.modules[__name__] = _target
