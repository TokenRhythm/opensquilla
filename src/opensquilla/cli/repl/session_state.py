"""Compatibility alias for the shared chat session state module."""

from __future__ import annotations

import sys

from opensquilla.cli.chat import session_state as _target

sys.modules[__name__] = _target
