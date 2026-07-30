"""Scheduler handler bridge: run Dream for a given agent.

The scheduler's `HandlerFn` signature is ``async def (job) -> HandlerResult``.
Dependencies (provider, workspace, memory config) are injected via a
``build_dream_fn(agent_id)`` factory so this module stays decoupled from
gateway wiring.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from opensquilla.scheduler.payloads import payload_agent_id
from opensquilla.scheduler.types import CronJob, HandlerResult

logger = logging.getLogger(__name__)

BuildDreamFn = Callable[[str], Any]
DreamGuardFn = Callable[[], str | None]


PostDreamHookFn = Callable[[str, str], Awaitable[None]]


def make_memory_dream_handler(
    build_dream: BuildDreamFn,
    should_skip: DreamGuardFn | None = None,
    post_dream_hook: PostDreamHookFn | None = None,
    usage_event_sink: Any | None = None,
) -> Callable[[CronJob], Awaitable[HandlerResult]]:
    """Factory: returns an async cron handler that runs Dream per agent.

    When ``post_dream_hook`` is provided it is invoked after the Dream
    completes successfully, with the same ``agent_id`` plus the Dream
    summary. Hook exceptions are logged but never poison the dream
    HandlerResult — observability must not turn a successful dream into a
    failed one.
    """

    async def handle_memory_dream(job: CronJob) -> HandlerResult:
        agent_id = payload_agent_id(job.payload) or "main"
        reason = "kill_switch" if os.getenv("OPENSQUILLA_MEMORY_DREAM_DISABLED") == "1" else None
        if reason is None and should_skip is not None:
            reason = should_skip()
        if reason:
            summary = f"dream skipped: {reason}"
            logger.info(
                "dream.run.skipped",
                extra={"agent_id": agent_id, "job_id": job.id, "reason": reason},
            )
            return HandlerResult(summary=summary, delivery_status="skipped")
        try:
            dream = build_dream(agent_id)
            usage_scope = None
            if usage_event_sink is not None:
                from opensquilla.engine.usage_accounting import (
                    UsageAccountingScope,
                    UsageExecutionContext,
                )

                execution_id = uuid.uuid4().hex
                usage_scope = UsageAccountingScope(
                    sink=usage_event_sink,
                    context=UsageExecutionContext(
                        execution_id=execution_id,
                        agent_run_id=execution_id,
                        turn_id=execution_id,
                        session_id=uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"opensquilla:system:memory-dream:{agent_id}",
                        ).hex,
                        agent_id=agent_id,
                        run_kind="memory_dream",
                    ),
                )
            from opensquilla.engine.usage_accounting import bind_usage_accounting_scope

            with bind_usage_accounting_scope(usage_scope):
                result = await dream.run()
            summary = (
                f"dream agent={agent_id} "
                f"processed={result.files_processed} "
                f"evidence={result.evidence_status} "
                f"apply={result.apply_status}"
            )
            logger.info(
                "dream.run.complete",
                extra={"agent_id": agent_id, "summary": summary},
            )
            if post_dream_hook is not None:
                try:
                    await post_dream_hook(agent_id, summary)
                except Exception:
                    logger.exception(
                        "dream.post_hook.failed",
                        extra={"agent_id": agent_id},
                    )
            return HandlerResult(summary=summary)
        except Exception as exc:
            logger.exception("dream.run.failed", extra={"agent_id": agent_id})
            return HandlerResult(summary=f"dream failed: {exc}")

    return handle_memory_dream
