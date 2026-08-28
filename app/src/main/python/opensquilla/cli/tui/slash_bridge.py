"""Compatibility alias for slash adapter composition."""

from __future__ import annotations

import sys

from opensquilla.cli.tui.adapters import slash_bridge as _target

sys.modules[__name__] = _target
