from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import opensquilla.tools.policy as policy_facade
from opensquilla.tools import policy_helpers
from opensquilla.tools.policy_runtime import (
    ToolSurfaceCapabilities,
    detect_runtime_tool_surface_capabilities,
    resolve_runtime_tool_surface,
    tool_surface_capabilities_from_runtime,
)
from opensquilla.tools.types import CallerKind, InteractionMode, ToolContext

ROOT = Path(__file__).resolve().parents[2]
POLICY_FACADE = ROOT / "src/opensquilla/tools/policy/__init__.py"
POLICY_HELPERS = ROOT / "src/opensquilla/tools/policy_helpers.py"
POLICY_RUNTIME = ROOT / "src/opensquilla/tools/policy_runtime.py"
VISIBILITY = ROOT / "src/opensquilla/tools/visibility.py"
RPC_PAYLOAD = ROOT / "src/opensquilla/tools/rpc_payload.py"
REGISTRY = ROOT / "src/opensquilla/tools/registry.py"


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


def test_policy_facade_and_helpers_delegate_runtime_surface_to_boundary() -> None:
    facade_imports = _imports_from(POLICY_FACADE)
    helper_functions = _top_level_functions(POLICY_HELPERS)
    helper_assignments = _top_level_assignments(POLICY_HELPERS)

    assert policy_facade.ToolSurfaceCapabilities is ToolSurfaceCapabilities
    assert policy_helpers.ToolSurfaceCapabilities is ToolSurfaceCapabilities
    assert policy_helpers.resolve_runtime_tool_surface is resolve_runtime_tool_surface
    assert (
        "opensquilla.tools.policy_runtime",
        "ToolSurfaceCapabilities",
    ) in facade_imports
    assert "ToolSurfaceCapabilities" not in _top_level_classes(POLICY_HELPERS)
    assert {
        "ToolSurfaceCapabilities",
        "resolve_runtime_tool_surface",
        "detect_runtime_tool_surface_capabilities",
        "tool_surface_capabilities_from_runtime",
    } <= helper_assignments
    assert "resolve_runtime_tool_surface" not in helper_functions
    assert "detect_runtime_tool_surface_capabilities" not in helper_functions
    assert "tool_surface_capabilities_from_runtime" not in helper_functions

    runtime_classes = _top_level_classes(POLICY_RUNTIME)
    runtime_functions = _top_level_functions(POLICY_RUNTIME)
    assert "ToolSurfaceCapabilities" in runtime_classes
    assert "resolve_runtime_tool_surface" in runtime_functions
    assert "detect_runtime_tool_surface_capabilities" in runtime_functions
    assert "tool_surface_capabilities_from_runtime" in runtime_functions


def test_internal_tool_modules_depend_on_policy_runtime_not_policy_facade() -> None:
    for path in (VISIBILITY, RPC_PAYLOAD, REGISTRY):
        imports = _imports_from(path)
        assert any(
            module == "opensquilla.tools.policy_runtime"
            and name
            in {
                "ToolSurfaceCapabilities",
                "resolve_runtime_tool_surface",
                "tool_surface_capabilities_from_runtime",
            }
            for module, name in imports
        )
        assert ("opensquilla.tools.policy", "ToolSurfaceCapabilities") not in imports
        assert ("opensquilla.tools.policy", "resolve_runtime_tool_surface") not in imports


def test_policy_runtime_preserves_runtime_capability_denylists() -> None:
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.SUBAGENT,
        interaction_mode=InteractionMode.UNATTENDED,
        allowed_tools={
            "agents_list",
            "cron",
            "gateway",
            "image_generate",
            "memory_get",
            "message",
            "session_status",
            "sessions_send",
        },
    )

    result = resolve_runtime_tool_surface(
        ctx,
        capabilities=ToolSurfaceCapabilities(
            session_manager=False,
            task_runtime=False,
            scheduler=False,
            gateway_config=False,
            channel_backing=False,
            image_generation=False,
        ),
    )

    assert {
        "agents_list",
        "cron",
        "gateway",
        "image_generate",
        "memory_get",
        "message",
        "session_status",
        "sessions_send",
    } <= result.denied_tools
    assert result.allowed_tools == set()


def test_policy_runtime_hides_git_tools_when_git_is_unavailable() -> None:
    git_tools = {"git_status", "git_diff", "git_log", "git_commit"}
    ctx = ToolContext(
        is_owner=True,
        allowed_tools={"read_file", *git_tools},
    )

    result = resolve_runtime_tool_surface(
        ctx,
        capabilities=ToolSurfaceCapabilities(git_available=False),
    )

    assert git_tools <= result.denied_tools
    assert result.allowed_tools == {"read_file"}


def test_policy_runtime_detects_git_with_unified_capability_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        "opensquilla.git_runtime.resolve_git_capability",
        lambda: SimpleNamespace(available=False),
    )

    caps = detect_runtime_tool_surface_capabilities()

    assert caps.git_available is False


def test_policy_runtime_builds_capabilities_from_injected_dependencies() -> None:
    caps = tool_surface_capabilities_from_runtime(
        session_manager=object(),
        task_runtime=None,
        scheduler=object(),
        gateway_config=object(),
        channel_manager=None,
        originating_envelope=object(),
        image_generation=False,
    )

    assert caps == ToolSurfaceCapabilities(
        session_manager=True,
        task_runtime=False,
        scheduler=True,
        gateway_config=True,
        channel_backing=True,
        image_generation=False,
    )


def test_policy_runtime_uses_current_media_image_generation_probe() -> None:
    imports = _imports_from(POLICY_RUNTIME)

    assert (
        "opensquilla.tools.builtin.media",
        "image_generation_available",
    ) in imports
    assert (
        "opensquilla.provider.image_generation_runtime",
        "image_generation_available",
    ) not in imports
