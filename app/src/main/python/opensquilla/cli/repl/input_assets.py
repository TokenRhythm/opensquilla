"""Compatibility alias for the shared chat input asset helpers."""

from __future__ import annotations

import sys

from opensquilla.cli.chat import input_assets as _target

sys.modules[__name__] = _target
