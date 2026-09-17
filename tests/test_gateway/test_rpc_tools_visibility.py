from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.scopes import READ_SCOPE
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolSpec


async def _handler() -> str:
    return "ok"


def _ctx(*, tool_registry: Any, is_owner: bool) -> RpcContext:
    return RpcContext(
        conn_id="test",
        config=GatewayConfig(),
        principal=Principal(
            role="operator",
            scopes=frozenset({READ_SCOPE}),
            is_owner=is_owner,
            authenticated=True,
        ),
        tool_registry=tool_registry,
        session_manager=object(),
        task_runtime=object(),
    )


def _tool_names(payload: dict[str, Any]) -> set[str]:
    return {tool["name"] for tool in payload["tools"]}


def test_tools_rpc_delegates_payloads_to_tools_boundary() -> None:
    from opensquilla.gateway import rpc_tools
    from opensquilla.tools import registry

    source = Path(rpc_tools.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    registry_tree = ast.parse(Path(registry.__file__).read_text(encoding="utf-8"))
    boundary_path = Path(registry.__file__).with_name("rpc_payload.py")

    assert boundary_path.exists()

    boundary_tree = ast.parse(boundary_path.read_text(encoding="utf-8"))
    imports = {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    registry_defs = {
        node.name
        for node in registry_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    boundary_defs = {
        node.name
        for node in boundary_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert {
        ("opensquilla.tools.rpc_payload", "tools_catalog_payload"),
        ("opensquilla.tools.rpc_payload", "tools_effective_payload"),
    } <= imports
    assert {
        "tools_catalog_payload",
        "tools_effective_payload",
    } <= registry_defs
    assert {
        "tool_rpc_params",
        "tool_surface_capabilities_for_runtime",
        "tools_catalog_payload",
        "tools_effective_payload",
    } <= boundary_defs


def _registry_with_owner_only_probe() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="ordinary_probe",
            description="ordinary probe",
            parameters={},
        ),
        _handler,
    )
    registry.register(
        ToolSpec(
            name="owner_probe",
            description="owner probe",
            parameters={},
            owner_only=True,
        ),
        _handler,
    )
    return registry


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["tools.catalog", "tools.effective"])
async def test_tools_rpc_visibility_respects_principal_ownership(method: str) -> None:
    registry = _registry_with_owner_only_probe()

    non_owner = await get_dispatcher().dispatch(
        "r1",
        method,
        {"callerKind": "agent"},
        _ctx(tool_registry=registry, is_owner=False),
    )
    owner = await get_dispatcher().dispatch(
        "r2",
        method,
        {"callerKind": "agent"},
        _ctx(tool_registry=registry, is_owner=True),
    )

    assert non_owner.error is None, non_owner.error
    assert owner.error is None, owner.error
    assert _tool_names(non_owner.payload) == {"ordinary_probe"}
    assert _tool_names(owner.payload) == {"ordinary_probe", "owner_probe"}


@pytest.mark.asyncio
async def test_tools_catalog_without_runtime_params_respects_principal_ownership() -> None:
    registry = _registry_with_owner_only_probe()

    non_owner = await get_dispatcher().dispatch(
        "r1",
        "tools.catalog",
        {},
        _ctx(tool_registry=registry, is_owner=False),
    )

    assert non_owner.error is None, non_owner.error
    assert _tool_names(non_owner.payload) == {"ordinary_probe"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"callerKind": "subagent"},
        {"sessionKey": "subagent:test"},
    ],
)
@pytest.mark.parametrize("method", ["tools.catalog", "tools.effective"])
async def test_tools_rpc_subagent_visibility_respects_principal_ownership(
    method: str,
    params: dict[str, str],
) -> None:
    registry = _registry_with_owner_only_probe()

    non_owner = await get_dispatcher().dispatch(
        "r1",
        method,
        params,
        _ctx(tool_registry=registry, is_owner=False),
    )

    assert non_owner.error is None, non_owner.error
    assert _tool_names(non_owner.payload) == {"ordinary_probe"}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["tools.catalog", "tools.effective"])
async def test_default_tools_rpc_hides_owner_only_tools_from_non_owner(method: str) -> None:
    import opensquilla.tools.builtin  # noqa: F401
    from opensquilla.gateway.config import ImageGenerationConfig, LlmProviderConfig
    from opensquilla.tools.builtin.media import configure_image_generation
    from opensquilla.tools.registry import get_default_registry

    configure_image_generation(
        ImageGenerationConfig(enabled=True),
        llm_config=LlmProviderConfig(provider="openrouter", api_key="sk-or-configured"),
    )
    try:
        non_owner = await get_dispatcher().dispatch(
            "r1",
            method,
            {"callerKind": "agent"},
            _ctx(tool_registry=get_default_registry(), is_owner=False),
        )
        owner = await get_dispatcher().dispatch(
            "r2",
            method,
            {"callerKind": "agent"},
            _ctx(tool_registry=get_default_registry(), is_owner=True),
        )
    finally:
        configure_image_generation(ImageGenerationConfig())

    assert non_owner.error is None, non_owner.error
    assert owner.error is None, owner.error

    non_owner_names = _tool_names(non_owner.payload)
    owner_names = _tool_names(owner.payload)

    assert "http_request" not in non_owner_names
    assert "git_commit" not in non_owner_names
    assert {"http_request", "git_commit"} <= owner_names
    assert {"image_generate", "sessions_spawn", "sessions_send"} <= owner_names
    assert "spawn_subagent" not in owner_names
    assert "send_message" not in owner_names
    assert "generate_image" not in owner_names


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["tools.catalog", "tools.effective"])
async def test_default_tools_rpc_hides_git_tools_when_runtime_is_unavailable(
    method: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.tools.builtin  # noqa: F401
    from opensquilla.tools.registry import get_default_registry

    monkeypatch.setattr(
        "opensquilla.git_runtime.resolve_git_capability",
        lambda: SimpleNamespace(available=False),
    )

    result = await get_dispatcher().dispatch(
        "r1",
        method,
        {"callerKind": "agent"},
        _ctx(tool_registry=get_default_registry(), is_owner=True),
    )

    assert result.error is None, result.error
    assert {
        "git_status",
        "git_diff",
        "git_log",
        "git_commit",
    }.isdisjoint(_tool_names(result.payload))


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["tools.catalog", "tools.effective"])
async def test_default_channel_tools_rpc_exposes_structured_file_authoring(method: str) -> None:
    import opensquilla.tools.builtin  # noqa: F401
    from opensquilla.tools.registry import get_default_registry

    result = await get_dispatcher().dispatch(
        "r1",
        method,
        {"callerKind": "channel"},
        _ctx(tool_registry=get_default_registry(), is_owner=False),
    )

    assert result.error is None, result.error
    names = _tool_names(result.payload)

    assert {"create_csv", "create_xlsx", "create_pdf_report", "create_pptx"} <= names
    assert "write_file" not in names
    assert "execute_code" not in names
    assert "apply_patch" not in names
