"""Compatibility alias for the shared gateway chat runtime module."""

from __future__ import annotations

import sys

from opensquilla.cli.chat import gateway_runtime as _target

sys.modules[__name__] = _target
