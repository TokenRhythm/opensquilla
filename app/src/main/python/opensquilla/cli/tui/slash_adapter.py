"""Compatibility alias for the gateway slash adapter."""

from __future__ import annotations

import sys

from opensquilla.cli.tui.adapters import slash_gateway as _target

sys.modules[__name__] = _target
