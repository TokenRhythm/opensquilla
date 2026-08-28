"""Channel → tool permission matrix.

Public surface: :func:`is_tool_allowed`, with the default matrix and override
semantics kept in this module.

Default policy:

* Channel kind ``webui`` allows all three tiers
  (SAFE + CONFIRM + ADMIN_ONLY).
* Channel kinds ``dm`` and ``group`` allow only SAFE + CONFIRM.
  An ``admin_only`` tool invoked from ``dm`` / ``group`` returns
  ``allowed=False`` with ``reason='admin_only_denied_in_dm'``
  (or ``'admin_only_denied_in_group'``).
* Unknown channel kinds are treated as ``dm`` for safety (fail closed).

Per-channel overrides: the process-global override registry lets ops
grant ``admin_only`` in a specific DM / group channel without widening
the global matrix. Use :func:`register_channel_override` keyed on a
stable ``channel_id`` (not the channel kind) to enable this.

Return shape: a :class:`PermissionDecision` dataclass rather than a bare
bool so callers can log or surface the reason verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from opensquilla.safety.tool_tiers import RiskTier, get_tier

CHANNEL_WEBUI: Final[str] = "webui"
CHANNEL_DM: Final[str] = "dm"
CHANNEL_GROUP: Final[str] = "group"

_DEFAULT_MATRIX: Final[dict[str, frozenset[RiskTier]]] = {
    CHANNEL_WEBUI: frozenset({RiskTier.SAFE, RiskTier.CONFIRM, RiskTier.ADMIN_ONLY}),
    CHANNEL_DM: frozenset({RiskTier.SAFE, RiskTier.CONFIRM}),
    CHANNEL_GROUP: frozenset({RiskTier.SAFE, RiskTier.CONFIRM}),
}

# channel_id → extra tiers granted beyond the default for that channel kind
_OVERRIDES: dict[str, frozenset[RiskTier]] = {}


@dataclass(frozen=True)
class PermissionDecision:
    """Structured permission result."""

    allowed: bool
    reason: str


@dataclass(frozen=True)
class Principal:
    """Minimal identity carrier for the matrix.

    ``role`` is the only field consulted today; ``'operator'`` grants
    admin-only tools everywhere. Additional fields can be added without
    breaking callers because the dataclass is keyword-only at call
    sites.
    """

    role: str = "user"
    channel_id: str | None = None


def _normalise_channel_kind(channel_kind: str) -> str:
    kind = (channel_kind or "").strip().lower()
    if kind in _DEFAULT_MATRIX:
        return kind
    return CHANNEL_DM  # fail closed: unknown channels behave like DM


def _allowed_tiers_for(channel_kind: str, channel_id: str | None) -> frozenset[RiskTier]:
    base = _DEFAULT_MATRIX[_normalise_channel_kind(channel_kind)]
    if channel_id and channel_id in _OVERRIDES:
        return base | _OVERRIDES[channel_id]
    return base


def is_tool_allowed(
    tool_name: str,
    channel_kind: str,
    principal: Principal | None = None,
) -> PermissionDecision:
    """Decide whether ``tool_name`` is invocable from ``channel_kind``.

    Rules:

    * Tier is resolved via :func:`opensquilla.safety.tool_tiers.get_tier`.
    * If the resolved tier is present in the channel's allowed tiers,
      allow.
    * If the tier is :attr:`RiskTier.ADMIN_ONLY` and the principal has
      ``role == 'operator'``, allow with reason ``operator_override``.
    * Otherwise deny with the structural reason string
      ``admin_only_denied_in_<channel_kind>`` or ``tier_denied``.
    """

    tier = get_tier(tool_name)
    normalised = _normalise_channel_kind(channel_kind)
    channel_id = principal.channel_id if principal else None
    allowed_tiers = _allowed_tiers_for(normalised, channel_id)

    if tier in allowed_tiers:
        return PermissionDecision(True, "tier_allowed")

    if tier is RiskTier.ADMIN_ONLY and principal is not None and principal.role == "operator":
        return PermissionDecision(True, "operator_override")

    if tier is RiskTier.ADMIN_ONLY:
        return PermissionDecision(False, f"admin_only_denied_in_{normalised}")

    return PermissionDecision(False, "tier_denied")


def register_channel_override(channel_id: str, extra_tiers: frozenset[RiskTier]) -> None:
    """Grant ``extra_tiers`` on the channel identified by ``channel_id``.

    Intended for operator-configured per-channel grants; call this from
    the gateway boot path after config load.
    """

    if not channel_id:
        raise ValueError("channel_id must be a non-empty str")
    _OVERRIDES[channel_id] = frozenset(extra_tiers)


def clear_channel_overrides() -> None:
    """Drop all per-channel overrides. Intended for tests only."""

    _OVERRIDES.clear()


__all__ = [
    "CHANNEL_DM",
    "CHANNEL_GROUP",
    "CHANNEL_WEBUI",
    "PermissionDecision",
    "Principal",
    "clear_channel_overrides",
    "is_tool_allowed",
    "register_channel_override",
]
