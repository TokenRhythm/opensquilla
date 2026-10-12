"""Runtime bridge for the auto-propose settings toggle.

The RPC layer (``exec.proposals.settings.{get,set}``) reads + mutates
the live ``MetaSkillAutoProposeConfig`` object that the cron handler's
``enabled_predicate`` consults, and adds / pauses per-agent cron jobs
when the operator flips ``enabled`` via the WebUI without restarting.

Boot owns the wiring; this module exposes the lookup so the RPC
handler doesn't need to grovel through ``ServiceContainer``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opensquilla.gateway.config import MetaSkillAutoProposeConfig


RegisterCronsFn = Callable[[], Awaitable[None]]
PauseCronsFn = Callable[[], Awaitable[None]]


@dataclass
class AutoProposeRuntime:
    """Wired by boot, consumed by ``exec.proposals.settings.*`` RPCs."""

    config: MetaSkillAutoProposeConfig
    home: Path
    register_crons: RegisterCronsFn
    pause_crons: PauseCronsFn


_runtime: AutoProposeRuntime | None = None


def register_runtime(rt: AutoProposeRuntime) -> None:
    """Boot installs the runtime once services are ready."""
    global _runtime
    _runtime = rt


def get_runtime() -> AutoProposeRuntime | None:
    """RPC + tests read the live runtime. ``None`` means the feature
    is unavailable (e.g. provider not configured)."""
    return _runtime


def reset_runtime() -> None:
    """Clear the module-level singleton during gateway shutdown."""
    global _runtime
    _runtime = None


def reset_runtime_for_test() -> None:
    """Test helper — clears the module-level singleton between cases."""
    reset_runtime()


__all__ = [
    "AutoProposeRuntime",
    "get_runtime",
    "register_runtime",
    "reset_runtime",
    "reset_runtime_for_test",
]
