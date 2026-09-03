from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from opensquilla.application.session_maintenance import (
    CompactSession,
    SessionCompactionDeadlineError,
    SessionCompactionFlushSafetyError,
    SessionCompactionMemoryAssessment,
    SessionCompactionResult,
)
from opensquilla.gateway.adapters.session_maintenance import (
    GatewaySessionMaintenanceAdapter,
)
from opensquilla.gateway.rpc.registry import RpcHandlerError


@dataclass
class _Application:
    commands: list[CompactSession] = field(default_factory=list)
    result: SessionCompactionResult = field(
        default_factory=lambda: SessionCompactionResult(
            session_key="canonical",
            compaction_id="compact-1",
            status="started",
            applied=False,
            context_window_tokens=8_192,
        )
    )
    error: Exception | None = None

    async def compact(self, command: CompactSession) -> SessionCompactionResult:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return self.result


def _adapter() -> tuple[GatewaySessionMaintenanceAdapter, _Application]:
    application = _Application()
    return (
        GatewaySessionMaintenanceAdapter(
            application,
            require_key=lambda params: str((params or {})["key"]),
        ),
        application,
    )


async def test_adapter_maps_wire_fields_to_typed_compaction_command() -> None:
    adapter, application = _adapter()

    response = await adapter.compact(
        {
            "key": "canonical",
            "wait": False,
            "contextWindowTokens": 8_192,
            "instructions": "Keep obligations.",
        }
    )

    assert application.commands == [
        CompactSession(
            session_key="canonical",
            wait=False,
            context_window_tokens=8_192,
            instructions="Keep obligations.",
        )
    ]
    assert response == {
        "key": "canonical",
        "compaction_id": "compact-1",
        "status": "started",
        "compacted": False,
        "applied": False,
        "durability": "none",
        "user_visible": True,
    }


async def test_adapter_accepts_legacy_context_window_alias() -> None:
    adapter, application = _adapter()

    await adapter.compact({"key": "canonical", "context_window_tokens": "4096"})

    assert application.commands[0].context_window_tokens == 4_096


@pytest.mark.parametrize("value", [True, 0, -1, "invalid"])
async def test_adapter_rejects_invalid_context_window_before_application(
    value: object,
) -> None:
    adapter, application = _adapter()

    with pytest.raises(RpcHandlerError) as raised:
        await adapter.compact({"key": "canonical", "contextWindowTokens": value})

    assert raised.value.code == "INVALID_PARAMS"
    assert application.commands == []


async def test_adapter_rejects_invalid_instructions_before_application() -> None:
    adapter, application = _adapter()

    with pytest.raises(RpcHandlerError) as raised:
        await adapter.compact({"key": "canonical", "instructions": 3})

    assert raised.value.code == "INVALID_PARAMS"
    assert application.commands == []


async def test_adapter_projects_terminal_domain_result() -> None:
    adapter, application = _adapter()
    application.result = SessionCompactionResult(
        session_key="canonical",
        compaction_id="compact-1",
        status="completed",
        applied=True,
        context_window_tokens=8_192,
        summary_len=12,
        summary_source="provider",
        tokens_before=100,
        tokens_after=40,
        remaining_budget_tokens=8_152,
        removed_count=4,
        kept_count=2,
        chunk_count=1,
        coverage_status="complete",
        missing_obligation_count=0,
        critical_carry_forward_count=1,
        state_kind="structured",
        quality_report={"score": 1},
        flush_receipt_status="flushed",
    )

    response = await adapter.compact({"key": "canonical"})

    assert response["compacted"] is True
    assert response["durability"] == "durable"
    assert response["summary_len"] == 12
    assert response["coverage_status"] == "complete"
    assert response["quality_report"] == {"score": 1}
    assert response["flush_receipt_status"] == "flushed"


async def test_adapter_maps_deadline_to_wire_error() -> None:
    adapter, application = _adapter()
    application.error = SessionCompactionDeadlineError(
        session_key="canonical",
        compaction_id="compact-1",
        phase="summarizing",
    )

    with pytest.raises(RpcHandlerError) as raised:
        await adapter.compact({"key": "canonical"})

    assert raised.value.code == "COMPACTION_TIMEOUT"
    assert raised.value.details["phase"] == "summarizing"


async def test_adapter_maps_flush_safety_to_wire_error() -> None:
    adapter, application = _adapter()
    application.error = SessionCompactionFlushSafetyError(
        session_key="canonical",
        session_id="session-1",
        receipt=None,
        receipt_status="missing",
        assessment=SessionCompactionMemoryAssessment(
            allows_destructive_compaction=False,
            safety_status="unsafe",
            semantic_status="missing",
        ),
    )

    with pytest.raises(RpcHandlerError) as raised:
        await adapter.compact({"key": "canonical"})

    assert raised.value.code == "CONTEXT_FLUSH_FAILED"
    assert raised.value.details["reason"] == (
        "destructive_manual_compact_requires_safe_flush"
    )
