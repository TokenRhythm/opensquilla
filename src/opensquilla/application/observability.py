"""Transport-neutral runtime observability and readiness use cases."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class RouterLearningQuery:
    agent_id: str = "main"


@dataclass(frozen=True, slots=True)
class LogTailQuery:
    cursor: int = 0
    limit: int = 100
    level: str | None = None


@dataclass(frozen=True, slots=True)
class ReadinessQuery:
    agent_id: str = "main"
    deep: bool = True
    probe_providers: bool = False


type ReadinessSeverity = Literal["error", "warn", "info", "ok"]
type ReadinessImpact = Literal["blocks_ready", "degrades", "optional", "none"]


@dataclass(frozen=True, slots=True)
class ReadinessFixStep:
    label: str
    command: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ReadinessFinding:
    id: str
    severity: ReadinessSeverity
    surface: str
    title: str
    detail: str
    readiness_impact: ReadinessImpact | None = None
    evidence: Mapping[str, Any] | None = None
    fix_steps: tuple[ReadinessFixStep, ...] = ()
    restart_required: bool = False


class RuntimeStatusPort(Protocol):
    async def snapshot(self) -> Mapping[str, Any]: ...


class RouterLearningStatusPort(Protocol):
    async def snapshot(self, query: RouterLearningQuery) -> Mapping[str, Any]: ...


class LogReaderPort(Protocol):
    async def status(self) -> Mapping[str, Any]: ...

    async def tail(self, query: LogTailQuery) -> Mapping[str, Any]: ...


class ReadinessDataPort(Protocol):
    async def provider(self, query: ReadinessQuery) -> Mapping[str, Any]: ...

    async def logs(self, query: ReadinessQuery) -> Mapping[str, Any]: ...

    async def memory(self, query: ReadinessQuery) -> Mapping[str, Any]: ...

    async def channels(self, query: ReadinessQuery) -> Mapping[str, Any]: ...

    async def sandbox(self, query: ReadinessQuery) -> Mapping[str, Any]: ...

    async def router(self, query: ReadinessQuery) -> Mapping[str, Any]: ...

    async def squilla_router(self, query: ReadinessQuery) -> Mapping[str, Any]: ...

    async def memory_embedding(self, query: ReadinessQuery) -> Mapping[str, Any]: ...

    async def search(self, query: ReadinessQuery) -> Mapping[str, Any]: ...

    async def image_generation(self, query: ReadinessQuery) -> Mapping[str, Any]: ...

    async def llm_ensemble(self, query: ReadinessQuery) -> Mapping[str, Any]: ...


class ReadinessEvaluationPort(Protocol):
    def normalize_agent_id(self, value: str) -> str: ...

    def evaluate(
        self, surface: str, payload: Mapping[str, Any]
    ) -> Sequence[ReadinessFinding]: ...

    def build_report(
        self,
        findings: Sequence[ReadinessFinding],
        *,
        config_path: str | None,
    ) -> dict[str, Any]: ...


class RuntimeStatus:
    def __init__(self, port: RuntimeStatusPort) -> None:
        self._port = port

    async def read(self) -> dict[str, Any]:
        return dict(await self._port.snapshot())


class RouterLearningStatus:
    def __init__(self, port: RouterLearningStatusPort) -> None:
        self._port = port

    async def read(self, query: RouterLearningQuery) -> dict[str, Any]:
        agent_id = str(query.agent_id or "main").strip() or "main"
        return dict(await self._port.snapshot(RouterLearningQuery(agent_id)))


class LogReader:
    def __init__(self, port: LogReaderPort) -> None:
        self._port = port

    async def status(self) -> dict[str, Any]:
        return dict(await self._port.status())

    async def tail(self, query: LogTailQuery) -> dict[str, Any]:
        if query.cursor < 0:
            raise ValueError("cursor must be non-negative")
        if query.limit < 1:
            raise ValueError("limit must be positive")
        bounded = replace(
            query,
            limit=min(query.limit, 1000),
            level=(query.level or "").strip().upper() or None,
        )
        return dict(await self._port.tail(bounded))


@dataclass(frozen=True, slots=True)
class _ReadinessCollection:
    surface: str
    collect: Callable[[ReadinessQuery], Awaitable[Mapping[str, Any]]]


_COLLECTION_INSPECT_COMMANDS = {
    "provider": "opensquilla providers status --json",
    "logs": "opensquilla diagnostics status",
    "memory": "opensquilla memory status --deep --json",
    "channels": "opensquilla channels status --json",
    "sandbox": "opensquilla sandbox status --json",
    "router": "opensquilla diagnostics status",
    "squilla_router": "opensquilla diagnostics status",
    "memory_embedding": "opensquilla memory status --deep --json",
    "search": "opensquilla search status --json",
    "image_generation": "opensquilla onboard status --json",
    "llm_ensemble": "opensquilla diagnostics status",
}
_READINESS_CRITICAL_COLLECTIONS = {"provider"}


class ReadinessDiagnostics:
    """Aggregate independent readiness projections with partial-failure isolation."""

    def __init__(
        self,
        port: ReadinessDataPort,
        evaluation: ReadinessEvaluationPort,
    ) -> None:
        self._port = port
        self._evaluation = evaluation

    async def assess(
        self,
        query: ReadinessQuery,
        *,
        connection_id: str,
        config_path: str | None = None,
    ) -> dict[str, Any]:
        normalized = replace(
            query,
            agent_id=self._evaluation.normalize_agent_id(str(query.agent_id or "main")),
            deep=bool(query.deep),
            probe_providers=bool(query.probe_providers),
        )
        findings: list[ReadinessFinding] = [
            ReadinessFinding(
                id="gateway.rpc.ready",
                severity="ok",
                surface="gateway",
                title="Gateway RPC ready",
                detail="The gateway accepted and handled doctor.status.",
                evidence={"connId": connection_id},
            )
        ]
        for collection in self._collections():
            findings.extend(await self._evaluate(collection, normalized))

        report = self._evaluation.build_report(findings, config_path=config_path)
        report["agentId"] = normalized.agent_id
        if config_path:
            report["configPath"] = config_path
        return report

    def _collections(self) -> Sequence[_ReadinessCollection]:
        return (
            _ReadinessCollection("provider", self._port.provider),
            _ReadinessCollection("logs", self._port.logs),
            _ReadinessCollection("memory", self._port.memory),
            _ReadinessCollection("channels", self._port.channels),
            _ReadinessCollection("sandbox", self._port.sandbox),
            _ReadinessCollection("router", self._port.router),
            _ReadinessCollection("squilla_router", self._port.squilla_router),
            _ReadinessCollection("memory_embedding", self._port.memory_embedding),
            _ReadinessCollection("search", self._port.search),
            _ReadinessCollection("image_generation", self._port.image_generation),
            _ReadinessCollection("llm_ensemble", self._port.llm_ensemble),
        )

    async def _evaluate(
        self,
        collection: _ReadinessCollection,
        query: ReadinessQuery,
    ) -> list[ReadinessFinding]:
        try:
            payload = await collection.collect(query)
            return list(self._evaluation.evaluate(collection.surface, payload))
        except Exception as exc:  # noqa: BLE001 - diagnostics isolate partial failures.
            return [self._collection_error(collection.surface, exc)]

    @staticmethod
    def _collection_error(surface: str, exc: Exception) -> ReadinessFinding:
        inspect_command = _COLLECTION_INSPECT_COMMANDS.get(surface)
        fix_steps = []
        if inspect_command:
            fix_steps.append(ReadinessFixStep(label=f"Inspect {surface}", command=inspect_command))
        if inspect_command != "opensquilla diagnostics status":
            fix_steps.append(
                ReadinessFixStep(
                    label="Inspect diagnostics", command="opensquilla diagnostics status"
                )
            )
        fix_steps.append(
            ReadinessFixStep(label="Restart gateway", command="opensquilla gateway restart")
        )
        severity: ReadinessSeverity = (
            "error" if surface in _READINESS_CRITICAL_COLLECTIONS else "warn"
        )
        return ReadinessFinding(
            id=f"{surface}.diagnostic.unavailable",
            severity=severity,
            surface=surface,
            title=f"{surface.title()} diagnostics unavailable",
            detail=f"{type(exc).__name__}: {exc}",
            evidence={"errorType": type(exc).__name__},
            fix_steps=tuple(fix_steps),
            restart_required=True,
        )


__all__ = [
    "LogReader",
    "LogReaderPort",
    "LogTailQuery",
    "ReadinessDataPort",
    "ReadinessDiagnostics",
    "ReadinessEvaluationPort",
    "ReadinessFinding",
    "ReadinessFixStep",
    "ReadinessQuery",
    "RouterLearningQuery",
    "RouterLearningStatus",
    "RouterLearningStatusPort",
    "RuntimeStatus",
    "RuntimeStatusPort",
]
