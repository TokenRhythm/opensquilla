from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from opensquilla.engine.agent import Agent
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.turn_runner.provider_and_tools_stage import ProviderAndToolsStageInput
from opensquilla.engine.types import AgentConfig
from opensquilla.gateway.config import GatewayConfig
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.skills.host import (
    PROTECTED_SKILL_TOOLS,
    ProtectedSkillConfig,
    bind_protected_skill,
    enforce_protected_tool_scope,
    runtime_digest,
)
from opensquilla.skills.http_broker import HTTPBrokerError
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.script_runtime import SkillScriptError, SkillScriptGrant
from opensquilla.tool_boundary import ToolCall, ToolResult
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import PlanAccess, ToolContext, ToolSpec

_SCRIPT = """import json, os, tomllib
from pathlib import Path
config = tomllib.loads(Path(os.environ['DEMO_HOST_CONFIG']).read_text())
counter = Path('/private/count')
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
print(json.dumps({'count': count, 'binding': config['caller_binding']}))
"""
_BYPASS_TOOLS = {
    "exec_command",
    "create_file",
    "write_file",
    "edit_file",
    "apply_patch",
    "read_file",
    "web_fetch",
    "web_search",
    "meta_invoke",
    "spawn_subagent",
    "send_message",
    "generate_image",
    "tts",
    "submit",
    "submit_plan",
    "request_user_input",
    "plan_run_checkpoint",
}


def _profile(tmp_path: Path) -> ProtectedSkillConfig:
    skill = tmp_path / "installed/host-demo"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: host-demo\ndescription: host test\n---\n")
    (skill / "scripts/main.py").write_text(_SCRIPT)
    (skill / "scripts/validate.py").write_text("print('{}')\n")
    sibling = skill.parent / "ungranted"
    sibling.mkdir()
    (sibling / "SKILL.md").write_text("---\nname: ungranted\ndescription: other\n---\n")
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/python").symlink_to("/usr/bin/python3")
    (runtime / "dependency.txt").write_text("pinned runtime dependency")
    scripts = frozenset({"scripts/main.py", "scripts/validate.py"})
    return ProtectedSkillConfig(
        agent_ids=["main"],
        name="host-demo",
        directory=skill,
        package_sha256=SkillScriptGrant.pin("host-demo", skill, scripts).digest,
        scripts=["scripts/main.py"],
        validator_script="scripts/validate.py",
        allowed_artifacts=["report.txt"],
        runtime_root=runtime,
        runtime_sha256=runtime_digest(runtime),
        state_root=tmp_path / "state",
        socket_root=tmp_path / "sockets",
        config_environment_variable="DEMO_HOST_CONFIG",
    )


def _context(**kwargs: Any) -> ToolContext:
    values: dict[str, Any] = {
        "agent_id": "main",
        "is_owner": True,
        "execution_id": "run-1",
        "session_key": "agent:main:webchat:one",
        "artifact_session_id": "durable-1",
        "session_epoch": 0,
    }
    values.update(kwargs)
    return ToolContext(**values)


def _gateway(profile: ProtectedSkillConfig | None, tmp_path: Path) -> GatewayConfig:
    return GatewayConfig.model_validate(
        {
            "skills": {"protected_script": profile},
            "attachments": {"media_root": str(tmp_path / "artifacts")},
            "tools": {"profile": "full", "also_allow": sorted(_BYPASS_TOOLS)},
            "meta_skill": {"enabled": True, "auto_trigger": True},
        }
    )


def test_gateway_protected_profile_defaults_off_and_requires_both_pins(tmp_path: Path) -> None:
    assert GatewayConfig().skills.protected_script is None
    profile = _profile(tmp_path)
    for field in ("package_sha256", "runtime_sha256"):
        data = profile.model_dump()
        del data[field]
        with pytest.raises(ValidationError, match=field):
            ProtectedSkillConfig.model_validate(data)


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_id": None},
        {"artifact_session_id": None},
        {"session_key": None},
        {"session_epoch": None},
        {"session_epoch": -1},
        {"session_epoch": True},
        {"subagent_depth": 1},
    ],
)
def test_protected_binding_requires_durable_top_level_identity(
    tmp_path: Path,
    changes: dict[str, Any],
) -> None:
    with pytest.raises(SkillScriptError, match="durable"):
        bind_protected_skill(_context(**changes), _profile(tmp_path))


def test_unselected_agent_is_unchanged(tmp_path: Path) -> None:
    context = _context(agent_id="unprotected", session_epoch=None)
    assert bind_protected_skill(context, _profile(tmp_path)) is context


@pytest.mark.parametrize("allowed", [set(), {"skill_view"}])
def test_actual_host_binding_preserves_upstream_narrow_allowlist(
    tmp_path: Path,
    allowed: set[str],
) -> None:
    context = _context(allowed_tools=allowed, denied_tools={"publish_artifact"})
    bound = bind_protected_skill(context, _profile(tmp_path))
    assert bound.allowed_tools == allowed
    assert bound.surfaced_tools == allowed
    assert "publish_artifact" in bound.denied_tools
    enforce_protected_tool_scope(bound, list(PROTECTED_SKILL_TOOLS | _BYPASS_TOOLS))
    assert bound.allowed_tools == allowed
    assert bound.surfaced_tools == allowed


@pytest.mark.parametrize("storage_status", ["absent", "missing", "error"])
async def test_protected_turn_refuses_best_effort_identity_fallback(
    tmp_path: Path,
    storage_status: str,
) -> None:
    manager = (
        None
        if storage_status == "absent"
        else SimpleNamespace(
            get_session=AsyncMock(
                return_value=None,
                side_effect=(RuntimeError("unavailable") if storage_status == "error" else None),
            ),
        )
    )
    runner = TurnRunner(
        None, session_manager=manager, config=_gateway(_profile(tmp_path), tmp_path)
    )
    with pytest.raises(SkillScriptError, match="durable session storage"):
        await runner._with_artifact_context(_context(), "agent:main:webchat:same-tail")


async def test_unprotected_turn_retains_existing_identity_fallback(tmp_path: Path) -> None:
    runner = TurnRunner(None, config=_gateway(None, tmp_path))
    context = await runner._with_artifact_context(_context(), "agent:main:webchat:legacy")
    assert context.artifact_session_id == "legacy"
    assert context.skill_script_runner is None
    assert context.artifact_publication_policy is None


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
async def test_normal_turn_persists_across_turns_restart_and_isolates_sessions(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    config = _gateway(profile, tmp_path)
    db_path = str(tmp_path / "sessions.db")
    storage = await SessionStorage.open(db_path)
    try:
        manager = SessionManager(storage)
        key = "agent:main:webchat:one"
        node = await manager.create(key)
        runner = TurnRunner(None, session_manager=manager, config=config)
        original = _context(
            elevated="full",
            run_mode="full",
            workspace_dir=str(tmp_path),
            sandbox_mounts=[{"source": "/", "target": "/host"}],
        )
        first = await runner._with_artifact_context(original, key)
        assert first.artifact_session_id == node.session_id
        assert first.session_epoch == node.epoch
        assert first.workspace_strict and first.workspace_lockdown
        assert first.protected_skill_host
        assert first.run_mode == "standard" and first.elevated is None
        assert first.sandbox_mounts == []
        assert first.artifact_publication_policy is not None
        assert first.skill_script_runner is not None
        assert first.allowed_tools == PROTECTED_SKILL_TOOLS
        assert first.skill_catalog is not None
        assert [skill.name for skill in first.skill_catalog.skills] == ["host-demo"]
        result = await first.skill_script_runner.run("host-demo", "scripts/main.py", [])
        assert result.returncode == 0, result.stderr
        saved = json.loads(result.stdout)
        assert saved["count"] == 1
        second = await runner._with_artifact_context(_context(execution_id="run-2"), key)
        assert second.workspace_dir == first.workspace_dir
        assert second.skill_script_runner is not None
        assert second.skill_script_runner.execution_id == "run-2"
        result = await second.skill_script_runner.run("host-demo", "scripts/main.py", [])
        assert json.loads(result.stdout) == {"count": 2, "binding": saved["binding"]}
    finally:
        await storage.close()

    reopened = await SessionStorage.open(db_path)
    try:
        manager = SessionManager(reopened)
        runner = TurnRunner(None, session_manager=manager, config=config)
        restarted = await runner._with_artifact_context(_context(execution_id="run-3"), key)
        assert restarted.workspace_dir == first.workspace_dir
        assert restarted.skill_script_runner is not None
        result = await restarted.skill_script_runner.run("host-demo", "scripts/main.py", [])
        assert json.loads(result.stdout) == {"count": 3, "binding": saved["binding"]}
        other_key = "agent:main:webchat:two"
        await manager.create(other_key)
        other = await runner._with_artifact_context(_context(execution_id="run-4"), other_key)
        assert other.workspace_dir != first.workspace_dir
        assert other.skill_script_runner is not None
        result = await other.skill_script_runner.run("host-demo", "scripts/main.py", [])
        assert json.loads(result.stdout)["count"] == 1
        assert json.loads(result.stdout)["binding"] != saved["binding"]
        # Simulate the stored epoch advance, preserving the session's durable ID.
        node.epoch += 1
        await reopened.upsert_session(node)
        advanced = await runner._with_artifact_context(_context(execution_id="run-5"), key)
        assert advanced.workspace_dir != first.workspace_dir
        assert advanced.skill_script_runner is not None
        result = await advanced.skill_script_runner.run("host-demo", "scripts/main.py", [])
        assert json.loads(result.stdout)["count"] == 1
        assert json.loads(result.stdout)["binding"] != saved["binding"]
    finally:
        await reopened.close()


@pytest.mark.parametrize("mode", ["default", "plan"])
async def test_normal_stage_pins_catalog_and_cannot_reopen_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    profile = _profile(tmp_path)
    loader = SkillLoader(
        extra_dirs=[profile.directory.parent], snapshot_path=tmp_path / "catalog.json"
    )
    loader.load_all()
    assert "ungranted" in {skill.name for skill in loader.snapshot().skills}
    called: list[str] = []

    async def forbidden() -> str:
        called.append("executed")
        return "must never run"

    registry = ToolRegistry()
    for name in PROTECTED_SKILL_TOOLS | _BYPASS_TOOLS:
        registry.register(ToolSpec(name, "test", {}, plan_access=PlanAccess.READ_ONLY), forbidden)
    manager = SimpleNamespace(
        get_session=AsyncMock(
            return_value=SimpleNamespace(
                session_id="durable",
                epoch=0,
                workspace_id=None,
            )
        )
    )
    runner = TurnRunner(
        None,
        tool_registry=registry,
        session_manager=manager,
        skill_loader=loader,
        config=_gateway(profile, tmp_path),
    )
    monkeypatch.setattr(runner, "_resolve_provider", lambda: (object(), None))
    context = _context(
        collaboration_mode=mode,
        surfaced_tools=set(_BYPASS_TOOLS),
        tool_policy={"profile": "full", "also_allow": sorted(_BYPASS_TOOLS)},
    )
    outcome = await runner._provider_and_tools_stage.run(
        ProviderAndToolsStageInput(
            session_key=context.session_key or "",
            agent_id="main",
            tool_context=context,
            run_kind="default",
            input_mode="text",
        )
    )
    assert outcome.output is not None
    output = outcome.output
    assert output.skill_catalog is not None
    assert {skill.name for skill in output.skill_catalog.skills} == {"host-demo"}
    assert {tool.name for tool in output.tool_defs} == PROTECTED_SKILL_TOOLS
    assert output.effective_tool_context is not None
    assert output.effective_tool_context.allowed_tools == PROTECTED_SKILL_TOOLS
    assert output.tool_handler is not None
    for name in sorted(_BYPASS_TOOLS):
        result = await output.tool_handler(
            ToolCall(tool_use_id=f"call-{name}", tool_name=name, arguments={})
        )
        assert result.is_error, name
    agent = Agent(
        provider=cast(Any, object()),
        config=AgentConfig(metadata={"skill_loader": loader, "meta_skill_enabled": True}),
        tool_registry=registry,
        tool_context=output.effective_tool_context,
    )
    events = [
        event
        async for event in agent._run_one_streaming(
            ToolCall(tool_use_id="streaming-meta", tool_name="meta_invoke", arguments={}),
            output.effective_tool_context,
        )
    ]
    assert len(events) == 1
    assert isinstance(events[0], ToolResult) and events[0].is_error
    assert called == []


@pytest.mark.parametrize("field", ["package_sha256", "runtime_sha256"])
def test_bad_installation_pin_is_rejected(tmp_path: Path, field: str) -> None:
    profile = _profile(tmp_path).model_copy(update={field: "0" * 64})
    with pytest.raises(SkillScriptError, match="digest"):
        bind_protected_skill(_context(), profile)


@pytest.mark.parametrize("target", ["package", "runtime"])
async def test_installation_changed_after_binding_is_never_executed(
    tmp_path: Path,
    target: str,
) -> None:
    profile = _profile(tmp_path)
    bound = bind_protected_skill(_context(), profile)
    assert bound.skill_script_runner is not None
    path = (
        profile.directory / "scripts/main.py"
        if target == "package"
        else profile.runtime_root / "dependency.txt"
    )
    path.write_text("changed after binding")
    with pytest.raises(SkillScriptError, match="changed"):
        await bound.skill_script_runner.run("host-demo", "scripts/main.py", [])
    assert not (Path(bound.workspace_dir or "") / "executed").exists()


def test_host_rejects_profile_changes_for_existing_task(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    bind_protected_skill(_context(), profile)
    changed = profile.model_copy(update={"allowed_artifacts": ["different.txt"]})
    with pytest.raises(SkillScriptError, match="differs"):
        bind_protected_skill(_context(execution_id="next-turn"), changed)


def test_runtime_rejects_editable_dependency_and_external_symlink(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    bad = root / "dependency.pth"
    bad.write_text("__editable__dependency")
    with pytest.raises(SkillScriptError, match="Editable"):
        runtime_digest(root)
    bad.unlink()
    (root / "outside").symlink_to("/etc/passwd")
    with pytest.raises(SkillScriptError, match="symlink"):
        runtime_digest(root)


def test_runtime_symlink_target_single_file_budget_is_checked_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    target = runtime / "z-target"
    with target.open("wb") as stream:
        stream.truncate(128 * 1024 * 1024 + 1)
    (runtime / "a-link").symlink_to(target.name)

    def unexpected_read(_path: Path) -> bytes:
        pytest.fail("oversized symlink target must be refused before reading")

    monkeypatch.setattr(Path, "read_bytes", unexpected_read)
    with pytest.raises(SkillScriptError, match="bounded installation"):
        runtime_digest(runtime)


def test_runtime_symlink_targets_count_towards_total_byte_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    target = runtime / "z-target"
    with target.open("wb") as stream:
        stream.truncate(128 * 1024 * 1024)
    for number in range(5):
        (runtime / f"a{number}-link").symlink_to(target.name)
    reads = 0

    def bounded_read(path: Path) -> bytes:
        nonlocal reads
        assert path == target
        reads += 1
        assert reads <= 4
        return b"synthetic content avoids allocating large test buffers"

    monkeypatch.setattr(Path, "read_bytes", bounded_read)
    with pytest.raises(SkillScriptError, match="bounded installation"):
        runtime_digest(runtime)
    assert reads == 4


async def test_broker_uses_short_operator_socket_root_and_refuses_long_one(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    services = [
        {
            "name": "isolated",
            "origin": "http://127.0.0.1:9",
            "routes": [
                {"method": "GET", "path_pattern": "/status"},
            ],
        }
    ]
    # The sandbox's Unix socket needs a shorter root than long pytest case names.
    with tempfile.TemporaryDirectory(
        prefix="host-s-", dir="/mnt/data/opensquilla-dev/tmp"
    ) as short:
        profile = ProtectedSkillConfig.model_validate(
            {
                **profile.model_dump(),
                "socket_root": short,
                "services": services,
            }
        )
        bound = bind_protected_skill(_context(), profile)
        assert bound.skill_script_runner is not None
        assert bound.skill_script_runner.broker is not None
        async with bound.skill_script_runner.broker.serve() as socket:
            assert socket.is_socket()
            assert len(bytes(socket)) < 108
        assert not socket.exists()
    long = profile.model_copy(
        update={
            "socket_root": tmp_path / ("long" * 40),
            "state_root": tmp_path / "long-state",
        }
    )
    with pytest.raises(HTTPBrokerError, match="short"):
        bind_protected_skill(_context(), long)


def test_protected_marker_preserves_narrower_allowlist_without_runtime_objects() -> None:
    context = _context(
        protected_skill_host=True,
        allowed_tools={"skill_view"},
        denied_tools={"publish_artifact"},
    )
    assert context.skill_script_runner is None and context.artifact_publication_policy is None
    for _ in range(2):
        enforce_protected_tool_scope(context, list(PROTECTED_SKILL_TOOLS | _BYPASS_TOOLS))
        assert context.allowed_tools == {"skill_view"}
        assert context.surfaced_tools == {"skill_view"}
        assert _BYPASS_TOOLS <= context.denied_tools
        assert "publish_artifact" in context.denied_tools


@pytest.mark.parametrize("protected", [False, True])
async def test_bootstrap_disables_submit_review_env_only_for_protected_host(
    monkeypatch: pytest.MonkeyPatch,
    protected: bool,
) -> None:
    from test_engine.turn_runner.test_agent_bootstrap_stage_unit import _make_input, _make_stage

    monkeypatch.setenv("OPENSQUILLA_SUBMIT_REVIEW", "1")
    stage = _make_stage()
    outcome = await stage.run(_make_input(tool_context=_context(protected_skill_host=protected)))
    assert outcome.output is not None
    assert outcome.output.agent_config.submit_review_enabled is (not protected)
