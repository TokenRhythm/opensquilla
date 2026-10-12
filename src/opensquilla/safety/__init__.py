"""Agent safety baseline.

Four modules form the public safety surface:

* :mod:`opensquilla.safety.injection_guard` — wrap untrusted content with
  ``<untrusted source='...'>...</untrusted>`` envelopes, escape XML, and
  detect tool-call refusals whose origin is traced to an untrusted block.
* :mod:`opensquilla.safety.tool_tiers` — ``RiskTier`` enum + declare/get tier
  API; hardcoded admin-only list for high-risk tools.
* :mod:`opensquilla.safety.permission_matrix` — ``is_tool_allowed`` decision
  function keyed on ``(tool_name, channel_kind, principal)`` with a
  default matrix and per-channel overrides.
* :mod:`opensquilla.safety.sandbox` — ``run_sandboxed`` subprocess runner with
  CPU/memory/wall/network limits via :mod:`resource`.

Import order: modules are side-effect-free; importing this package is safe
during engine/gateway boot.
"""

from __future__ import annotations

from opensquilla.safety import (
    injection_guard,
    permission_matrix,
    sandbox,
    tool_tiers,
)

__all__ = [
    "injection_guard",
    "permission_matrix",
    "sandbox",
    "tool_tiers",
]
