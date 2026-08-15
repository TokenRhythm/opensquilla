from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from opensquilla.cli.agent_cmd import (
    AgentRunResult,
    _to_benchmark_transcript,
    run_agent_command,
    run_agent_once,
)
from opensquilla.engine.types import (
    AnswerGenerationResetEvent,
    ArtifactEvent,
    DoneEvent,
    TextDeltaEvent,
    ThinkingEvent,
)
from opensquilla.gateway.config import AgentEntryConfig, GatewayConfig, PermissionsConfig
from opensquilla.project_workspaces import (
    ProjectWorkspaceStateError,
    project_path_key,
)
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.types import CallerKind, InteractionMode


class _FakeSessionManager:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    async def get_or_create(self, session_key: str, agent_id: str = "main") -> object:
        return SimpleNamespace(session_key=session_key, agent_id=agent_id)

    async def append_message(self, session_key: str, role: str, content: str) -> None:
        self.messages.append((session_key, role, content))
        return None


class _FakeServices:
    def __init__(
        self,
        config: GatewayConfig,
        session_manager: Any | None = None,
    ) -> None:
        self.memory_sync_managers = {"main": object()}
        self.memory_retrievers = {"main": object()}
        self.turn_capture_services = {"main": object()}
        self.flush_service = object()
        self.model_catalog = object()
        self.provider_selector = object()
        self.tool_registry = None
        self.session_manager = session_manager or _FakeSessionManager()
        self.skill_loader = None
        self.usage_tracker = None
        self.config = config

    async def close(self) -> None:
        return None


def test_benchmark_transcript_preserves_tool_result_execution_status() -> None:
    status = {
        "version": 1,
        "status": "timeout",
        "exit_code": None,
        "timed_out": True,
        "truncated": False,
        "reason": "runtime_timeout",
        "source": "tool_runtime",
        "preservation_class": "diagnostic",
    }
    entry = SimpleNamespace(
        role="assistant",
        content="",
        created_at=None,
        tool_calls=[
            {
                "type": "tool_use",
                "tool_use_id": "call-1",
                "name": "exec_command",
                "input": {"cmd": "sleep"},
            },
            {
                "type": "tool_result",
                "tool_use_id": "call-1",
                "name": "exec_command",
                "result": "timeout",
                "is_error": True,
                "execution_status": status,
            },
        ],
    )

    transcript = _to_benchmark_transcript([entry])

    assert transcript[1]["message"]["isError"] is True
    assert transcript[1]["message"]["executionStatus"] == status


def test_benchmark_transcript_preserves_result_truncated() -> None:
    entry = SimpleNamespace(
        role="assistant",
        content="",
        created_at=None,
        tool_calls=[
            {
                "type": "tool_use",
                "tool_use_id": "call-1",
                "name": "exec_command",
                "input": {"cmd": "cat big.log"},
            },
            {
                "type": "tool_result",
                "tool_use_id": "call-1",
                "name": "exec_command",
                "result": "head of output ...",
                "is_error": False,
                "result_truncated": True,
                "result_original_chars": 48210,
            },
        ],
    )

    transcript = _to_benchmark_transcript([entry])

    assert transcript[1]["message"]["resultTruncated"] is True
    assert transcript[1]["message"]["resultOriginalChars"] == 48210


def test_benchmark_transcript_omits_truncation_fields_when_absent() -> None:
    entry = SimpleNamespace(
        role="assistant",
        content="",
        created_at=None,
        tool_calls=[
            {
                "type": "tool_use",
                "tool_use_id": "call-1",
                "name": "exec_command",
                "input": {"cmd": "true"},
            },
            {
                "type": "tool_result",
                "tool_use_id": "call-1",
                "name": "exec_command",
                "result": "",
                "is_error": False,
            },
        ],
    )

    transcript = _to_benchmark_transcript([entry])

    assert "resultTruncated" not in transcript[1]["message"]
    assert "resultOriginalChars" not in transcript[1]["message"]


@pytest.mark.asyncio
async def test_run_agent_once_uses_agent_registry_model_when_model_not_explicit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            captured["runner_config_model"] = kwargs["config"].llm.model

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["run_model"] = kwargs.get("model")
            captured["tool_context"] = kwargs.get("tool_context")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        captured["service_config_model"] = config.llm.model
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    root = tmp_path / "root"
    agent_workspace = tmp_path / "ops-workspace"
    cfg = GatewayConfig(
        workspace_dir=str(root),
        agents=[
            AgentEntryConfig(
                id="ops",
                model="agent/default",
                workspace=str(agent_workspace),
            )
        ],
    )
    result = await run_agent_once(message="hello", agent_id="ops", config=cfg)

    assert result.text == "ok"
    assert captured["service_config_model"] == "agent/default"
    assert captured["runner_config_model"] == "agent/default"
    assert captured["run_model"] == "agent/default"
    assert captured["tool_context"].workspace_dir == str(agent_workspace)


@pytest.mark.asyncio
async def test_run_agent_once_uses_configured_full_host_run_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            return None

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["tool_context"] = kwargs.get("tool_context")
            yield DoneEvent(text="ok")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    cfg = GatewayConfig(
        workspace_dir=str(tmp_path),
        sandbox=SandboxSettings(run_mode="full"),
    )

    result = await run_agent_once(message="hello", config=cfg)

    assert result.text == "ok"
    assert captured["tool_context"].run_mode == "full"
    assert captured["tool_context"].elevated == "full"


@pytest.mark.asyncio
async def test_run_agent_once_collects_artifact_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = {
        "id": "art-cli",
        "kind": "artifact_ref",
        "name": "report.txt",
        "mime": "text/plain",
        "size": 4,
        "sha256": "d" * 64,
        "session_id": "session-1",
        "session_key": "agent:main:main",
        "source": "publish_artifact",
        "created_at": "2026-05-06T12:00:00Z",
        "download_url": "/api/v1/artifacts/art-cli?sessionKey=agent%3Amain%3Amain",
        "store": "artifacts",
    }

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            return None

        async def run(self, message: str, session_key: str, **kwargs: Any):
            yield ArtifactEvent(**artifact)
            yield DoneEvent(text="ok")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    result = await run_agent_once(message="hello", config=GatewayConfig())

    assert result.artifacts == [
        {
            **{key: value for key, value in artifact.items() if key != "session_key"},
            "download_url": "/api/v1/artifacts/art-cli",
        }
    ]
    assert result.artifacts[0]["download_url"] == "/api/v1/artifacts/art-cli"


@pytest.mark.asyncio
async def test_run_agent_once_terminal_reset_replaces_partial_and_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_text = "The fixed model could not complete this answer."
    drained = False

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs: Any):
            nonlocal drained
            yield TextDeltaEvent(text="superseded partial")
            yield AnswerGenerationResetEvent(
                terminal=True,
                terminal_text_snapshot=terminal_text,
                terminal_error_message="The model provider rejected the request.",
                terminal_error_code="provider_bad_request",
                terminal_failure_kind="bad_request",
            )
            drained = True

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    monkeypatch.setattr(
        "opensquilla.gateway.build_turn_runner_from_services",
        lambda _services: FakeTurnRunner(),
    )

    result = await run_agent_once(message="hello", config=GatewayConfig())

    assert drained is True
    assert result.status == "error"
    assert result.text == terminal_text
    assert "superseded partial" not in result.text
    assert result.errors == [
        {
            "message": "The model provider rejected the request.",
            "code": "provider_bad_request",
        }
    ]


@pytest.mark.asyncio
async def test_run_agent_once_continues_when_stderr_event_sink_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.agent_event_stream import StderrAgentEventSink

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs: Any):
            yield ThinkingEvent(text="private")
            yield DoneEvent(text="ok")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    class BrokenStream:
        def __init__(self) -> None:
            self.write_calls = 0

        def write(self, _value: str) -> int:
            self.write_calls += 1
            raise BrokenPipeError("synthetic closed stderr")

        def flush(self) -> None:
            raise AssertionError("flush must not follow a failed write")

    stream = BrokenStream()
    sink = StderrAgentEventSink(stream=stream)  # type: ignore[arg-type]
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    monkeypatch.setattr(
        "opensquilla.gateway.build_turn_runner_from_services",
        lambda _services: FakeTurnRunner(),
    )

    result = await run_agent_once(
        message="hello",
        config=GatewayConfig(),
        event_sink=sink,
    )

    assert result.status == "ok"
    assert result.text == "ok"
    assert sink.active is False
    assert stream.write_calls == 2


def test_run_agent_command_json_includes_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact = {
        "id": "art-cli",
        "kind": "artifact_ref",
        "name": "report.txt",
        "mime": "text/plain",
        "size": 4,
        "sha256": "d" * 64,
        "session_id": "session-1",
        "session_key": "agent:main:main",
        "source": "publish_artifact",
        "created_at": "2026-05-06T12:00:00Z",
        "download_url": "/api/v1/artifacts/art-cli?sessionKey=agent%3Amain%3Amain",
        "store": "artifacts",
    }

    async def fake_run_agent_once(**kwargs: Any) -> AgentRunResult:
        return AgentRunResult(
            status="ok",
            agent_id="main",
            session_key="agent:main:main",
            text="ok",
            usage={},
            errors=[],
            artifacts=[artifact],
        )

    monkeypatch.setattr("opensquilla.cli.agent_cmd.run_agent_once", fake_run_agent_once)

    run_agent_command(message="hello", json_output=True)

    output_artifact = json.loads(capsys.readouterr().out)["artifacts"][0]
    assert "session_key" not in output_artifact
    assert "sessionKey" not in json.dumps(output_artifact)
    assert output_artifact["download_url"] == "/api/v1/artifacts/art-cli"


def test_run_agent_command_direct_call_normalizes_typer_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_agent_once(**kwargs: Any) -> AgentRunResult:
        captured.update(kwargs)
        return AgentRunResult(
            status="ok",
            agent_id="main",
            session_key="agent:main:main",
            text="ok",
            usage={},
            errors=[],
        )

    monkeypatch.setattr("opensquilla.cli.agent_cmd.run_agent_once", fake_run_agent_once)

    run_agent_command(message="hello")

    assert captured["agent_id"] == "main"
    assert captured["session_id"] == ""
    assert captured["model"] is None
    assert captured["workspace"] is None
    assert captured["workspace_strict"] is None
    assert captured["workspace_lockdown"] is False
    assert captured["workspace_write_deny_globs"] == []
    assert captured["scratch_dir"] is None
    assert captured["timeout"] is None
    assert captured["max_iterations"] is None
    assert captured["iteration_timeout"] is None
    assert captured["tool_timeout"] is None
    assert captured["request_timeout"] is None
    assert captured["max_provider_retries"] is None
    assert captured["length_capped_continuations"] is None
    assert captured["thinking"] is None
    assert captured["transcript_path"] is None
    assert captured["usage_path"] is None
    assert captured["event_sink"] is None
    assert captured["session_db_path"] == ":memory:"
    assert captured["no_memory_capture"] is False
    assert captured["attachment_paths"] == []
    assert captured["unattended"] is True
    assert captured["stateless"] is False
    assert captured["stateless_keep_project_rules"] is False
    assert captured["permissions"] is None


def test_run_agent_command_enables_stderr_event_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.agent_event_stream import StderrAgentEventSink

    captured: dict[str, Any] = {}

    async def fake_run_agent_once(**kwargs: Any) -> AgentRunResult:
        captured.update(kwargs)
        return AgentRunResult(
            status="ok",
            agent_id="main",
            session_key="agent:main:main",
            text="ok",
            usage={},
            errors=[],
        )

    monkeypatch.setattr("opensquilla.cli.agent_cmd.run_agent_once", fake_run_agent_once)

    run_agent_command(message="hello", event_stream_stderr=True)

    assert isinstance(captured["event_sink"], StderrAgentEventSink)


def test_run_agent_command_json_includes_routing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_run_agent_once(**kwargs: Any) -> AgentRunResult:
        result = AgentRunResult(
            status="ok",
            agent_id="main",
            session_key="agent:main:main",
            text="ok",
            usage={},
            errors=[],
        )
        result.routing = {  # type: ignore[attr-defined]
            "routed_tier": "c2",
            "routing_source": "v4_phase3",
            "routing_confidence": 0.91,
            "baseline_model": "openrouter/heavy",
            "routed_model": "openrouter/light",
        }
        return result

    monkeypatch.setattr("opensquilla.cli.agent_cmd.run_agent_once", fake_run_agent_once)

    run_agent_command(message="hello", json_output=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["routing"] == {
        "routed_tier": "c2",
        "routing_source": "v4_phase3",
        "routing_confidence": 0.91,
        "baseline_model": "openrouter/heavy",
        "routed_model": "openrouter/light",
    }


@pytest.mark.asyncio
async def test_run_agent_once_collects_done_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            return None

        async def run(self, message: str, session_key: str, **kwargs: Any):
            yield DoneEvent(
                text="ok",
                routed_tier="c2",
                routing_source="v4_phase3",
                routing_confidence=0.91,
                baseline_model="openrouter/heavy",
                routed_model="openrouter/light",
            )

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    result = await run_agent_once(message="hello", config=GatewayConfig())

    assert result.routing == {  # type: ignore[attr-defined]
        "routed_tier": "c2",
        "routing_source": "v4_phase3",
        "routing_confidence": 0.91,
        "baseline_model": "openrouter/heavy",
        "routed_model": "openrouter/light",
    }


@pytest.mark.asyncio
async def test_run_agent_once_explicit_model_overrides_agent_registry_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            captured["runner_config_model"] = kwargs["config"].llm.model

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["run_model"] = kwargs.get("model")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        captured["service_config_model"] = config.llm.model
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    cfg = GatewayConfig(agents=[AgentEntryConfig(id="ops", model="agent/default")])
    await run_agent_once(
        message="hello",
        agent_id="ops",
        model="explicit/model",
        config=cfg,
    )

    assert captured["service_config_model"] == "explicit/model"
    assert captured["runner_config_model"] == "explicit/model"
    assert captured["run_model"] == "explicit/model"


@pytest.mark.asyncio
async def test_run_agent_once_uses_configured_agent_workspace_without_global_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["tool_context"] = kwargs.get("tool_context")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    agent_workspace = tmp_path / "ops-workspace"
    cfg = GatewayConfig(
        agents=[
            AgentEntryConfig(
                id="ops",
                workspace=str(agent_workspace),
            )
        ],
    )

    result = await run_agent_once(message="hello", agent_id="ops", config=cfg)

    assert captured["tool_context"].workspace_dir == str(agent_workspace)
    assert result.workspace == str(agent_workspace)


@pytest.mark.asyncio
async def test_run_agent_once_uses_bound_project_workspace_over_tampered_origin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "cli-project.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    project_path = tmp_path / "project"
    outside = tmp_path / "outside"
    project_path.mkdir()
    outside.mkdir()
    project = await storage.create_or_restore_project_workspace(
        path=str(project_path.resolve()),
        path_key=project_path_key(project_path, strict=True),
        display_name="project",
        trusted_at=1,
    )
    key = "agent:main:cli-project"
    await manager.create(
        key,
        workspace_id=project.workspace_id,
        origin={
            RUN_CONTEXT_ORIGIN_KEY: {
                "run_mode": "standard",
                "workspace": str(outside),
            }
        },
    )
    calls: list[dict[str, Any]] = []

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            return None

        async def run(self, message: str, session_key: str, **kwargs: Any):
            calls.append(kwargs)
            yield DoneEvent(text="ok")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config, manager)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    try:
        result = await run_agent_once(
            message="pwd",
            session_id=key,
            config=GatewayConfig(workspace_dir=str(tmp_path / "default")),
        )
    finally:
        await storage.close()

    assert calls[0]["tool_context"].workspace_dir == project.path
    assert result.workspace == project.path


@pytest.mark.parametrize(
    ("requested_mode", "expected_mode", "expected_mode_source"),
    [
        (None, "safe", "user"),
        ("full", "safe", "user"),
    ],
)
@pytest.mark.asyncio
async def test_run_agent_once_refreshes_unbound_saved_context_before_config_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    requested_mode: str | None,
    expected_mode: str,
    expected_mode_source: str,
) -> None:
    storage = await SessionStorage.open(
        str(tmp_path / f"cli-unbound-{requested_mode or 'saved'}.db")
    )
    manager = SessionManager(storage, inject_time_prefix=False)
    saved_workspace = tmp_path / f"saved-{requested_mode or 'saved'}"
    saved_workspace.mkdir()
    key = f"agent:main:cli-unbound-{requested_mode or 'saved'}"
    await manager.create(
        key,
        origin={
            RUN_CONTEXT_ORIGIN_KEY: {
                "run_mode": "standard",
                "run_mode_source": "user",
                "workspace": str(saved_workspace),
                "domains": [
                    {
                        "domain": "saved.example",
                        "scope": "chat",
                        "source": "manual",
                    }
                ],
            }
        },
    )
    calls: list[dict[str, Any]] = []

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            return None

        async def run(self, message: str, session_key: str, **kwargs: Any):
            calls.append(kwargs)
            yield DoneEvent(text="ok")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config, manager)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    sandbox = (
        SandboxSettings()
        if requested_mode is None
        else SandboxSettings(run_mode=requested_mode)
    )
    try:
        result = await run_agent_once(
            message="pwd",
            session_id=key,
            config=GatewayConfig(
                workspace_dir=str(tmp_path / "default"),
                sandbox=sandbox,
            ),
        )
    finally:
        await storage.close()

    tool_context = calls[0]["tool_context"]
    assert tool_context.run_mode == expected_mode
    assert tool_context.workspace_dir == str(saved_workspace.resolve())
    assert tool_context.sandbox_run_context.run_mode_source == expected_mode_source
    assert [grant.domain for grant in tool_context.sandbox_run_context.domains] == [
        "saved.example"
    ]
    assert getattr(tool_context, "_sandbox_run_context_fresh", False) is True
    assert result.workspace == str(saved_workspace.resolve())


@pytest.mark.parametrize(
    ("saved_mode", "requested_mode", "expected_mode"),
    [("full", "standard", "full"), ("standard", "full", "safe")],
)
@pytest.mark.asyncio
async def test_run_agent_once_preserves_saved_mode_over_config_fallback_for_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    saved_mode: str,
    requested_mode: str,
    expected_mode: str,
) -> None:
    storage = await SessionStorage.open(
        str(tmp_path / f"cli-mode-{saved_mode}-{requested_mode}.db")
    )
    manager = SessionManager(storage, inject_time_prefix=False)
    project_path = tmp_path / f"cli-mode-{saved_mode}-project"
    project_path.mkdir()
    project = await storage.create_or_restore_project_workspace(
        path=str(project_path.resolve()),
        path_key=project_path_key(project_path, strict=True),
        display_name="project",
        trusted_at=1,
    )
    key = f"agent:main:cli-mode-{saved_mode}-to-{requested_mode}"
    await manager.create(
        key,
        workspace_id=project.workspace_id,
        origin={
            RUN_CONTEXT_ORIGIN_KEY: {
                "run_mode": saved_mode,
                "run_mode_source": "user",
                "workspace": project.path,
                "domains": [
                    {
                        "domain": "example.com",
                        "scope": "chat",
                        "source": "manual",
                    }
                ],
            }
        },
    )
    calls: list[dict[str, Any]] = []

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            return None

        async def run(self, message: str, session_key: str, **kwargs: Any):
            calls.append(kwargs)
            yield DoneEvent(text="ok")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config, manager)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    try:
        result = await run_agent_once(
            message="pwd",
            session_id=key,
            config=GatewayConfig(
                workspace_dir=str(tmp_path / "default"),
                sandbox=SandboxSettings(run_mode=requested_mode),
            ),
        )
    finally:
        await storage.close()

    tool_context = calls[0]["tool_context"]
    assert tool_context.run_mode == expected_mode
    assert tool_context.workspace_dir == project.path
    assert tool_context.sandbox_run_context.run_mode_source == "user"
    assert [grant.domain for grant in tool_context.sandbox_run_context.domains] == [
        "example.com"
    ]
    assert result.workspace == project.path


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
@pytest.mark.asyncio
async def test_run_agent_once_revalidates_bound_project_after_transcript_preparation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "cli-retarget.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    project_path = tmp_path / "project"
    replacement = tmp_path / "replacement"
    project_path.mkdir()
    replacement.mkdir()
    project = await storage.create_or_restore_project_workspace(
        path=str(project_path.resolve()),
        path_key=project_path_key(project_path, strict=True),
        display_name="project",
        trusted_at=1,
    )
    key = "agent:main:cli-retarget"
    await manager.create(
        key,
        workspace_id=project.workspace_id,
        origin={
            RUN_CONTEXT_ORIGIN_KEY: {
                "run_mode": "standard",
                "workspace": project.path,
            }
        },
    )
    original_append_message = manager.append_message
    retargeted = False

    async def retarget_after_append(*args: Any, **kwargs: Any) -> Any:
        nonlocal retargeted
        entry = await original_append_message(*args, **kwargs)
        if not retargeted:
            retargeted = True
            project_path.rename(tmp_path / "project-old")
            project_path.symlink_to(replacement, target_is_directory=True)
        return entry

    manager.append_message = retarget_after_append  # type: ignore[method-assign]
    calls: list[dict[str, Any]] = []

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            return None

        async def run(self, message: str, session_key: str, **kwargs: Any):
            calls.append(kwargs)
            yield DoneEvent(text="unsafe")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config, manager)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    try:
        with pytest.raises(ProjectWorkspaceStateError) as raised:
            await run_agent_once(
                message="pwd",
                session_id=key,
                config=GatewayConfig(workspace_dir=str(tmp_path / "default")),
            )
    finally:
        await storage.close()

    assert retargeted is True
    assert raised.value.reason == "canonical_changed"
    assert calls == []


@pytest.mark.asyncio
async def test_run_agent_once_wires_memory_services_into_turnrunner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    services: _FakeServices | None = None

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def run(self, message: str, session_key: str, **kwargs: Any):
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        nonlocal services
        services = _FakeServices(config)
        return services

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    await run_agent_once(message="hello", agent_id="main", config=GatewayConfig())

    assert services is not None
    assert captured["memory_sync_managers"] is services.memory_sync_managers
    assert captured["memory_retrievers"] is services.memory_retrievers
    assert captured["turn_capture_services"] is services.turn_capture_services
    assert captured["session_flush_service"] is services.flush_service
    assert captured["model_catalog"] is services.model_catalog


@pytest.mark.asyncio
async def test_run_agent_once_forwards_max_iterations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["max_iterations"] = kwargs.get("max_iterations")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    await run_agent_once(
        message="hello",
        agent_id="main",
        config=GatewayConfig(),
        max_iterations=321,
    )

    assert captured["max_iterations"] == 321


@pytest.mark.asyncio
async def test_run_agent_once_forwards_zero_max_iterations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["max_iterations"] = kwargs.get("max_iterations")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    await run_agent_once(
        message="hello",
        agent_id="main",
        config=GatewayConfig(),
        max_iterations=0,
    )

    assert captured["max_iterations"] == 0


@pytest.mark.asyncio
async def test_run_agent_once_defaults_to_unattended_interaction_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["tool_context"] = kwargs.get("tool_context")
            captured["bootstrap_context_mode"] = kwargs.get("bootstrap_context_mode")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    await run_agent_once(message="hello", agent_id="main", config=GatewayConfig())

    ctx = captured["tool_context"]
    assert ctx.caller_kind is CallerKind.CLI
    assert ctx.interaction_mode is InteractionMode.UNATTENDED
    assert ctx.run_mode == "full"
    assert ctx.elevated == "full"
    assert captured["bootstrap_context_mode"] == "unattended"


@pytest.mark.asyncio
async def test_run_agent_once_passes_bypass_permissions_to_tool_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["tool_context"] = kwargs.get("tool_context")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    await run_agent_once(
        message="hello",
        agent_id="main",
        config=GatewayConfig(),
        permissions="bypass",
    )

    ctx = captured["tool_context"]
    assert ctx.interaction_mode is InteractionMode.UNATTENDED
    assert ctx.elevated == "full"
    assert ctx.run_mode == "full"


@pytest.mark.asyncio
async def test_run_agent_once_uses_permissions_environment_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["tool_context"] = kwargs.get("tool_context")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.setenv("OPENSQUILLA_AGENT_PERMISSIONS", "full")
    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    await run_agent_once(message="hello", agent_id="main", config=GatewayConfig())

    assert captured["tool_context"].elevated == "full"


@pytest.mark.asyncio
async def test_run_agent_once_uses_default_full_host_run_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["tool_context"] = kwargs.get("tool_context")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.delenv("OPENSQUILLA_AGENT_PERMISSIONS", raising=False)
    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    await run_agent_once(
        message="hello",
        agent_id="main",
        config=GatewayConfig(),
    )

    assert captured["tool_context"].run_mode == "full"
    assert captured["tool_context"].elevated == "full"


@pytest.mark.parametrize(
    ("sandbox", "permissions", "expected_mode"),
    [
        pytest.param(SandboxSettings(), None, "safe", id="default-config"),
        pytest.param(
            SandboxSettings(run_mode="full"),
            None,
            "safe",
            id="explicit-config",
        ),
        pytest.param(SandboxSettings(), "full", "full", id="explicit-cli-full"),
    ],
)
@pytest.mark.asyncio
async def test_run_agent_once_resolves_persisted_safe_before_cli_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sandbox: SandboxSettings,
    permissions: str | None,
    expected_mode: str,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "cli-preference.db"))
    await storage.set_runtime_preference("sandbox.run_mode", "safe")
    manager = SessionManager(storage, inject_time_prefix=False)
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["tool_context"] = kwargs.get("tool_context")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config, manager)

    monkeypatch.delenv("OPENSQUILLA_AGENT_PERMISSIONS", raising=False)
    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    try:
        await run_agent_once(
            message="hello",
            agent_id="main",
            config=GatewayConfig(sandbox=sandbox),
            permissions=permissions,
        )
    finally:
        await storage.close()

    tool_context = captured["tool_context"]
    assert tool_context.run_mode == expected_mode
    assert tool_context.sandbox_run_context.run_mode.value == expected_mode
    assert tool_context.elevated == ("full" if expected_mode == "full" else None)


@pytest.mark.asyncio
async def test_run_agent_once_uses_configured_permissions_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["tool_context"] = kwargs.get("tool_context")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.delenv("OPENSQUILLA_AGENT_PERMISSIONS", raising=False)
    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    await run_agent_once(
        message="hello",
        agent_id="main",
        config=GatewayConfig(permissions=PermissionsConfig(default_mode="bypass")),
    )

    assert captured["tool_context"].elevated == "full"
    assert captured["tool_context"].run_mode == "full"


@pytest.mark.asyncio
async def test_run_agent_once_rejects_invalid_permissions() -> None:
    with pytest.raises(ValueError, match="permissions must be one of"):
        await run_agent_once(
            message="hello",
            agent_id="main",
            config=GatewayConfig(),
            permissions="benchmark",
        )


@pytest.mark.asyncio
async def test_run_agent_once_can_opt_into_interactive_single_shot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["tool_context"] = kwargs.get("tool_context")
            captured["bootstrap_context_mode"] = kwargs.get("bootstrap_context_mode")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    await run_agent_once(
        message="hello",
        agent_id="main",
        config=GatewayConfig(),
        unattended=False,
    )

    assert captured["tool_context"].interaction_mode is InteractionMode.INTERACTIVE
    assert captured["bootstrap_context_mode"] is None


@pytest.mark.asyncio
async def test_run_agent_once_can_opt_into_stateless_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["bootstrap_context_mode"] = kwargs.get("bootstrap_context_mode")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    await run_agent_once(message="hello", config=GatewayConfig(), stateless=True)

    assert captured["bootstrap_context_mode"] == "stateless"


@pytest.mark.asyncio
async def test_run_agent_once_can_keep_project_rules_in_stateless_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["bootstrap_context_mode"] = kwargs.get("bootstrap_context_mode")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    await run_agent_once(
        message="hello",
        config=GatewayConfig(),
        stateless=True,
        stateless_keep_project_rules=True,
    )

    assert captured["bootstrap_context_mode"] == "stateless_keep_project_rules"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stateless", "stateless_keep_project_rules"),
    [
        (True, False),
        (False, True),
    ],
)
async def test_run_agent_once_disables_workspace_template_seeding_for_stateless(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    stateless: bool,
    stateless_keep_project_rules: bool,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        captured.update(kwargs)
        captured["config"] = config
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    await run_agent_once(
        message="hello",
        config=GatewayConfig(),
        workspace=str(tmp_path),
        stateless=stateless,
        stateless_keep_project_rules=stateless_keep_project_rules,
        no_memory_capture=True,
    )

    assert captured["seed_agent_workspaces"] is False
    assert captured["config"].memory.source == "state"


@pytest.mark.asyncio
async def test_run_agent_once_keeps_workspace_template_seeding_for_normal_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        captured.update(kwargs)
        captured["config"] = config
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    await run_agent_once(
        message="hello",
        config=GatewayConfig(),
        workspace=str(tmp_path),
    )

    assert captured["seed_agent_workspaces"] is True
    assert captured["config"].memory.source == "workspace"


@pytest.mark.asyncio
async def test_run_agent_once_passes_scratch_and_lockdown_to_tool_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["tool_context"] = kwargs.get("tool_context")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    scratch_dir = tmp_path / "scratch"
    await run_agent_once(
        message="hello",
        config=GatewayConfig(),
        scratch_dir=str(scratch_dir),
        workspace_lockdown=True,
        workspace_write_deny_globs=["reports/*.txt, tmp/**", "generated/**"],
    )

    ctx = captured["tool_context"]
    assert ctx.scratch_dir == str(scratch_dir)
    assert ctx.workspace_lockdown is True
    assert ctx.workspace_write_deny_globs == ["reports/*.txt", "tmp/**", "generated/**"]


@pytest.mark.asyncio
async def test_run_agent_once_merges_config_workspace_write_deny_globs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeRegistry:
        def list_names(self) -> list[str]:
            return ["write_file", "exec_command", "apply_patch"]

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["tool_context"] = kwargs.get("tool_context")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        services = _FakeServices(config)
        services.tool_registry = FakeRegistry()
        return services

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    scratch_dir = tmp_path / "scratch"
    result = await run_agent_once(
        message="hello",
        config=GatewayConfig(
            tools={
                "workspace_write_deny_globs": [
                    "**/test/**",
                    "**/*Test.java",
                ]
            }
        ),
        scratch_dir=str(scratch_dir),
        workspace_lockdown=True,
        workspace_write_deny_globs=["reports/*.txt", "**/test/**"],
    )

    ctx = captured["tool_context"]
    assert ctx.workspace_write_deny_globs[0] == "reports/*.txt"
    assert set(ctx.workspace_write_deny_globs) == {
        "reports/*.txt",
        "**/test/**",
        "**/*Test.java",
    }
    assert len(ctx.workspace_write_deny_globs) == 3
    assert set(result.workspace_write_deny_globs or []) == {
        "reports/*.txt",
        "**/test/**",
        "**/*Test.java",
    }


def test_run_agent_command_forwards_workspace_lockdown_deny_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_agent_once(**kwargs: Any) -> AgentRunResult:
        captured.update(kwargs)
        return AgentRunResult(
            status="ok",
            agent_id="main",
            session_key="agent:main:main",
            text="ok",
            usage={},
            errors=[],
        )

    monkeypatch.setattr("opensquilla.cli.agent_cmd.run_agent_once", fake_run_agent_once)

    run_agent_command(
        message="hello",
        workspace_lockdown_deny_paths=["reports/*.txt, tmp/**", "generated/**"],
    )

    assert captured["workspace_write_deny_globs"] == [
        "reports/*.txt",
        "tmp/**",
        "generated/**",
    ]


@pytest.mark.asyncio
async def test_run_agent_once_rejects_workspace_lockdown_without_allowed_roots() -> None:
    with pytest.raises(ValueError, match="workspace_lockdown requires"):
        await run_agent_once(
            message="hello",
            config=GatewayConfig(workspace_dir=None),
            workspace_lockdown=True,
        )


@pytest.mark.asyncio
async def test_run_agent_once_rejects_invalid_max_iterations() -> None:
    with pytest.raises(ValueError, match="max_iterations"):
        await run_agent_once(
            message="hello",
            agent_id="main",
            config=GatewayConfig(),
            max_iterations=-1,
        )


@pytest.mark.asyncio
async def test_run_agent_once_forwards_inline_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["attachments"] = kwargs.get("attachments")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    payload = base64.b64encode(b"hello").decode("ascii")
    await run_agent_once(
        message="summarize",
        agent_id="main",
        config=GatewayConfig(),
        attachments=[{"type": "text/plain", "data": payload, "name": "note.txt"}],
    )

    assert captured["attachments"] == [{"type": "text/plain", "data": payload, "name": "note.txt"}]


@pytest.mark.asyncio
async def test_run_agent_once_builds_multiple_file_attachments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, message: str, session_key: str, **kwargs: Any):
            captured["attachments"] = kwargs.get("attachments")
            yield DoneEvent(text="ok", model=kwargs.get("model") or "")

    async def fake_build_services(*, config: GatewayConfig, **kwargs: Any) -> _FakeServices:
        return _FakeServices(config)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)

    note = tmp_path / "note.txt"
    note.write_bytes(b"hello")
    data = tmp_path / "data.csv"
    data.write_bytes(b"a,b\n1,2\n")

    await run_agent_once(
        message="compare",
        agent_id="main",
        config=GatewayConfig(),
        attachment_paths=[str(note), str(data)],
    )

    attachments = captured["attachments"]
    assert len(attachments) == 2
    assert [item["type"] for item in attachments] == ["text/plain", "text/csv"]
    assert base64.b64decode(attachments[0]["data"]) == b"hello"
    assert base64.b64decode(attachments[1]["data"]) == b"a,b\n1,2\n"


@pytest.mark.asyncio
async def test_run_agent_once_rejects_agent_file_requiring_upload_bridge(tmp_path) -> None:
    big_pdf = tmp_path / "big.pdf"
    big_pdf.write_bytes(b"%PDF-1.4\n" + b"a" * 2_000_001)

    with pytest.raises(ValueError, match="gateway bridge upload"):
        await run_agent_once(
            message="read",
            agent_id="main",
            config=GatewayConfig(),
            attachment_paths=[str(big_pdf)],
        )


@pytest.mark.asyncio
async def test_run_agent_once_requires_staging_for_large_text_file(tmp_path) -> None:
    # Text above the inline threshold is stageable now; the one-shot path has
    # no gateway upload bridge, so it must fail with the staging hint rather
    # than a text-family dead end.
    big_csv = tmp_path / "big.csv"
    big_csv.write_bytes(b"a" * 2_000_001)

    with pytest.raises(ValueError, match=r"upload is required|too large to inline"):
        await run_agent_once(
            message="read",
            agent_id="main",
            config=GatewayConfig(),
            attachment_paths=[str(big_csv)],
        )


def test_top_level_agent_command_accepts_repeated_file_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.main import app

    captured: dict[str, Any] = {}

    def fake_run_agent_command(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("opensquilla.cli.main.run_agent_command", fake_run_agent_command)

    result = CliRunner().invoke(
        app,
        [
            "agent",
            "--message",
            "compare",
            "--file",
            "a.txt",
            "--file",
            "b.csv",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["message"] == "compare"
    assert captured["file_paths"] == ["a.txt", "b.csv"]
    assert captured["json_output"] is True
    assert captured["unattended"] is True
    assert captured["permissions"] is None


def test_top_level_agent_command_accepts_interactive_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.main import app

    captured: dict[str, Any] = {}

    def fake_run_agent_command(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("opensquilla.cli.main.run_agent_command", fake_run_agent_command)

    result = CliRunner().invoke(
        app,
        [
            "agent",
            "--message",
            "hello",
            "--interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["unattended"] is False


def test_top_level_agent_command_accepts_permissions_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.main import app

    captured: dict[str, Any] = {}

    def fake_run_agent_command(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("opensquilla.cli.main.run_agent_command", fake_run_agent_command)

    result = CliRunner().invoke(
        app,
        [
            "agent",
            "--message",
            "hello",
            "--permissions",
            "bypass",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["permissions"] == "bypass"


def test_top_level_agent_command_accepts_automation_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.main import app

    captured: dict[str, Any] = {}

    def fake_run_agent_command(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("opensquilla.cli.main.run_agent_command", fake_run_agent_command)

    result = CliRunner().invoke(
        app,
        [
            "agent",
            "--message",
            "hello",
            "--clean-room",
            "--stateless-keep-project-rules",
            "--scratch-dir",
            "scratch",
            "--workspace-lockdown",
            "--workspace-lockdown-deny-paths",
            "reports/*.txt, tmp/**",
            "--workspace-lockdown-deny-paths",
            "generated/**",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["clean_room"] is True
    assert captured["stateless_keep_project_rules"] is True
    assert captured["scratch_dir"] == "scratch"
    assert captured["workspace_lockdown"] is True
    assert captured["workspace_lockdown_deny_paths"] == [
        "reports/*.txt, tmp/**",
        "generated/**",
    ]


def test_top_level_agent_command_accepts_event_stream_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.main import app

    captured: dict[str, Any] = {}

    def fake_run_agent_command(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("opensquilla.cli.main.run_agent_command", fake_run_agent_command)

    result = CliRunner().invoke(
        app,
        ["agent", "--message", "hello", "--json", "--event-stream-stderr"],
    )

    assert result.exit_code == 0, result.output
    assert captured["event_stream_stderr"] is True


def test_top_level_agent_command_accepts_length_capped_continuations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.main import app

    captured: dict[str, Any] = {}

    def fake_run_agent_command(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("opensquilla.cli.main.run_agent_command", fake_run_agent_command)

    result = CliRunner().invoke(
        app,
        [
            "agent",
            "--message",
            "hello",
            "--length-capped-continuations",
            "3",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["length_capped_continuations"] == 3


def test_top_level_agent_command_rejects_invalid_length_capped_continuations() -> None:
    from opensquilla.cli.main import app

    result = CliRunner().invoke(
        app,
        [
            "agent",
            "--message",
            "hello",
            "--length-capped-continuations",
            "0",
        ],
    )

    assert result.exit_code != 0
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.output)
    assert "Invalid value" in output
    assert "--length-capped-continuations" in output
    assert "x>=1" in output
