"""Transport-neutral ancillary conversation use cases.

These small interfaces replace the unrelated usage, command, feedback,
prompt-cache, and clarification methods previously exposed through one broad
conversation facade.  Runtime storage and services stay behind narrow Ports.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from opensquilla.session_key import canonicalize_session_key

type RouteFeedbackRating = Literal["up", "down", "neutral"]


@dataclass(frozen=True, slots=True)
class UsageQuery:
    session_key: str | None = None
    filters: Mapping[str, Any] | None = None


class UsageReportingPort(Protocol):
    async def status(self, query: UsageQuery) -> Mapping[str, Any]: ...

    async def query(self, query: UsageQuery) -> Mapping[str, Any]: ...

    async def cost_breakdown(self, query: UsageQuery) -> Mapping[str, Any]: ...


class UsageReporting:
    def __init__(self, port: UsageReportingPort) -> None:
        self._port = port

    async def status(self, query: UsageQuery = UsageQuery()) -> Mapping[str, Any]:
        return await self._port.status(self._normalize(query))

    async def query(self, query: UsageQuery) -> Mapping[str, Any]:
        return await self._port.query(self._normalize(query))

    async def cost_breakdown(self, query: UsageQuery = UsageQuery()) -> Mapping[str, Any]:
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
    async def list(self, query: CommandCatalogQuery) -> Mapping[str, Any]: ...


class CommandCatalog:
    def __init__(self, port: CommandCatalogPort) -> None:
        self._port = port

    async def list(self, query: CommandCatalogQuery) -> Mapping[str, Any]:
        surface = query.surface.strip()
        if not surface:
            raise ValueError("surface must be non-empty")
        return await self._port.list(replace(query, surface=surface))


@dataclass(frozen=True, slots=True)
class SubmitRouteFeedback:
    decision_id: str
    rating: RouteFeedbackRating


class RouteFeedbackPort(Protocol):
    async def submit(self, command: SubmitRouteFeedback) -> Mapping[str, Any]: ...


class RouteFeedback:
    def __init__(self, port: RouteFeedbackPort) -> None:
        self._port = port

    async def submit(self, command: SubmitRouteFeedback) -> Mapping[str, Any]:
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
    async def status(self, session_key: str) -> Mapping[str, Any]: ...

    async def set_policy(self, command: SetPromptCacheLease) -> Mapping[str, Any]: ...


class PromptCacheLease:
    def __init__(self, port: PromptCacheLeasePort, policy: PromptCachePolicy) -> None:
        self._port = port
        self._policy = policy

    async def status(self, session_key: str) -> Mapping[str, Any]:
        return await self._port.status(self._key(session_key))

    async def set_policy(self, command: SetPromptCacheLease) -> Mapping[str, Any]:
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
    async def submit(self, command: SubmitClarification) -> Mapping[str, Any]: ...


class ClarificationSubmission:
    def __init__(self, port: ClarificationSubmissionPort) -> None:
        self._port = port

    async def submit(self, command: SubmitClarification) -> Mapping[str, Any]:
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
    "ClarificationSubmission",
    "ClarificationSubmissionPort",
    "CommandCatalog",
    "CommandCatalogPort",
    "CommandCatalogQuery",
    "PromptCacheLease",
    "PromptCacheLeasePort",
    "PromptCachePolicy",
    "RouteFeedback",
    "RouteFeedbackPort",
    "RouteFeedbackRating",
    "SetPromptCacheLease",
    "SubmitClarification",
    "SubmitRouteFeedback",
    "UsageQuery",
    "UsageReporting",
    "UsageReportingPort",
]
