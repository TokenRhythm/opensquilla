"""Tool risk-tier declarations.

Every tool that goes through the dispatch pipeline has exactly one
:class:`RiskTier`. A fixed set of tool names are always
:attr:`RiskTier.ADMIN_ONLY` regardless of any :func:`declare_tier`
override:

* ``shell_exec`` / ``exec_command`` / ``background_process`` / ``process``
* ``file_write`` / ``write_file`` / ``edit_file`` / ``apply_patch``
  / ``execute_code`` / ``git_push``
* ``channel_send_as_admin``

Tier semantics (enforced upstream in the engine / dispatch layer):

* :attr:`RiskTier.SAFE` — auto-executes, no ACK gate.
* :attr:`RiskTier.CONFIRM` — blocks on ACK gate; resumes on ack.
* :attr:`RiskTier.ADMIN_ONLY` — rejects unless an operator-role
  principal is present.
* :attr:`RiskTier.WORKSPACE_AUTHORING` — available to an ordinary channel
  only when the trusted gateway has attested a managed sandbox workspace.

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
    WORKSPACE_AUTHORING = "workspace_authoring"


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
        "process",
        "file_write",
        "write_file",
        "edit_file",
        "apply_patch",
        "execute_code",
        "git_commit",
        "git_push",
        "channel_send_as_admin",
    }
)

_DECLARATIONS: dict[str, RiskTier] = {}

# These tools remain privileged by default, but may be admitted to an ordinary
# channel when the per-turn workspace attestation is true.  Keep the set here
# rather than changing ``HARDCODED_ADMIN_ONLY`` so existing callers that
# inspect that compatibility constant retain its meaning.
WORKSPACE_AUTHORING_TOOLS: Final[frozenset[str]] = frozenset(
    {"read_file", "write_file", "edit_file", "apply_patch", "execute_code"}
)


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


def tier_for_context(
    tool_name: str,
    *,
    workspace_authoring_attested: bool = False,
    default: RiskTier = RiskTier.CONFIRM,
) -> RiskTier:
    """Resolve a tool tier with the bounded workspace exception.

    The exception is an explicit input from the trusted ``ToolContext``; it is
    never inferred from tool arguments.  Without it, file/code mutation keeps
    the historical admin-only tier.
    """

    if workspace_authoring_attested and tool_name in WORKSPACE_AUTHORING_TOOLS:
        return RiskTier.WORKSPACE_AUTHORING
    return get_tier(tool_name, default)


def reset_declarations() -> None:
    """Clear all runtime-declared tiers. Intended for tests only."""

    _DECLARATIONS.clear()


__all__ = [
    "HARDCODED_ADMIN_ONLY",
    "RiskTier",
    "WORKSPACE_AUTHORING_TOOLS",
    "declare_tier",
    "get_tier",
    "reset_declarations",
    "tier_for_context",
]
