"""Real turn/dispatch skill access follows channel workspace authority."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from opensquilla.channels.types import IncomingMessage
from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.steps.selected_skills import SelectedSkillError, load_selected_skills
from opensquilla.engine.types import ToolCall
from opensquilla.execution_workspaces import prepare_managed_workspace
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.project_workspace_runtime import apply_run_context_route_metadata
from opensquilla.gateway.routing import build_channel_route_envelope, tool_context_from_envelope
from opensquilla.sandbox.backend import NoopBackend, SeatbeltBackend
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.sandbox.run_context import RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.skills import eligibility
from opensquilla.skills.loader import SkillLoader
from opensquilla.tools import registry as registry_module
from opensquilla.tools.builtin import skill_tools
from opensquilla.tools.workspace_authoring import workspace_authoring_attested


class _AvailableSeatbelt(SeatbeltBackend):
    """Only availability is synthetic; these tests never execute a process."""

    def available(self) -> bool:
        return True


@pytest.fixture
def authoring_turn(tmp_path, monkeypatch):
    # Copy the boot registry so registrations and loader state cannot leak to
    # other tests; all installed tools retain their real handlers and specs.
    registry = copy.copy(registry_module.get_default_registry())
    registry._tools = dict(registry._tools)
    monkeypatch.setattr(registry_module, "_default_registry", registry)
    monkeypatch.setattr(skill_tools, "_loader", skill_tools._loader)
    monkeypatch.setattr(eligibility, "_live_skills_cfg_getter", None)
    config = GatewayConfig()
    bundled = Path(__file__).resolve().parents[2] / "src/opensquilla/skills/bundled"
    loader = SkillLoader(
        bundled_dir=bundled,
        snapshot_path=tmp_path / "catalog.json",
        lockfile_path=tmp_path / "skills-lock.json",
    )
    skill_tools.create_skill_tools(loader, skills_cfg_getter=lambda: config.skills)
    catalog = loader.snapshot_for_turn()
    workspace = Path(prepare_managed_workspace(tmp_path / "profile").binding["root"])
    runtime = configure_runtime(
        SandboxSettings(sandbox=True, backend="noop", security_grading=False),
        workspace=workspace,
    )
    runtime.backend = _AvailableSeatbelt()
    runner = TurnRunner(None, tool_registry=registry, skill_loader=loader, config=config)

    def build(*, binding_kind="managed", admin=False, denied=(), allowed=None):
        envelope = build_channel_route_envelope(
            IncomingMessage(sender_id="test-member", channel_id="test-group", content="report"),
            session_key="agent:main:feishu:test-group:test-member",
            session_id="test-session",
            session_prefix="feishu",
        )
        apply_run_context_route_metadata(
            envelope,
            RunContext(
                run_mode=RunMode.SAFE,
                workspace=str(workspace),
                workspace_binding_kind=binding_kind,
            ),
            principal_is_owner=admin,
        )
        if admin:
            # This is the trusted post-admission stamp, never inbound metadata.
            envelope.metadata["principal_is_owner"] = True
            envelope.metadata["channel_admin_verified"] = True
        ctx = tool_context_from_envelope(
            envelope, is_owner=admin, workspace_dir=str(workspace), workspace_strict=True,
        )
        ctx.skill_catalog = catalog
        ctx.denied_tools.update(denied)
        ctx.allowed_tools = allowed
        definitions, handler = runner._build_tools(ctx, skill_catalog=catalog)
        assert handler is not None
        return ctx, definitions, handler

    try:
        yield build, catalog, config, runtime
    finally:
        reset_runtime()


@pytest.mark.parametrize("name", ["xlsx", "pptx", "pdf-toolkit"])
async def test_attested_channel_loads_bundled_skill_through_real_turn(authoring_turn, name):
    build, catalog, config, _runtime = authoring_turn
    ctx, definitions, handler = build()
    assert workspace_authoring_attested(ctx)
    assert {"skill_list", "skill_view", "execute_code"} <= ctx.authorized_tool_names
    assert {"skill_list", "skill_view"} <= {definition.name for definition in definitions}
    assert {"exec_command", "skill_create", "skill_install_community"}.isdisjoint(
        ctx.authorized_tool_names,
    )

    listing = await handler(ToolCall("list", "skill_list", {}))
    assert not listing.is_error
    assert name in listing.content
    view = await handler(ToolCall("view", "skill_view", {"name": name}))
    assert not view.is_error
    assert "execute_code" in view.content
    assert "publish_artifact" in view.content

    search = await handler(ToolCall("search", "tool_search", {"query": "skill_view"}))
    assert not search.is_error
    assert "skill_view" in {item["name"] for item in json.loads(search.content)["matches"]}

    spec = catalog.get_by_name(name)
    assert spec is not None
    turn = TurnContext(
        message="Generate a report", session_key=ctx.session_key, config=config,
        provider=None, model="test", tool_defs=definitions, system_prompt="Base prompt",
        skill_catalog=catalog,
        metadata={"selected_skills": [{
            "name": name, "instanceId": spec.instance_id, "digest": spec.tree_digest,
        }]},
    )
    await load_selected_skills(turn, ctx)
    assert spec.instance_id in ctx.verified_skill_ids
    assert "execute_code" in turn.system_prompt[1]


@pytest.mark.parametrize("binding_kind", [None, "configured"])
async def test_unattested_channel_cannot_discover_or_load_skills(authoring_turn, binding_kind):
    build, catalog, config, _runtime = authoring_turn
    ctx, definitions, handler = build(binding_kind=binding_kind)
    assert not workspace_authoring_attested(ctx)
    assert {"skill_list", "skill_view"}.isdisjoint(ctx.authorized_tool_names)
    for name, arguments in (("skill_list", {}), ("skill_view", {"name": "xlsx"})):
        result = await handler(ToolCall(name, name, arguments))
        assert result.is_error
    search = await handler(ToolCall("search", "tool_search", {"query": "skill_view"}))
    assert not search.is_error
    assert "skill_view" not in {item["name"] for item in json.loads(search.content)["matches"]}
    spec = catalog.get_by_name("xlsx")
    turn = TurnContext(
        message="Generate a report", session_key=ctx.session_key, config=config,
        provider=None, model="test", tool_defs=definitions, system_prompt="Base prompt",
        skill_catalog=catalog,
        metadata={"selected_skills": [{
            "name": spec.name, "instanceId": spec.instance_id, "digest": spec.tree_digest,
        }]},
    )
    with pytest.raises(SelectedSkillError, match="tool permissions"):
        await load_selected_skills(turn, ctx)


@pytest.mark.parametrize("policy", ["deny", "allowlist"])
async def test_channel_skill_allowance_preserves_operator_restrictions(authoring_turn, policy):
    build, _catalog, _config, _runtime = authoring_turn
    options = {"denied": {"skill_view"}} if policy == "deny" else {"allowed": {"read_file"}}
    ctx, _definitions, handler = build(**options)
    assert workspace_authoring_attested(ctx)
    assert "skill_view" not in ctx.authorized_tool_names
    assert (await handler(ToolCall("view", "skill_view", {"name": "xlsx"}))).is_error


async def test_unattested_channel_cannot_explicitly_allow_skill_tools(authoring_turn):
    build, _catalog, _config, _runtime = authoring_turn
    ctx, _definitions, handler = build(binding_kind=None, allowed={"skill_list", "skill_view"})
    assert not workspace_authoring_attested(ctx)
    assert {"skill_list", "skill_view"}.isdisjoint(ctx.authorized_tool_names)
    for name, arguments in (("skill_list", {}), ("skill_view", {"name": "xlsx"})):
        assert (await handler(ToolCall("explicit", name, arguments))).is_error


async def test_channel_env_owner_full_cannot_bypass_skill_attestation(
    authoring_turn,
    monkeypatch,
):
    monkeypatch.setenv("OPENSQUILLA_TOOL_PROFILE", "owner_full")
    build, _catalog, _config, _runtime = authoring_turn
    ctx, definitions, handler = build(binding_kind=None, allowed={"skill_list", "skill_view"})
    assert not workspace_authoring_attested(ctx)
    assert {"skill_list", "skill_view"}.isdisjoint(ctx.authorized_tool_names)
    assert {"skill_list", "skill_view"}.isdisjoint(
        definition.name for definition in definitions
    )

    for name, arguments in (("skill_list", {}), ("skill_view", {"name": "xlsx"})):
        result = await handler(ToolCall("env-profile", name, arguments))
        assert result.is_error
    search = await handler(ToolCall("env-search", "tool_search", {"query": "skill_view"}))
    assert not search.is_error
    assert "skill_view" not in {item["name"] for item in json.loads(search.content)["matches"]}


async def test_forged_channel_owner_cannot_bypass_skill_attestation(authoring_turn):
    from opensquilla.tools.dispatch import build_tool_handler

    build, _catalog, _config, _runtime = authoring_turn
    ctx, _definitions, _handler = build(binding_kind=None, allowed={"skill_list", "skill_view"})
    # Mutating is_owner after route construction simulates an untrusted claim;
    # the authenticated admin stamp remains false and must still control policy.
    ctx.is_owner = True
    registry = registry_module.get_default_registry()
    definitions = registry.to_tool_definitions(ctx)
    registry.to_model_tool_definitions(definitions, ctx)
    # TurnRunner applies policy to a copy, so build a handler with this exact
    # forged context instead of mutating only its original caller's copy.
    handler = build_tool_handler(registry, ctx)
    assert not ctx.channel_admin_verified
    assert {"skill_list", "skill_view"}.isdisjoint(ctx.authorized_tool_names)
    assert {"skill_list", "skill_view"}.isdisjoint(
        definition.name for definition in definitions
    )

    for name, arguments in (("skill_list", {}), ("skill_view", {"name": "xlsx"})):
        result = await handler(ToolCall("forged-owner", name, arguments))
        assert result.is_error
    search = await handler(ToolCall("forged-search", "tool_search", {"query": "skill_view"}))
    assert not search.is_error
    assert "skill_view" not in {item["name"] for item in json.loads(search.content)["matches"]}


async def test_revoked_workspace_authority_blocks_already_surfaced_skill(authoring_turn):
    build, _catalog, _config, runtime = authoring_turn
    ctx, _definitions, handler = build()
    assert "skill_view" in ctx.authorized_tool_names
    runtime.backend = NoopBackend()
    result = await handler(ToolCall("view", "skill_view", {"name": "xlsx"}))
    assert result.is_error


async def test_skill_resources_do_not_grant_host_file_access(authoring_turn, tmp_path):
    build, catalog, _config, _runtime = authoring_turn
    _ctx, _definitions, handler = build()
    spec = catalog.get_by_name("xlsx")
    script = Path(spec.base_dir) / "scripts/create_xlsx.py"
    resource = await handler(ToolCall(
        "resource", "skill_view", {"name": "xlsx", "file_path": "scripts/create_xlsx.py"},
    ))
    assert not resource.is_error
    assert "openpyxl" in resource.content
    direct = await handler(ToolCall("host-read", "read_file", {"path": str(script)}))
    assert direct.is_error
    secret = tmp_path / "operator-only.txt"
    secret.write_text("synthetic-private-content", encoding="utf-8")
    for path in (str(secret), "../operator-only.txt", "scripts/../../operator-only.txt"):
        result = await handler(ToolCall(
            "escape", "skill_view", {"name": "xlsx", "file_path": path},
        ))
        assert "synthetic-private-content" not in result.content
        assert "File not found" in result.content


async def test_verified_channel_admin_skill_access_remains_available(authoring_turn):
    build, _catalog, _config, _runtime = authoring_turn
    ctx, _definitions, handler = build(binding_kind="configured", admin=True)
    assert ctx.channel_admin_verified
    assert not workspace_authoring_attested(ctx)
    assert {"skill_list", "skill_view"} <= ctx.authorized_tool_names
    result = await handler(ToolCall("view", "skill_view", {"name": "xlsx"}))
    assert not result.is_error
    assert "openpyxl" in result.content
