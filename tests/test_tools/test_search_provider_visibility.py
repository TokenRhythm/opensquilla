from __future__ import annotations

import json
from dataclasses import replace

import pytest

import opensquilla.search.registry as search_registry
import opensquilla.search.runtime_config as search_runtime
import opensquilla.tools  # noqa: F401
from opensquilla.provider.types import ToolDefinition
from opensquilla.search.registry import list_provider_specs
from opensquilla.search.runtime_config import SearchRuntimeConfig, configure_search_runtime
from opensquilla.search.types import SearchProviderSpec
from opensquilla.tools.registry import ToolRegistry, get_default_registry
from opensquilla.tools.types import CallerKind, ToolContext, ToolSpec, current_tool_context

_PUBLIC_PROVIDER_IDS = ["auto", "bocha", "tavily", "brave", "duckduckgo", "exa", "iqs", "keenable"]
# Keyless opt-in providers stay hidden when only keyed providers are configured.
_ALL_KEYED_PROVIDER_IDS = [
    provider_id for provider_id in _PUBLIC_PROVIDER_IDS if provider_id != "keenable"
]
_KEYED_PROVIDERS = {
    "bocha": "BOCHA_SEARCH_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "brave": "BRAVE_SEARCH_API_KEY",
    "exa": "EXA_API_KEY",
    "iqs": "IQS_SEARCH_API_KEY",
}


@pytest.fixture(autouse=True)
def isolated_search_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_runtime, "_runtime_config", SearchRuntimeConfig())
    for spec in list_provider_specs():
        if spec.env_key:
            monkeypatch.delenv(spec.env_key, raising=False)
    monkeypatch.delenv("TEST_SEARCH_PROVIDER_KEY", raising=False)


def _registry(*, discovery: bool = True) -> ToolRegistry:
    registry = ToolRegistry()
    for name in ("web_search", "tool_search") if discovery else ("web_search",):
        registered = get_default_registry().get(name)
        assert registered is not None
        registry.register(registered.spec, registered.handler)
    return registry


def _provider_ids(definitions: list[ToolDefinition]) -> list[str]:
    search_definition = next(
        definition for definition in definitions if definition.name == "web_search"
    )
    return search_definition.input_schema.properties["provider"]["enum"]


def _context() -> ToolContext:
    return ToolContext(is_owner=True, caller_kind=CallerKind.AGENT)


@pytest.mark.parametrize("discovery", [False, True])
@pytest.mark.parametrize("has_context", [False, True])
@pytest.mark.parametrize(
    ("configured_providers", "expected"),
    [
        ((), ["auto", "duckduckgo"]),
        (("brave", "exa"), ["auto", "brave", "duckduckgo", "exa"]),
        (tuple(_KEYED_PROVIDERS), _ALL_KEYED_PROVIDER_IDS),
    ],
)
def test_model_surface_only_advertises_available_search_providers(
    monkeypatch: pytest.MonkeyPatch,
    discovery: bool,
    has_context: bool,
    configured_providers: tuple[str, ...],
    expected: list[str],
) -> None:
    for provider_id in configured_providers:
        monkeypatch.setenv(_KEYED_PROVIDERS[provider_id], "synthetic-search-key")
    registry = _registry(discovery=discovery)
    ctx = _context() if has_context else None
    authorized = registry.to_tool_definitions(ctx)

    model_definitions = registry.to_model_tool_definitions(authorized, ctx)

    assert _provider_ids(model_definitions) == expected
    assert _provider_ids(authorized) == _PUBLIC_PROVIDER_IDS
    assert "synthetic-search-key" not in json.dumps(
        [definition.model_dump(mode="json") for definition in model_definitions]
    )
    if ctx is not None:
        assert ctx.tool_search_index is not None
        hit = ctx.tool_search_index.search("web_search", limit=1)[0]
        assert hit.input_schema["properties"]["provider"]["enum"] == expected


@pytest.mark.parametrize("credential_source", ["configured", "configured_env", "spec_env"])
def test_model_provider_visibility_uses_all_runtime_credential_sources(
    monkeypatch: pytest.MonkeyPatch,
    credential_source: str,
) -> None:
    if credential_source == "configured":
        configure_search_runtime("exa", api_key="synthetic-inline-key")
    elif credential_source == "configured_env":
        monkeypatch.setenv("TEST_SEARCH_PROVIDER_KEY", "synthetic-configured-env-key")
        configure_search_runtime("exa", api_key_env="TEST_SEARCH_PROVIDER_KEY")
    else:
        monkeypatch.setenv("EXA_API_KEY", "synthetic-spec-env-key")
    registry = _registry()
    ctx = _context()

    definitions = registry.to_model_tool_definitions(registry.to_tool_definitions(ctx), ctx)

    assert _provider_ids(definitions) == ["auto", "duckduckgo", "exa"]
    assert "synthetic-" not in json.dumps(
        [definition.model_dump(mode="json") for definition in definitions]
    )


@pytest.mark.parametrize("opt_in", ["selected", "spec_env"])
def test_model_surface_advertises_keyless_opt_in_provider_only_after_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    opt_in: str,
) -> None:
    registry = _registry()
    ctx = _context()
    authorized = registry.to_tool_definitions(ctx)
    assert _provider_ids(registry.to_model_tool_definitions(authorized, ctx)) == [
        "auto",
        "duckduckgo",
    ]

    if opt_in == "selected":
        configure_search_runtime("keenable")
    else:
        monkeypatch.setenv("KEENABLE_API_KEY", "synthetic-keenable-key")
    definitions = registry.to_model_tool_definitions(authorized, ctx)

    assert _provider_ids(definitions) == ["auto", "duckduckgo", "keenable"]
    assert "synthetic-" not in json.dumps(
        [definition.model_dump(mode="json") for definition in definitions]
    )


def test_model_provider_schema_refreshes_without_mutating_authorized_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    ctx = _context()
    authorized = registry.to_tool_definitions(ctx)
    initial = registry.to_model_tool_definitions(authorized, ctx)
    assert _provider_ids(initial) == ["auto", "duckduckgo"]

    monkeypatch.setenv("EXA_API_KEY", "synthetic-env-key")
    with_exa = registry.to_model_tool_definitions(authorized, ctx)
    assert _provider_ids(with_exa) == ["auto", "duckduckgo", "exa"]
    monkeypatch.delenv("EXA_API_KEY")
    configure_search_runtime("brave", api_key="synthetic-inline-key")
    with_brave = registry.to_model_tool_definitions(authorized, ctx)
    assert _provider_ids(with_brave) == ["auto", "brave", "duckduckgo"]
    configure_search_runtime("duckduckgo")
    cleared = registry.to_model_tool_definitions(authorized, ctx)
    assert _provider_ids(cleared) == ["auto", "duckduckgo"]

    assert _provider_ids(initial) == ["auto", "duckduckgo"]
    assert _provider_ids(with_exa) == ["auto", "duckduckgo", "exa"]
    assert _provider_ids(with_brave) == ["auto", "brave", "duckduckgo"]
    assert _provider_ids(authorized) == _PUBLIC_PROVIDER_IDS
    assert ctx.tool_search_index is not None
    hit = ctx.tool_search_index.search("web_search", limit=1)[0]
    assert hit.input_schema["properties"]["provider"]["enum"] == ["auto", "duckduckgo"]


def test_model_provider_visibility_uses_runtime_support_and_declared_enum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "synthetic-brave-key")
    monkeypatch.setitem(
        search_registry._provider_specs,
        "brave",
        replace(search_registry.get_provider_spec("brave"), runtime_supported=False),
    )
    monkeypatch.setitem(
        search_registry._provider_specs,
        "custom_search",
        SearchProviderSpec(provider_id="custom_search"),
    )
    registry = _registry()
    ctx = _context()

    definitions = registry.to_model_tool_definitions(registry.to_tool_definitions(ctx), ctx)

    assert _provider_ids(definitions) == ["auto", "duckduckgo"]


@pytest.mark.asyncio
async def test_public_search_catalog_retains_registered_provider_contract() -> None:
    registry = _registry()
    ctx = _context()
    registry.to_model_tool_definitions(registry.to_tool_definitions(ctx), ctx)

    assert _provider_ids(registry.to_tool_definitions(ctx)) == _PUBLIC_PROVIDER_IDS
    registered = registry.get("web_search")
    assert registered is not None
    assert registered.spec.parameters["provider"]["enum"] == _PUBLIC_PROVIDER_IDS
    for catalog in (await registry.list_tools(), await registry.effective_tools()):
        search_entry = next(entry for entry in catalog if entry["name"] == "web_search")
        assert search_entry["schema"]["properties"]["provider"]["enum"] == _PUBLIC_PROVIDER_IDS


@pytest.mark.asyncio
async def test_tool_search_response_hides_unconfigured_search_providers() -> None:
    registry = _registry()
    ctx = _context()
    registry.to_model_tool_definitions(registry.to_tool_definitions(ctx), ctx)
    registered = registry.get("tool_search")
    assert registered is not None

    token = current_tool_context.set(ctx)
    try:
        payload = json.loads(await registered.handler(query="web_search", limit=1))
    finally:
        current_tool_context.reset(token)

    match = payload["matches"][0]
    assert match["name"] == "web_search"
    assert match["input_schema"]["properties"]["provider"]["enum"] == ["auto", "duckduckgo"]


@pytest.mark.asyncio
async def test_progressive_disclosure_projects_search_provider_schema_before_model_call() -> None:
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import ToolCall
    from opensquilla.tools.dispatch import build_tool_handler

    async def unavailable_tool(**_kwargs: object) -> str:
        return "unused"

    registry = _registry()
    registry.register(
        ToolSpec(name="unapproved_utility", description="Utility", parameters={}),
        unavailable_tool,
    )
    ctx = _context()
    authorized = [
        definition
        for definition in registry.to_tool_definitions(ctx)
        if definition.name != "unapproved_utility"
    ]
    initial = registry.to_model_tool_definitions(authorized, ctx)
    agent = Agent(
        provider=object(),
        tool_definitions=[definition for definition in initial if definition.name == "tool_search"],
        tool_handler=build_tool_handler(registry, ctx),
        tool_registry=registry,
        tool_context=ctx,
    )

    result = await agent._execute_tool(
        ToolCall(
            tool_use_id="discover-search",
            tool_name="tool_search",
            arguments={"query": "web_search", "limit": 1},
        )
    )

    assert result.is_error is False
    assert _provider_ids(agent.tool_definitions) == ["auto", "duckduckgo"]
    assert ctx.authorized_tool_names == frozenset({"web_search", "tool_search"})
    assert ctx.tool_search_index is not None
    assert "unapproved_utility" not in {
        hit.name for hit in ctx.tool_search_index.search("unapproved_utility")
    }
