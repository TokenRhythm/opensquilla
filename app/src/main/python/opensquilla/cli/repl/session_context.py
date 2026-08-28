"""Compatibility alias for the shared chat session context module."""

from __future__ import annotations

import sys

from opensquilla.cli.chat import session_context as _target

sys.modules[__name__] = _target
