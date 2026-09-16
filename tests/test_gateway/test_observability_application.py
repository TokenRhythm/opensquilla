from __future__ import annotations

import pytest

from opensquilla.application.observability import (
    LogReader,
    LogTailQuery,
    ReadinessDiagnostics,
    ReadinessFinding,
    ReadinessQuery,
)


class _LogPort:
    def __init__(self) -> None:
        self.query: LogTailQuery | None = None

    async def status(self) -> dict[str, object]:
        return {"gateway_file_log": {"enabled": True}}

    async def tail(self, query: LogTailQuery) -> dict[str, object]:
        self.query = query
        return {"lines": ["ready"], "cursor": 5, "has_more": False}


class _ReadinessPort:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def _ready(self, surface: str) -> tuple[ReadinessFinding, ...]:
        self.calls.append(surface)
        return ()

    async def provider(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        self.calls.append("provider")
        raise RuntimeError("provider unavailable")

    async def logs(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        return await self._ready("logs")

    async def memory(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        return await self._ready("memory")

    async def channels(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        return await self._ready("channels")

    async def sandbox(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        return await self._ready("sandbox")

    async def router(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        return await self._ready("router")

    async def squilla_router(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        return await self._ready("squilla_router")

    async def memory_embedding(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        return await self._ready("memory_embedding")

    async def search(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        return await self._ready("search")

    async def image_generation(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        return await self._ready("image_generation")

    async def llm_ensemble(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        return await self._ready("llm_ensemble")


class _EvaluationPort:
    def normalize_agent_id(self, value: str) -> str:
        return value.strip().lower()

    def build_report(
        self,
        findings: tuple[ReadinessFinding, ...] | list[ReadinessFinding],
        *,
        config_path: str | None,
    ) -> dict[str, object]:
        return {
            "status": "action_required"
            if any(finding.severity == "error" for finding in findings)
            else "ready",
            "ready": not any(finding.severity == "error" for finding in findings),
            "findings": [
                {
                    "id": finding.id,
                    "severity": finding.severity,
                    "evidence": dict(finding.evidence or {}),
                    "fixSteps": [
                        {
                            "label": step.label,
                            "command": (
                                f"{step.command} --config {config_path}"
                                if step.command and config_path
                                else step.command
                            ),
                        }
                        for step in finding.fix_steps
                    ],
                }
                for finding in findings
            ],
        }


@pytest.mark.asyncio
async def test_log_reader_normalizes_tail_query_before_calling_port() -> None:
    port = _LogPort()

    result = await LogReader(port).tail(LogTailQuery(cursor=3, limit=5_000, level=" info "))

    assert result == {"lines": ["ready"], "cursor": 5, "has_more": False}
    assert port.query == LogTailQuery(cursor=3, limit=1_000, level="INFO")


@pytest.mark.asyncio
async def test_log_reader_rejects_invalid_cursor_before_calling_port() -> None:
    port = _LogPort()

    with pytest.raises(ValueError, match="cursor must be non-negative"):
        await LogReader(port).tail(LogTailQuery(cursor=-1))

    assert port.query is None


@pytest.mark.asyncio
async def test_readiness_diagnostics_isolates_collectors_in_stable_order() -> None:
    port = _ReadinessPort()

    report = await ReadinessDiagnostics(port, _EvaluationPort()).assess(
        ReadinessQuery(agent_id=" MAIN "),
        connection_id="conn-1",
        config_path="/tmp/opensquilla.toml",
    )

    assert port.calls == [
        "provider",
        "logs",
        "memory",
        "channels",
        "sandbox",
        "router",
        "squilla_router",
        "memory_embedding",
        "search",
        "image_generation",
        "llm_ensemble",
    ]
    assert report["agentId"] == "main"
    unavailable = next(
        finding
        for finding in report["findings"]
        if finding["id"] == "provider.diagnostic.unavailable"
    )
    assert unavailable["severity"] == "error"
    assert unavailable["evidence"] == {"errorType": "RuntimeError"}
    assert any(
        step.get("command") == "opensquilla providers status --json --config /tmp/opensquilla.toml"
        for step in unavailable["fixSteps"]
    )
