"""Compatibility alias for the standalone slash adapter."""

from __future__ import annotations

import sys

from opensquilla.cli.tui.adapters import slash_standalone as _target

sys.modules[__name__] = _target
