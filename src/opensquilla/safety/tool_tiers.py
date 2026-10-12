"""Tool risk-tier declarations.

Every tool that goes through the dispatch pipeline has exactly one
:class:`RiskTier`. A fixed set of tool names are always
:attr:`RiskTier.ADMIN_ONLY` regardless of any :func:`declare_tier`
override:

* ``shell_exec`` / ``exec_command`` / ``background_process`` / ``execute_code``
* ``file_write`` / ``write_file`` / ``edit_file`` / ``apply_patch``
* ``git_push``
* ``channel_send_as_admin``

Tier semantics (enforced upstream in the engine / dispatch layer):

* :attr:`RiskTier.SAFE` — auto-executes, no ACK gate.
* :attr:`RiskTier.CONFIRM` — blocks on ACK gate; resumes on ack.
* :attr:`RiskTier.ADMIN_ONLY` — rejects unless an operator-role
  principal is present.

Default resolution: :func:`get_tier` returns :attr:`RiskTier.CONFIRM`
for any tool that has not been explicitly declared — this is the
fail-closed policy.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class RiskTier(StrEnum):
    """Coarse risk tier applied at dispatch time."""

    SAFE = "safe"
    CONFIRM = "confirm"
    ADMIN_ONLY = "admin_only"


# The tools whose tier is not negotiable. These names are enforced
# by `get_tier` even when `declare_tier` has been called with a lower
# tier — a defense against mis-declared contrib tools. ``execute_code``
# runs arbitrary Python (and can shell out via os.system/subprocess), so
# it is pinned alongside the other arbitrary-execution tools.
HARDCODED_ADMIN_ONLY: Final[frozenset[str]] = frozenset(
    {
        "shell_exec",
        "exec_command",
        "background_process",
        "execute_code",
        "file_write",
        "write_file",
        "edit_file",
        "apply_patch",
        "git_push",
        "channel_send_as_admin",
    }
)

_DECLARATIONS: dict[str, RiskTier] = {}


def declare_tier(tool_name: str, tier: RiskTier) -> None:
    """Declare ``tier`` as the risk classification for ``tool_name``.

    Subsequent :func:`get_tier` calls return ``tier`` unless
    ``tool_name`` is in :data:`HARDCODED_ADMIN_ONLY`, in which case the
    declaration is silently ignored (the hardcoded policy wins).
    """

    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("tool_name must be a non-empty str")
    if not isinstance(tier, RiskTier):
        raise TypeError("tier must be a RiskTier member")
    _DECLARATIONS[tool_name] = tier


def get_tier(tool_name: str, default: RiskTier = RiskTier.CONFIRM) -> RiskTier:
    """Return the risk tier for ``tool_name``.

    Resolution order:

    1. If ``tool_name`` is in :data:`HARDCODED_ADMIN_ONLY`, return
       :attr:`RiskTier.ADMIN_ONLY` unconditionally.
    2. If declared via :func:`declare_tier`, return the declaration.
    3. Otherwise, return ``default`` (``RiskTier.CONFIRM`` by
       contract — fail closed).
    """

    if tool_name in HARDCODED_ADMIN_ONLY:
        return RiskTier.ADMIN_ONLY
    declared = _DECLARATIONS.get(tool_name)
    if declared is not None:
        return declared
    return default


def reset_declarations() -> None:
    """Clear all runtime-declared tiers. Intended for tests only."""

    _DECLARATIONS.clear()


__all__ = [
    "HARDCODED_ADMIN_ONLY",
    "RiskTier",
    "declare_tier",
    "get_tier",
    "reset_declarations",
]
