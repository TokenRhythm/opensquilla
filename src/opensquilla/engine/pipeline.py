"""Pre-turn pipeline: ordered async steps that transform TurnContext before agent execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import structlog

from opensquilla.observability.decision_log import PipelineStepRecord, RoutingSource
from opensquilla.provider import ToolDefinition
from opensquilla.provider.protocol import LLMProvider

if TYPE_CHECKING:
    from opensquilla.provider.types import ProviderRequestCorrelation

log = structlog.get_logger(__name__)

TurnStep = Callable[["TurnContext"], Awaitable["TurnContext"]]


@dataclass
class TurnContext:
    """Mutable context passed through the pre-turn pipeline."""

    message: str
    session_key: str
    config: Any
    provider: LLMProvider | None
    model: str
    tool_defs: list[ToolDefinition]
    system_prompt: str | tuple[str, str]
    attachments: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    raw_message: str | None = None
    # Immutable catalog pinned at the provider/tools boundary. Selection
    # steps use this instead of probing the loader again mid-turn.
    skill_catalog: Any | None = None
    # Opaque transport-only identity. Keep this typed field out of metadata so
    # decision logs and persisted pipeline snapshots never serialize it.
    provider_request_correlation: ProviderRequestCorrelation | None = field(
        default=None,
        repr=False,
    )
    # PR3 (design §14): surface origin so PR4's clarify reply parser
    # can adapt its tolerance per surface. Defaults to "unknown" so
    # the gateway/CLI/channel adapters can set it post-construction.
    surface_kind: str = "unknown"  # "web" | "cli" | "channel:<adapter>" | "unknown"

    @property
    def semantic_message(self) -> str:
        """Raw user text for routing/relevance decisions, falling back to message."""
        return self.raw_message if self.raw_message is not None else self.message


async def run_pipeline(ctx: TurnContext, steps: list[TurnStep]) -> TurnContext:
    """Execute pipeline steps in order. Failures are logged and skipped (fail-open).

    For each step we append one :class:`PipelineStepRecord` to
    ``ctx.metadata["pipeline_steps"]``:

    * success path — ``applied=True`` plus any step-published hints
      (``routed_tier``, ``routing_source``, ``routing_confidence``,
      ``filtered_skill_ids``).
    * fail-open path — the step raised; we roll back ``ctx.metadata`` to a
      pre-step snapshot and record ``applied=False`` with
      ``fallback_reason=str(exc)``.
    * skipped-by-gate path — the step returned early and set
      ``ctx.metadata[f"{step.__name__}__applied"] = False``; we record
      ``applied=False`` with ``routing_source="none"``.
    """

    records: list[PipelineStepRecord] = ctx.metadata.setdefault("pipeline_steps", [])
    for step in steps:
        step_name = step.__name__
        snapshot_meta = dict(ctx.metadata)
        try:
            ctx = await step(ctx)
        except Exception as exc:
            log.warning("pipeline.step_failed", step=step_name, error=str(exc))
            # Fail-open: restore pre-step metadata so a partial delta cannot
            # poison downstream steps or the final DecisionEntry.
            ctx.metadata.clear()
            ctx.metadata.update(snapshot_meta)
            records = ctx.metadata.setdefault("pipeline_steps", records)
            records.append(
                PipelineStepRecord(
                    step_name=step_name,
                    applied=False,
                    routing_source="none",
                    fallback_reason=str(exc),
                )
            )
            continue

        applied = bool(ctx.metadata.get(f"{step_name}__applied", True))
        if step_name == "apply_squilla_router":
            routed_tier = ctx.metadata.get("routed_tier")
            routing_source = cast(RoutingSource, ctx.metadata.get("routing_source", "none"))
            confidence = ctx.metadata.get("routing_confidence")
            filtered_skill_ids = None
        elif step_name == "filter_skills":
            routed_tier = None
            routing_source = "none"
            confidence = None
            filtered_skill_ids = ctx.metadata.get("filtered_skill_ids")
        else:
            routed_tier = None
            routing_source = "none"
            confidence = None
            filtered_skill_ids = None

        records = ctx.metadata.setdefault("pipeline_steps", records)
        records.append(
            PipelineStepRecord(
                step_name=step_name,
                applied=applied,
                routed_tier=routed_tier,
                filtered_skill_ids=filtered_skill_ids,
                routing_source=routing_source,
                confidence=confidence,
                fallback_reason=None,
            )
        )
    return ctx
