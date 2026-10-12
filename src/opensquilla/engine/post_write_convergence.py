"""Post-write convergence tracking for coding-agent turns."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

PostWriteConvergenceAction = Literal["observe", "warn", "finalize", "reset"]


@dataclass(frozen=True)
class PostWriteConvergenceObservation:
    iteration: int
    provider_call_count: int = 0
    workspace_write_count: int = 0
    changed_receipt_count: int = 0
    diff_fingerprint: str | None = None
    diff_paths: list[str] = field(default_factory=list)
    focused_verification_success_observed: bool = False
    continued_activity_after_verification: bool = False


@dataclass(frozen=True)
class PostWriteConvergenceDecision:
    action: PostWriteConvergenceAction
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PostWriteConvergenceTracker:
    """Detect stable post-verification diffs that keep consuming turns."""

    def __init__(
        self,
        *,
        warn_threshold: int = 3,
        finalize_after_warning: int = 3,
    ) -> None:
        self.warn_threshold = max(0, int(warn_threshold or 0))
        self.finalize_after_warning = max(0, int(finalize_after_warning or 0))
        self._diff_fingerprint: str | None = None
        self._stable_count = 0
        self._warned_at_count = 0
        self._finalized = False

    def observe(
        self,
        observation: PostWriteConvergenceObservation,
    ) -> PostWriteConvergenceDecision:
        if not self._eligible(observation):
            self._reset()
            return self._decision("observe", "not_eligible", observation)

        if self._diff_fingerprint and observation.diff_fingerprint != self._diff_fingerprint:
            previous = self._diff_fingerprint
            self._diff_fingerprint = observation.diff_fingerprint
            self._stable_count = 1
            self._warned_at_count = 0
            self._finalized = False
            return self._decision(
                "reset",
                "diff_fingerprint_changed",
                observation,
                previous_diff_fingerprint=previous,
            )

        self._diff_fingerprint = observation.diff_fingerprint
        self._stable_count += 1

        if self._should_finalize():
            self._finalized = True
            return self._decision(
                "finalize",
                "stable_verified_workspace_diff_finalization",
                observation,
            )

        if self._should_warn():
            self._warned_at_count = self._stable_count
            return self._decision(
                "warn",
                "stable_verified_workspace_diff_continued_activity",
                observation,
            )

        return self._decision("observe", "stable_verified_workspace_diff", observation)

    def _eligible(self, observation: PostWriteConvergenceObservation) -> bool:
        return bool(
            observation.changed_receipt_count > 0
            and observation.diff_fingerprint
            and observation.diff_paths
            and observation.focused_verification_success_observed
            and observation.continued_activity_after_verification
        )

    def _should_warn(self) -> bool:
        if self.warn_threshold <= 0:
            return False
        if self._stable_count < self.warn_threshold:
            return False
        return self._warned_at_count == 0

    def _should_finalize(self) -> bool:
        if self._finalized or self._warned_at_count <= 0:
            return False
        threshold = self._warned_at_count + self.finalize_after_warning
        return self.finalize_after_warning > 0 and self._stable_count >= threshold

    def _reset(self) -> None:
        self._diff_fingerprint = None
        self._stable_count = 0
        self._warned_at_count = 0
        self._finalized = False

    def _decision(
        self,
        action: PostWriteConvergenceAction,
        reason: str,
        observation: PostWriteConvergenceObservation,
        **extra: Any,
    ) -> PostWriteConvergenceDecision:
        details = {
            "iteration": observation.iteration,
            "provider_call_count": observation.provider_call_count,
            "workspace_write_count": observation.workspace_write_count,
            "changed_receipt_count": observation.changed_receipt_count,
            "diff_fingerprint": observation.diff_fingerprint,
            "diff_paths": list(observation.diff_paths),
            "stable_count": self._stable_count,
            "warn_threshold": self.warn_threshold,
            "finalize_after_warning": self.finalize_after_warning,
            "warned_at_count": self._warned_at_count,
            **extra,
        }
        return PostWriteConvergenceDecision(action, reason, details)
