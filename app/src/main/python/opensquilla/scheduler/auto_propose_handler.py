"""Scheduler handler bridge: drive ``auto_propose`` for one agent.

Mirrors the dream-handler factory pattern at
``opensquilla.scheduler.dream_handler`` — the heavy dependencies
(MetaOrchestrator + provider + tool_registry) are injected via a
``build_orchestrator(agent_id)`` factory so this module stays
decoupled from gateway wiring.

The returned handler:
  * honours ``OPENSQUILLA_AUTO_PROPOSE_DISABLED=1`` as a kill switch
  * consults ``enabled_predicate()`` at fire time (so config reload
    can disable a running schedule without restarting the gateway)
  * runs ``auto_propose`` and logs the resulting structured summary
  * NEVER raises — any uncaught exception lands in the summary as a
    ``failed`` HandlerResult; the scheduler's run-loop keeps going
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from opensquilla.scheduler.payloads import payload_agent_id
from opensquilla.scheduler.types import CronJob, HandlerResult
from opensquilla.skills.creator.auto_propose import (
    auto_propose,
    is_auto_propose_disabled,
)

if TYPE_CHECKING:
    from opensquilla.gateway.config import MetaSkillAutoProposeConfig
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.skills.meta.orchestrator import MetaOrchestrator

logger = logging.getLogger(__name__)

BuildOrchestratorFn = Callable[[str], "MetaOrchestrator"]
EnabledPredicateFn = Callable[[], bool]


def make_auto_propose_handler(
    *,
    build_orchestrator: BuildOrchestratorFn,
    skill_loader: SkillLoader,
    log_dir: Path,
    proposals_dir: Path,
    config: MetaSkillAutoProposeConfig,
    enabled_predicate: EnabledPredicateFn | None = None,
) -> Callable[[CronJob], Awaitable[HandlerResult]]:
    """Build a cron handler that runs the auto-propose pipeline."""

    async def handle_auto_propose(job: CronJob) -> HandlerResult:
        agent_id = payload_agent_id(job.payload) or "main"
        # Pre-flight kill switch: ``is_auto_propose_disabled`` is the
        # single source of truth shared with the dream callback and
        # manual creator paths. Checking here too lets us skip the
        # expensive orchestrator build, but the load-bearing guard
        # lives inside ``auto_propose`` itself.
        if is_auto_propose_disabled():
            logger.info(
                "auto_propose.skipped",
                extra={"agent_id": agent_id, "job_id": job.id, "reason": "kill_switch"},
            )
            return HandlerResult(
                summary="auto_propose skipped: kill_switch",
                delivery_status="skipped",
            )
        if enabled_predicate is not None and not enabled_predicate():
            logger.info(
                "auto_propose.skipped",
                extra={"agent_id": agent_id, "job_id": job.id, "reason": "disabled"},
            )
            return HandlerResult(
                summary="auto_propose skipped: disabled",
                delivery_status="skipped",
            )
        try:
            orchestrator = build_orchestrator(agent_id)
            result = await auto_propose(
                orchestrator=orchestrator,
                skill_loader=skill_loader,
                log_dir=log_dir,
                window_days=config.window_days,
                min_freq=config.min_freq,
                top_k=config.top_k,
                triggered_by="cron",
                proposals_dir=proposals_dir,
                auto_enable=bool(getattr(config, "auto_enable", False)),
                auto_enable_max_risk=str(
                    getattr(config, "auto_enable_max_risk", "low"),
                ),
            )
            summary = result.summary()
            logger.info(
                "auto_propose.run.complete",
                extra={
                    "agent_id": agent_id,
                    "summary": summary,
                    "proposal_ids": result.proposals_created,
                    "enabled_proposal_ids": result.proposals_enabled,
                    "auto_enable": result.auto_enable,
                    "skipped": result.skipped,
                    "errors": result.errors,
                },
            )
            return HandlerResult(summary=summary, delivery_status="delivered")
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "auto_propose.run.failed", extra={"agent_id": agent_id},
            )
            return HandlerResult(
                summary=f"auto_propose failed: {exc}",
                delivery_status="failed",
            )

    return handle_auto_propose


__all__ = ["make_auto_propose_handler"]
