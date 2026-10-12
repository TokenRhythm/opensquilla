"""Compatibility alias for the shared chat turn stream module."""

from __future__ import annotations

import sys

from opensquilla.cli.chat import turn_stream as _target

sys.modules[__name__] = _target
