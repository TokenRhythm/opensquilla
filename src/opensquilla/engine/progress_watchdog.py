"""Observe-first progress watchdog for agent turns."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ProgressAction = Literal["observe", "warn", "block"]


@dataclass(frozen=True)
class ProgressObservation:
    iteration: int
    provider_call_count: int = 0
    successful_tool_result: bool = False
    successful_source_context_tool_result: bool = False
    successful_execution_tool_result: bool = False
    source_context_signature: str | None = None
    user_visible_output: bool = False
    artifact_completed: bool = False
    workspace_change_likely_required: bool = False
    workspace_write_count: int = 0
    changed_receipt_count: int = 0
    noop_receipt_count: int = 0
    partial_receipt_count: int = 0
    scratch_write_count: int = 0
    post_write_focused_verification_observed: bool = False
    tool_error_signature: str | None = None
    provider_failure_signature: str | None = None
    failure_anchor_signature: str | None = None
    failure_anchor_summary: str | None = None


@dataclass(frozen=True)
class ProgressDecision:
    action: ProgressAction
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProgressWatchdog:
    """Detect repeated no-progress loops without owning the main turn loop."""

    def __init__(
        self,
        *,
        repeated_tool_error_threshold: int = 3,
        repeated_provider_failure_threshold: int = 2,
        repeated_failure_anchor_threshold: int = 3,
        source_context_without_write_threshold: int = 8,
        source_context_exploration_without_write_threshold: int = 12,
        source_context_after_write_threshold: int = 8,
        tool_activity_without_write_threshold: int = 8,
        verified_post_write_activity_threshold: int = 3,
        observe_only: bool = True,
    ) -> None:
        self.repeated_tool_error_threshold = repeated_tool_error_threshold
        self.repeated_provider_failure_threshold = repeated_provider_failure_threshold
        self.repeated_failure_anchor_threshold = repeated_failure_anchor_threshold
        self.source_context_without_write_threshold = source_context_without_write_threshold
        self.source_context_exploration_without_write_threshold = (
            source_context_exploration_without_write_threshold
        )
        self.source_context_after_write_threshold = source_context_after_write_threshold
        self.tool_activity_without_write_threshold = tool_activity_without_write_threshold
        self.verified_post_write_activity_threshold = verified_post_write_activity_threshold
        self.observe_only = observe_only
        self._last_tool_error: str | None = None
        self._tool_error_count = 0
        self._last_provider_failure: str | None = None
        self._provider_failure_count = 0
        self._last_failure_anchor: str | None = None
        self._failure_anchor_count = 0
        self._failure_anchor_warned_at = 0
        self._last_workspace_progress_count = 0
        self._last_source_context_without_write_signature: str | None = None
        self._source_context_without_write_count = 0
        self._source_context_without_write_warned_at = 0
        self._source_context_exploration_without_write_count = 0
        self._source_context_exploration_without_write_warned_at = 0
        self._source_context_after_write_count = 0
        self._source_context_after_write_warned_at = 0
        self._tool_activity_without_write_count = 0
        self._tool_activity_without_write_warned_at = 0
        self._verified_post_write_activity_count = 0
        self._verified_post_write_activity_warned_at = 0

    def observe(self, observation: ProgressObservation) -> ProgressDecision:
        workspace_progress_observed = self._sync_workspace_progress_count(
            _workspace_progress_count(observation)
        )

        source_context_decision = self._record_source_context_without_write(observation)
        if source_context_decision is not None:
            return source_context_decision

        source_context_decision = self._record_source_context_exploration_without_write(observation)
        if source_context_decision is not None:
            return source_context_decision

        source_context_decision = self._record_source_context_after_write(observation)
        if source_context_decision is not None:
            return source_context_decision

        failure_anchor_decision = self._record_repeated_failure_anchor_without_write(observation)
        if failure_anchor_decision is not None:
            return failure_anchor_decision

        tool_activity_decision = self._record_tool_activity_without_write(observation)
        if tool_activity_decision is not None:
            return tool_activity_decision

        verified_post_write_decision = self._record_verified_post_write_activity(observation)
        if verified_post_write_decision is not None:
            return verified_post_write_decision

        if _has_progress(
            observation,
            workspace_progress_observed=workspace_progress_observed,
        ):
            self._reset_progress_sensitive_counts()
            return ProgressDecision("observe", "progress")

        tool_decision = self._record_repeated_tool_error(observation)
        if tool_decision is not None:
            return tool_decision

        provider_decision = self._record_repeated_provider_failure(observation)
        if provider_decision is not None:
            return provider_decision

        return ProgressDecision("observe", "no_signal")

    def _record_source_context_without_write(
        self, observation: ProgressObservation
    ) -> ProgressDecision | None:
        if observation.artifact_completed:
            self._reset_source_context_without_write_count()
            return None
        if not observation.successful_source_context_tool_result:
            return None
        if _workspace_progress_count(observation) > 0:
            return None

        signature = observation.source_context_signature or "<unknown>"
        if signature == self._last_source_context_without_write_signature:
            self._source_context_without_write_count += 1
        else:
            self._last_source_context_without_write_signature = signature
            self._source_context_without_write_count = 1
            self._source_context_without_write_warned_at = 0
        threshold = max(0, int(self.source_context_without_write_threshold or 0))
        if threshold <= 0 or self._source_context_without_write_count < threshold:
            return None
        if (
            self._source_context_without_write_warned_at
            and self._source_context_without_write_count % threshold != 0
        ):
            return None
        self._source_context_without_write_warned_at = self._source_context_without_write_count
        return self._decision(
            "source_context_without_workspace_write",
            {
                "count": self._source_context_without_write_count,
                "threshold": threshold,
                "iteration": observation.iteration,
                "provider_call_count": observation.provider_call_count,
                "source_context_signature": signature,
                "workspace_change_likely_required": (
                    observation.workspace_change_likely_required
                ),
            },
        )

    def _record_source_context_exploration_without_write(
        self, observation: ProgressObservation
    ) -> ProgressDecision | None:
        if observation.artifact_completed:
            self._reset_source_context_exploration_without_write_count()
            return None
        if not observation.successful_source_context_tool_result:
            return None
        if _workspace_progress_count(observation) > 0:
            self._reset_source_context_exploration_without_write_count()
            return None

        self._source_context_exploration_without_write_count += 1
        threshold = max(
            0,
            int(self.source_context_exploration_without_write_threshold or 0),
        )
        if threshold <= 0 or self._source_context_exploration_without_write_count < threshold:
            return None
        if (
            self._source_context_exploration_without_write_warned_at
            and self._source_context_exploration_without_write_count % threshold != 0
        ):
            return None
        self._source_context_exploration_without_write_warned_at = (
            self._source_context_exploration_without_write_count
        )
        return self._decision(
            "source_context_exploration_without_workspace_write",
            {
                "count": self._source_context_exploration_without_write_count,
                "threshold": threshold,
                "iteration": observation.iteration,
                "provider_call_count": observation.provider_call_count,
                "source_context_signature": (observation.source_context_signature or "<unknown>"),
                "workspace_change_likely_required": (
                    observation.workspace_change_likely_required
                ),
            },
        )

    def _record_source_context_after_write(
        self, observation: ProgressObservation
    ) -> ProgressDecision | None:
        if observation.artifact_completed:
            self._reset_source_context_after_write_count()
            return None
        if _workspace_progress_count(observation) <= 0:
            self._reset_source_context_after_write_count()
            return None
        if not observation.successful_source_context_tool_result:
            return None

        self._source_context_after_write_count += 1
        threshold = max(0, int(self.source_context_after_write_threshold or 0))
        if threshold <= 0 or self._source_context_after_write_count < threshold:
            return None
        if (
            self._source_context_after_write_warned_at
            and self._source_context_after_write_count % threshold != 0
        ):
            return None
        self._source_context_after_write_warned_at = self._source_context_after_write_count
        return self._decision(
            "source_context_after_workspace_write",
            {
                "count": self._source_context_after_write_count,
                "threshold": threshold,
                "iteration": observation.iteration,
                "provider_call_count": observation.provider_call_count,
                "workspace_write_count": observation.workspace_write_count,
            },
        )

    def _record_repeated_failure_anchor_without_write(
        self, observation: ProgressObservation
    ) -> ProgressDecision | None:
        signature = observation.failure_anchor_signature
        if not signature:
            return None
        if signature == self._last_failure_anchor:
            self._failure_anchor_count += 1
        else:
            self._last_failure_anchor = signature
            self._failure_anchor_count = 1
            self._failure_anchor_warned_at = 0

        threshold = max(0, int(self.repeated_failure_anchor_threshold or 0))
        if threshold <= 0 or self._failure_anchor_count < threshold:
            return None
        if self._failure_anchor_warned_at and self._failure_anchor_count % threshold != 0:
            return None
        self._failure_anchor_warned_at = self._failure_anchor_count
        return self._decision(
            "repeated_failure_anchor_without_workspace_write",
            {
                "signature": signature,
                "count": self._failure_anchor_count,
                "threshold": threshold,
                "iteration": observation.iteration,
                "provider_call_count": observation.provider_call_count,
                "workspace_write_count": observation.workspace_write_count,
                "failure_anchor_summary": observation.failure_anchor_summary or "",
                "workspace_change_likely_required": (
                    observation.workspace_change_likely_required
                ),
            },
        )

    def _record_tool_activity_without_write(
        self, observation: ProgressObservation
    ) -> ProgressDecision | None:
        if observation.artifact_completed:
            self._reset_tool_activity_without_write_count()
            return None
        if _workspace_progress_count(observation) > 0:
            self._reset_tool_activity_without_write_count()
            return None
        if not observation.successful_tool_result:
            return None
        if not observation.successful_execution_tool_result and (
            observation.scratch_write_count <= 0
        ):
            return None

        self._tool_activity_without_write_count += 1
        threshold = max(0, int(self.tool_activity_without_write_threshold or 0))
        if threshold <= 0 or self._tool_activity_without_write_count < threshold:
            return None
        if (
            self._tool_activity_without_write_warned_at
            and self._tool_activity_without_write_count % threshold != 0
        ):
            return None
        self._tool_activity_without_write_warned_at = self._tool_activity_without_write_count
        return self._decision(
            "tool_activity_without_workspace_write",
            {
                "count": self._tool_activity_without_write_count,
                "threshold": threshold,
                "iteration": observation.iteration,
                "provider_call_count": observation.provider_call_count,
                "scratch_write_count": observation.scratch_write_count,
                "successful_execution_tool_result": (observation.successful_execution_tool_result),
                "workspace_change_likely_required": (
                    observation.workspace_change_likely_required
                ),
            },
        )

    def _record_verified_post_write_activity(
        self, observation: ProgressObservation
    ) -> ProgressDecision | None:
        if observation.artifact_completed:
            self._reset_verified_post_write_activity_count()
            return None
        if _workspace_progress_count(observation) <= 0:
            self._reset_verified_post_write_activity_count()
            return None
        if not observation.post_write_focused_verification_observed:
            self._reset_verified_post_write_activity_count()
            return None
        if not observation.successful_tool_result:
            return None
        if not (
            observation.successful_execution_tool_result
            or observation.successful_source_context_tool_result
        ):
            return None

        self._verified_post_write_activity_count += 1
        threshold = max(0, int(self.verified_post_write_activity_threshold or 0))
        if threshold <= 0 or self._verified_post_write_activity_count < threshold:
            return None
        if (
            self._verified_post_write_activity_warned_at
            and self._verified_post_write_activity_count % threshold != 0
        ):
            return None
        self._verified_post_write_activity_warned_at = self._verified_post_write_activity_count
        return self._decision(
            "verified_workspace_diff_continued_tool_activity",
            {
                "count": self._verified_post_write_activity_count,
                "threshold": threshold,
                "iteration": observation.iteration,
                "provider_call_count": observation.provider_call_count,
                "workspace_write_count": observation.workspace_write_count,
                "changed_receipt_count": observation.changed_receipt_count,
                "noop_receipt_count": observation.noop_receipt_count,
                "partial_receipt_count": observation.partial_receipt_count,
                "successful_execution_tool_result": (observation.successful_execution_tool_result),
                "successful_source_context_tool_result": (
                    observation.successful_source_context_tool_result
                ),
            },
        )

    def _sync_workspace_progress_count(self, workspace_progress_count: int) -> bool:
        if workspace_progress_count > self._last_workspace_progress_count:
            self._last_workspace_progress_count = workspace_progress_count
            self._reset_workspace_dependent_counts()
            return True
        if workspace_progress_count < self._last_workspace_progress_count:
            self._last_workspace_progress_count = workspace_progress_count
            self._reset_workspace_dependent_counts()
        return False

    def _reset_workspace_dependent_counts(self) -> None:
        self._reset_source_context_without_write_count()
        self._reset_source_context_exploration_without_write_count()
        self._reset_source_context_after_write_count()
        self._reset_failure_anchor_count()
        self._reset_tool_activity_without_write_count()
        self._reset_verified_post_write_activity_count()

    def _reset_source_context_without_write_count(self) -> None:
        self._last_source_context_without_write_signature = None
        self._source_context_without_write_count = 0
        self._source_context_without_write_warned_at = 0

    def _reset_source_context_exploration_without_write_count(self) -> None:
        self._source_context_exploration_without_write_count = 0
        self._source_context_exploration_without_write_warned_at = 0

    def _reset_source_context_after_write_count(self) -> None:
        self._source_context_after_write_count = 0
        self._source_context_after_write_warned_at = 0

    def _reset_failure_anchor_count(self) -> None:
        self._last_failure_anchor = None
        self._failure_anchor_count = 0
        self._failure_anchor_warned_at = 0

    def _reset_tool_activity_without_write_count(self) -> None:
        self._tool_activity_without_write_count = 0
        self._tool_activity_without_write_warned_at = 0

    def _reset_verified_post_write_activity_count(self) -> None:
        self._verified_post_write_activity_count = 0
        self._verified_post_write_activity_warned_at = 0

    def _record_repeated_tool_error(
        self, observation: ProgressObservation
    ) -> ProgressDecision | None:
        signature = observation.tool_error_signature
        if not signature:
            return None
        if signature == self._last_tool_error:
            self._tool_error_count += 1
        else:
            self._last_tool_error = signature
            self._tool_error_count = 1
        if self._tool_error_count < self.repeated_tool_error_threshold:
            return None
        return self._decision(
            "repeated_tool_error",
            self._decision_details(observation, signature, self._tool_error_count),
        )

    def _record_repeated_provider_failure(
        self, observation: ProgressObservation
    ) -> ProgressDecision | None:
        signature = observation.provider_failure_signature
        if not signature:
            return None
        if signature == self._last_provider_failure:
            self._provider_failure_count += 1
        else:
            self._last_provider_failure = signature
            self._provider_failure_count = 1
        if self._provider_failure_count < self.repeated_provider_failure_threshold:
            return None
        return self._decision(
            "repeated_provider_failure",
            self._decision_details(observation, signature, self._provider_failure_count),
        )

    def _decision_details(
        self,
        observation: ProgressObservation,
        signature: str,
        count: int,
    ) -> dict[str, Any]:
        return {
            "signature": signature,
            "count": count,
            "iteration": observation.iteration,
            "provider_call_count": observation.provider_call_count,
        }

    def _decision(self, reason: str, details: dict[str, Any]) -> ProgressDecision:
        if self.observe_only:
            return ProgressDecision("warn", reason, details)
        return ProgressDecision("block", reason, details)

    def _reset_progress_sensitive_counts(self) -> None:
        self._last_tool_error = None
        self._tool_error_count = 0
        self._last_provider_failure = None
        self._provider_failure_count = 0


def _has_progress(
    observation: ProgressObservation,
    *,
    workspace_progress_observed: bool,
) -> bool:
    return (
        workspace_progress_observed
        or observation.successful_tool_result
        or observation.user_visible_output
        or observation.artifact_completed
    )


def _workspace_progress_count(observation: ProgressObservation) -> int:
    changed_receipts = max(0, int(observation.changed_receipt_count or 0))
    if _workspace_receipt_count(observation) > 0:
        return changed_receipts
    return max(0, int(observation.workspace_write_count or 0))


def _workspace_receipt_count(observation: ProgressObservation) -> int:
    return (
        max(0, int(observation.changed_receipt_count or 0))
        + max(0, int(observation.noop_receipt_count or 0))
        + max(0, int(observation.partial_receipt_count or 0))
    )
