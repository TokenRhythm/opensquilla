from __future__ import annotations

import ast
from pathlib import Path

from opensquilla.provider.types import ToolDefinition, ToolInputSchema
from opensquilla.tools.policy import ToolSurfaceCapabilities
from opensquilla.tools.types import (
    CallerKind,
    InteractionMode,
    RegisteredTool,
    ToolContext,
    ToolSpec,
)
from opensquilla.tools.visibility import (
    ToolProfile,
    effective_tool_context,
    filter_by_profile,
    profile_allows_tool,
    resolve_profile,
    visible_registered_tools,
)

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "src/opensquilla/tools/registry.py"
VISIBILITY = ROOT / "src/opensquilla/tools/visibility.py"


async def _handler() -> str:
    return "ok"


def _registered_tool(
    name: str,
    *,
    default_access: str = "allow",
    owner_only: bool = False,
) -> RegisteredTool:
    return RegisteredTool(
        spec=ToolSpec(
            name=name,
            description=f"{name} tool",
            parameters={},
            default_access=default_access,
            owner_only=owner_only,
        ),
        handler=_handler,
    )


def _tool_definition(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"{name} tool",
        input_schema=ToolInputSchema(type="object", properties={}, required=[]),
    )


def _imports_from(path: Path) -> set[tuple[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imports.add((node.module, alias.name))
    return imports


def _top_level_classes(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.name for node in tree.body if isinstance(node, ast.ClassDef)}


def _top_level_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _top_level_assignments(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_registry_delegates_visibility_policy_to_tools_visibility_boundary() -> None:
    imports = _imports_from(REGISTRY)

    assert ("opensquilla.tools", "visibility") in imports

    registry_classes = _top_level_classes(REGISTRY)
    registry_functions = _top_level_functions(REGISTRY)
    registry_assignments = _top_level_assignments(REGISTRY)
    assert "ToolProfile" not in registry_classes
    assert "filter_by_profile" not in registry_functions
    assert "profile_allows_tool" not in registry_functions
    assert "resolve_profile" not in registry_functions
    assert {
        "ToolProfile",
        "_CHANNEL_DEFAULT_ALLOW",
        "_CHANNEL_HARD_DENY_NON_OWNER",
        "filter_by_profile",
        "profile_allows_tool",
        "resolve_profile",
    } <= registry_assignments

    visibility_classes = _top_level_classes(VISIBILITY)
    visibility_functions = _top_level_functions(VISIBILITY)
    visibility_assignments = _top_level_assignments(VISIBILITY)
    assert "ToolProfile" in visibility_classes
    assert "filter_by_profile" in visibility_functions
    assert "profile_allows_tool" in visibility_functions
    assert "resolve_profile" in visibility_functions
    assert "effective_tool_context" in visibility_functions
    assert "visible_registered_tools" in visibility_functions
    assert "_CHANNEL_DEFAULT_ALLOW" in visibility_assignments


def test_visibility_boundary_preserves_channel_profile_filtering() -> None:
    channel_ctx = ToolContext(is_owner=False, caller_kind=CallerKind.CHANNEL)
    profile = resolve_profile(channel_ctx)

    filtered = filter_by_profile(
        [
            _tool_definition("publish_artifact"),
            _tool_definition("git_commit"),
            _tool_definition("read_file"),
        ],
        profile,
    )

    assert profile is ToolProfile.CHANNEL_DEFAULT
    assert [tool.name for tool in filtered] == ["publish_artifact", "read_file"]
    assert profile_allows_tool("cron", profile) is True
    assert profile_allows_tool("git_commit", profile) is False


def test_visibility_boundary_preserves_context_visibility_rules() -> None:
    tools = [
        _registered_tool("visible"),
        _registered_tool("owner_only", owner_only=True),
        _registered_tool("hidden", default_access="deny"),
    ]
    ctx = ToolContext(
        is_owner=False,
        caller_kind=CallerKind.CHANNEL,
        allowed_tools={"visible", "hidden", "owner_only"},
        surfaced_tools={"hidden"},
    )

    visible = visible_registered_tools(tools, ctx, sort=True)

    assert [tool.spec.name for tool in visible] == ["hidden", "visible"]


def test_visibility_boundary_preserves_effective_runtime_contexts() -> None:
    subagent_ctx = effective_tool_context(
        session_key="subagent:worker",
        caller_kind=None,
        interaction_mode=None,
        tool_surface_capabilities=ToolSurfaceCapabilities(session_manager=True),
        is_owner=True,
    )
    owner_cron_ctx = effective_tool_context(
        session_key="cron:nightly",
        caller_kind=None,
        interaction_mode=None,
        tool_surface_capabilities=ToolSurfaceCapabilities(scheduler=True),
        is_owner=True,
    )
    channel_ctx = effective_tool_context(
        caller_kind=CallerKind.CHANNEL,
        tool_surface_capabilities=ToolSurfaceCapabilities(
            channel_backing=True,
            scheduler=True,
        ),
        is_owner=False,
    )

    assert subagent_ctx.caller_kind is CallerKind.SUBAGENT
    assert subagent_ctx.interaction_mode is InteractionMode.UNATTENDED
    assert "publish_artifact" in subagent_ctx.denied_tools
    assert owner_cron_ctx.caller_kind is CallerKind.CRON
    assert owner_cron_ctx.is_owner is True
    assert channel_ctx.caller_kind is CallerKind.CHANNEL
    assert "cron" in (channel_ctx.allowed_tools or set())
