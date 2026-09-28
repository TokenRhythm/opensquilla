from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.application.session_maintenance import (
    CompactSession,
    SessionCompactionDeadlineError,
    SessionCompactionResult,
    SessionCompactionSession,
)
from opensquilla.gateway.adapters.session_maintenance import (
    GatewaySessionMaintenanceAdapter,
    GatewaySessionMaintenancePorts,
)
from opensquilla.gateway.compaction_target import (
    GatewayCompactionTarget,
    GatewayConsumerBudget,
    build_gateway_compaction_budget,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc.registry import RpcContext, RpcHandlerError
from opensquilla.project_workspaces import project_path_key
from opensquilla.provider.selector import ProviderConfig
from opensquilla.session.models import ProjectWorkspace, SessionNode
from tests.helpers.image_bytes import image_bytes


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
        GatewaySessionMaintenanceAdapter(application),
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
    )

    response = await adapter.compact({"key": "canonical"})

    assert response["compacted"] is True
    assert response["durability"] == "durable"
    assert response["summary_len"] == 12
    assert response["coverage_status"] == "complete"
    assert response["quality_report"] == {"score": 1}


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


def test_manual_plan_keeps_generation_budget_without_fabricating_active_request() -> None:
    config = GatewayConfig(llm={
        "provider": "openai", "model": "synthetic-manual", "api_key": "synthetic-key",
        "context_window_tokens": 32_000, "max_tokens": 8192,
    })
    current = ProviderConfig(
        provider="openai", model="synthetic-manual", api_key="synthetic-key",
    )
    ports = GatewaySessionMaintenancePorts(RpcContext(
        conn_id="manual-generation", config=config, session_manager=SimpleNamespace(storage=None),
        provider_selector=SimpleNamespace(current_config=current),
    ))

    plan = ports.build_plan(None, None, "manual-generation", time.monotonic() + 120)

    compaction = plan.runtime_value.config
    assert compaction.request_context is None
    assert compaction.llm_plan.primary.max_generation_tokens == 8192
    assert compaction.llm_plan.primary.max_output_tokens == 1024


def test_manual_plan_uses_real_runner_prompt_and_tools_without_starting_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.runtime import TurnRunner
    from opensquilla.provider.openai import OpenAIProvider
    from opensquilla.provider.types import ToolDefinition

    config = GatewayConfig(llm={
        "provider": "openai", "model": "synthetic-manual", "api_key": "synthetic-key",
        "context_window_tokens": 32_000, "max_tokens": 1024, "thinking": "off",
    })
    current = ProviderConfig(
        provider="openai", model="synthetic-manual", api_key="synthetic-key",
    )
    runner = TurnRunner(provider_selector=None, config=config)
    prompt = "Synthetic session instructions. " * 200
    tool = ToolDefinition(
        name="synthetic_lookup", description="Synthetic lookup tool.",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    monkeypatch.setattr(runner, "_build_tools", lambda **_: ([tool], None))
    monkeypatch.setattr(runner, "_assemble_prompt", lambda *_, **__: prompt)
    monkeypatch.setattr(runner, "_extra_context_for_tool_context", lambda _: None)

    def unexpected_turn(*args, **kwargs):
        raise AssertionError("manual envelope preparation must not start a turn or call a model")

    monkeypatch.setattr(runner, "run", unexpected_turn)
    monkeypatch.setattr(OpenAIProvider, "chat", unexpected_turn)
    projected = []
    project = OpenAIProvider.project_final_request

    def capture_projection(self, *args, **kwargs):
        result = project(self, *args, **kwargs)
        projected.append(result.payload)
        return result

    monkeypatch.setattr(OpenAIProvider, "project_final_request", capture_projection)
    raw_session = SessionNode(
        session_key="agent:main:webchat:manual-envelope", session_id="session-envelope",
    )
    before = raw_session.model_dump()
    ports = GatewaySessionMaintenancePorts(RpcContext(
        conn_id="manual-envelope", config=config, session_manager=SimpleNamespace(storage=None),
        provider_selector=SimpleNamespace(current_config=current), turn_runner=runner,
    ))
    session = SessionCompactionSession(raw_session.session_id, "main", raw_session)
    plan = ports.build_plan(session, None, "manual-envelope", time.monotonic() + 120)
    shared = plan.runtime_value.config.budget
    fallback = build_gateway_compaction_budget(plan.runtime_value.budget)

    assert shared.physical_context_window_tokens == 32_000
    assert plan.context_window_tokens == shared.history_capacity_tokens
    assert 0 < shared.history_capacity_tokens < fallback.history_capacity_tokens
    assert 0 < shared.history_capacity_chars < fallback.history_capacity_chars
    assert shared.consumer_admission("complete checkpoint", []) is True
    assert any(prompt in json.dumps(payload) for payload in projected)
    assert any("synthetic_lookup" in json.dumps(payload) for payload in projected)
    assert raw_session.model_dump() == before


def test_manual_budget_uses_session_deployment_without_a_legacy_default_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = GatewayConfig(context_budget_tokens=100_000)
    raw_session = SimpleNamespace(session_key="agent:main:webchat:large-context")
    session = SessionCompactionSession("session-large", "main", raw_session)
    ports = GatewaySessionMaintenancePorts(RpcContext(
        conn_id="manual-budget", config=config, session_manager=SimpleNamespace(storage=None),
    ))
    resolved_sessions = []

    def resolve(_ctx, candidate):
        resolved_sessions.append(candidate)
        return GatewayConsumerBudget(
            context_window_tokens=1_000_000,
            physical_context_window_tokens=1_000_000,
            provider_request_max_chars=3_000_000,
        )

    monkeypatch.setattr(
        "opensquilla.gateway.adapters.session_maintenance.resolve_gateway_consumer_budget",
        resolve,
    )

    assert ports.resolve_context_window_tokens(session, None) == 1_000_000
    assert ports.resolve_context_window_tokens(session, 40_000) == 40_000
    assert ports.resolve_context_window_tokens(session, 2_000_000) == 1_000_000
    assert resolved_sessions == [raw_session, raw_session, raw_session]


@pytest.mark.parametrize("workspace_kind", ["agent", "project", "untrusted", "disabled"])
async def test_manual_compaction_image_paths_use_validated_session_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_kind: str
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    session = SessionNode(
        session_key="agent:main:webchat:example", session_id="real-session",
        workspace_id="project-1" if workspace_kind in {"project", "untrusted"} else None,
    )
    project = ProjectWorkspace(
        workspace_id="project-1", path=str(project_dir.resolve()),
        path_key=project_path_key(project_dir), display_name="Example",
        trusted_at=None if workspace_kind == "untrusted" else 1,
    )
    storage = SimpleNamespace(
        get_session=AsyncMock(return_value=session),
        get_project_workspace=AsyncMock(return_value=project),
    )
    config = SimpleNamespace(
        workspace_dir=str(tmp_path / "agent"),
        attachments=SimpleNamespace(
            media_root=str(tmp_path / "media"), persist_transcripts=workspace_kind != "disabled",
        ),
    )
    ports = GatewaySessionMaintenancePorts(RpcContext(
        conn_id="connection-1", config=config, session_manager=SimpleNamespace(storage=storage),
    ))
    monkeypatch.setattr(
        "opensquilla.gateway.adapters.session_maintenance.resolve_gateway_compaction_target",
        lambda *_: GatewayCompactionTarget(),
    )
    monkeypatch.setattr(
        ports, "_consumer_budget", lambda *_: GatewayConsumerBudget(context_window_tokens=8192),
    )
    loaded = await ports.load_session(session.session_key)
    plan = ports.build_plan(loaded, 8192, "compaction-1", time.monotonic() + 120)
    resolver = plan.runtime_value.config.attachment_path_resolver
    if workspace_kind in {"untrusted", "disabled"}:
        assert resolver is None
        assert not (tmp_path / "agent").exists()
        assert not (project_dir / ".opensquilla").exists()
        return

    assert resolver is not None
    payload = image_bytes("JPEG")
    path = resolver(
        {"mime": "image/jpeg", "name": "photo.jpg", "data": base64.b64encode(payload).decode()},
        "parent-session",
    )
    assert path is not None and "/real-session/" in path
    workspace = project_dir if workspace_kind == "project" else tmp_path / "agent"
    assert (workspace / path).read_bytes() == payload
    if workspace_kind == "project":
        assert not (tmp_path / "agent").exists()

    config.attachments.persist_transcripts = False
    disabled_plan = ports.build_plan(loaded, 8192, "compaction-2", time.monotonic() + 120)
    assert disabled_plan.runtime_value.config.attachment_path_resolver is None
    reloaded = await ports.load_session(session.session_key)
    reloaded_plan = ports.build_plan(reloaded, 8192, "compaction-3", time.monotonic() + 120)
    assert reloaded_plan.runtime_value.config.attachment_path_resolver is None
    assert session.session_id not in ports._attachment_workspace_dirs
