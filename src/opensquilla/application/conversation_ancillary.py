"""Transport-neutral ancillary conversation use cases.

These small interfaces replace the unrelated usage, command, feedback,
prompt-cache, and clarification methods previously exposed through one broad
conversation facade.  Runtime storage and services stay behind narrow Ports.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, TypedDict

from opensquilla.session_key import canonicalize_session_key

type RouteFeedbackRating = Literal["up", "down", "neutral"]


class UsageSessionProjection(TypedDict, total=False):
    sessionKey: str
    session: str
    key: str
    model: str | None
    inputTokens: int
    outputTokens: int
    costUsd: float
    billedCostUsd: float
    estimatedCostUsd: float
    costSource: str
    missingCostEntries: int
    cacheReadTokens: int
    cacheWriteTokens: int
    contextStatus: Mapping[str, object] | None
    modelBreakdown: list[Mapping[str, object]]
    deploymentBreakdown: list[Mapping[str, object]]


class UsageStatusResult(TypedDict, total=False):
    totalSessions: int
    activeSessions: int
    totalInputTokens: int
    totalOutputTokens: int
    totalTokens: int | float
    totalCostUsd: float
    totalCacheReadTokens: int
    totalCacheWriteTokens: int
    sessions: list[UsageSessionProjection]


class UsageQueryResult(TypedDict, total=False):
    schemaVersion: int
    source: str
    asOfMs: int
    range: Mapping[str, object]
    fxRatesNativePerUsd: Mapping[str, object]
    totals: Mapping[str, object]
    attributedTotals: Mapping[str, object]
    coverage: Mapping[str, object]
    legacyUnattributed: Mapping[str, object]
    missingCostEntries: int
    eventCount: int
    sessionCount: int
    days: list[Mapping[str, object]]
    models: list[Mapping[str, object]]
    sessions: list[Mapping[str, object]]


class UsageCostResult(TypedDict, total=False):
    breakdown: list[UsageSessionProjection]
    totalCostUsd: float


class CommandChoiceProjection(TypedDict, total=False):
    value: str
    description: str
    status: str
    missing_bins: list[str]
    missing_env: list[str]
    missing_env_any: list[list[str]]
    missing_skills: list[str]
    missing_capabilities: list[str]


class CommandExecutionProjection(TypedDict, total=False):
    kind: str
    action: str
    rpc_method: str


class CommandProjection(TypedDict, total=False):
    name: str
    usage: str
    description: str
    aliases: list[str]
    argument_choices: list[CommandChoiceProjection]
    execution: CommandExecutionProjection
    rpc_method: str
    category: str
    busy_policy: str
    presentation: str
    order: int
    visible_by_default: bool
    deprecated: bool


class CommandCatalogResult(TypedDict, total=False):
    surface: str
    commands: list[CommandProjection]


class RouteFeedbackResult(TypedDict, total=False):
    accepted: bool
    reason: str | None
    recorded: str | None


class PromptCacheLeaseResult(TypedDict, total=False):
    enabled: bool
    ttlSeconds: int
    intervalSeconds: int
    idleTimeoutSeconds: int
    idleExpiresAt: int | None
    state: str
    reason: str | None
    hasSnapshot: bool
    nextProbeAt: int | None
    lastProbeAt: int | None
    lastCacheHitTokens: int
    provider: str | None
    model: str | None


class ClarificationSubmissionResult(TypedDict, total=False):
    resolved: bool
    replayed: bool | None
    request_id: str
    ok: bool | None
    status: str | None
    accepted: bool | None
    sessionKey: str | None
    session_key: str | None
    key: str | None
    session_id: str | None
    user_message_id: str | None
    client_message_id: str | None
    task_id: str | None
    turn_id: str | None
    instant_accept: bool | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class UsageQuery:
    session_key: str | None = None
    filters: Mapping[str, Any] | None = None


class UsageReportingPort(Protocol):
    async def status(self, query: UsageQuery) -> UsageStatusResult: ...

    async def query(self, query: UsageQuery) -> UsageQueryResult: ...

    async def cost_breakdown(self, query: UsageQuery) -> UsageCostResult: ...


class UsageReporting:
    def __init__(self, port: UsageReportingPort) -> None:
        self._port = port

    async def status(self, query: UsageQuery = UsageQuery()) -> UsageStatusResult:
        return await self._port.status(self._normalize(query))

    async def query(self, query: UsageQuery) -> UsageQueryResult:
        return await self._port.query(self._normalize(query))

    async def cost_breakdown(self, query: UsageQuery = UsageQuery()) -> UsageCostResult:
        return await self._port.cost_breakdown(self._normalize(query))

    @staticmethod
    def _normalize(query: UsageQuery) -> UsageQuery:
        key = query.session_key
        if key is not None:
            key = canonicalize_session_key(key)
            if not key:
                raise ValueError("session_key must be non-empty")
        return replace(query, session_key=key)


@dataclass(frozen=True, slots=True)
class CommandCatalogQuery:
    surface: str


class CommandCatalogPort(Protocol):
    async def list(self, query: CommandCatalogQuery) -> CommandCatalogResult: ...


class CommandCatalog:
    def __init__(self, port: CommandCatalogPort) -> None:
        self._port = port

    async def list(self, query: CommandCatalogQuery) -> CommandCatalogResult:
        surface = query.surface.strip()
        if not surface:
            raise ValueError("surface must be non-empty")
        return await self._port.list(replace(query, surface=surface))


@dataclass(frozen=True, slots=True)
class SubmitRouteFeedback:
    decision_id: str
    rating: RouteFeedbackRating


class RouteFeedbackPort(Protocol):
    async def submit(self, command: SubmitRouteFeedback) -> RouteFeedbackResult: ...


class RouteFeedback:
    def __init__(self, port: RouteFeedbackPort) -> None:
        self._port = port

    async def submit(self, command: SubmitRouteFeedback) -> RouteFeedbackResult:
        decision_id = command.decision_id.strip()
        if not decision_id:
            raise ValueError("decision_id must be non-empty")
        if command.rating not in {"up", "down", "neutral"}:
            raise ValueError("rating must be up, down, or neutral")
        return await self._port.submit(replace(command, decision_id=decision_id))


@dataclass(frozen=True, slots=True)
class PromptCachePolicy:
    default_ttl_seconds: int
    minimum_ttl_seconds: int
    maximum_ttl_seconds: int
    default_idle_timeout_seconds: int
    minimum_idle_timeout_seconds: int
    maximum_idle_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class SetPromptCacheLease:
    session_key: str
    enabled: bool
    ttl_seconds: int | None = None
    idle_timeout_seconds: int | None = None


class PromptCacheLeasePort(Protocol):
    async def status(self, session_key: str) -> PromptCacheLeaseResult: ...

    async def set_policy(self, command: SetPromptCacheLease) -> PromptCacheLeaseResult: ...


class PromptCacheLease:
    def __init__(self, port: PromptCacheLeasePort, policy: PromptCachePolicy) -> None:
        self._port = port
        self._policy = policy

    async def status(self, session_key: str) -> PromptCacheLeaseResult:
        return await self._port.status(self._key(session_key))

    async def set_policy(self, command: SetPromptCacheLease) -> PromptCacheLeaseResult:
        key = self._key(command.session_key)
        ttl = command.ttl_seconds
        if ttl is None:
            ttl = self._policy.default_ttl_seconds
        minimum_idle = int(ttl * 0.8) + 1
        idle_timeout = command.idle_timeout_seconds
        if idle_timeout is None:
            idle_timeout = max(self._policy.default_idle_timeout_seconds, minimum_idle)
        if command.enabled and not (
            self._policy.minimum_ttl_seconds <= ttl <= self._policy.maximum_ttl_seconds
        ):
            raise ValueError("ttl_seconds is outside the supported range")
        if command.enabled and not (
            self._policy.minimum_idle_timeout_seconds
            <= idle_timeout
            <= self._policy.maximum_idle_timeout_seconds
        ):
            raise ValueError("idle_timeout_seconds is outside the supported range")
        if command.enabled and idle_timeout < minimum_idle:
            raise ValueError("idle_timeout_seconds must exceed the probe interval")
        return await self._port.set_policy(
            replace(
                command,
                session_key=key,
                ttl_seconds=ttl,
                idle_timeout_seconds=idle_timeout,
            )
        )

    @staticmethod
    def _key(value: str) -> str:
        key = canonicalize_session_key(value)
        if not key:
            raise ValueError("session_key must be non-empty")
        return key


@dataclass(frozen=True, slots=True)
class SubmitClarification:
    session_key: str
    fields: Mapping[str, Any]
    request_id: str | None = None
    run_id: str | None = None


class ClarificationSubmissionPort(Protocol):
    async def submit(self, command: SubmitClarification) -> ClarificationSubmissionResult: ...


class ClarificationSubmission:
    def __init__(self, port: ClarificationSubmissionPort) -> None:
        self._port = port

    async def submit(self, command: SubmitClarification) -> ClarificationSubmissionResult:
        key = canonicalize_session_key(command.session_key)
        if not key:
            raise ValueError("session_key must be non-empty")
        if not command.fields:
            raise ValueError("fields must be non-empty")
        request_id = command.request_id.strip() if command.request_id is not None else None
        if request_id == "":
            raise ValueError("request_id must be non-empty")
        run_id = command.run_id.strip() if command.run_id is not None else None
        return await self._port.submit(
            replace(command, session_key=key, request_id=request_id, run_id=run_id)
        )


__all__ = [
    "ClarificationSubmissionResult",
    "ClarificationSubmission",
    "ClarificationSubmissionPort",
    "CommandCatalogResult",
    "CommandCatalog",
    "CommandCatalogPort",
    "CommandCatalogQuery",
    "CommandChoiceProjection",
    "CommandExecutionProjection",
    "CommandProjection",
    "PromptCacheLease",
    "PromptCacheLeasePort",
    "PromptCachePolicy",
    "PromptCacheLeaseResult",
    "RouteFeedback",
    "RouteFeedbackPort",
    "RouteFeedbackRating",
    "RouteFeedbackResult",
    "SetPromptCacheLease",
    "SubmitClarification",
    "SubmitRouteFeedback",
    "UsageQuery",
    "UsageQueryResult",
    "UsageReporting",
    "UsageReportingPort",
    "UsageSessionProjection",
    "UsageStatusResult",
    "UsageCostResult",
]
