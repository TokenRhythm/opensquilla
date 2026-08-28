"""Compatibility alias for slash command policy helpers."""

from __future__ import annotations

import sys

from opensquilla.cli.tui.adapters import slash_policy as _target

sys.modules[__name__] = _target
