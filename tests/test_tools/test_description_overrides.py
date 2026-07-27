"""Env-gated tool-description overrides (off by default).

Covers OPENSQUILLA_TOOL_DESCRIPTION_OVERRIDES: unset/off keeps every
model-facing description byte-identical even when the config table is
populated, "config" reads [tools.description_overrides] from gateway config,
and a .toml/.json path loads the table from that file. Motivation: tool and
parameter wording is a per-deployment lever; the override must flow through
the single registry choke point, replace text verbatim, keep the functional
scratch-dir suffix, and record a runtime event once per process so delivery
gates can attribute the rewrite.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from opensquilla.gateway.config import GatewayConfig, ToolsConfig
from opensquilla.tools import registry as registry_module
from opensquilla.tools.description_overrides import (
    resolve_tool_description_overrides,
)
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolContext, ToolSpec

_ENV = "OPENSQUILLA_TOOL_DESCRIPTION_OVERRIDES"
_EVENTS_PATH_ENV = "OPENSQUILLA_RUNTIME_EVENTS_PATH"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.delenv(_EVENTS_PATH_ENV, raising=False)
    registry_module._description_override_event_keys.clear()
    yield
    registry_module._description_override_event_keys.clear()


async def _noop() -> str:
    return "ok"


def _make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="exec_command",
            description="Run a command in the workspace shell.",
            parameters={
                "command": {"type": "string", "description": "Command to run."},
            },
            required=["command"],
        ),
        _noop,
    )
    registry.register(
        ToolSpec(
            name="list_dir",
            description="List directory contents with type and size.",
            parameters={
                "path": {"type": "string", "description": "Directory path."},
            },
            required=["path"],
        ),
        _noop,
    )
    return registry


def _make_config(table: dict[str, str]) -> GatewayConfig:
    return GatewayConfig(tools=ToolsConfig(description_overrides=table))


def _ctx_with_resolved(
    ctx: ToolContext,
    resolved: tuple[dict[str, str], str] | None,
) -> ToolContext:
    # Mirrors the TurnRunner.run_turn replace() plumbing.
    return replace(
        ctx,
        tool_description_overrides=(resolved[0] if resolved else None),
        tool_description_overrides_source=(resolved[1] if resolved else None),
    )


def _serialize(registry: ToolRegistry, ctx: ToolContext) -> str:
    return "\n".join(
        definition.model_dump_json()
        for definition in registry.to_tool_definitions(ctx)
    )


def test_env_unset_with_populated_config_table_keeps_definitions_byte_identical() -> None:
    registry = _make_registry()
    config = _make_config({"exec_command": "Replacement wording."})
    ctx = ToolContext(is_owner=True)
    baseline = _serialize(registry, ctx)

    resolved = resolve_tool_description_overrides(config)
    assert resolved is None
    assert _serialize(registry, _ctx_with_resolved(ctx, resolved)) == baseline


@pytest.mark.parametrize("env_value", ["off", "0", "false", "no", "  "])
def test_env_off_values_resolve_to_none(
    monkeypatch: pytest.MonkeyPatch, env_value: str
) -> None:
    monkeypatch.setenv(_ENV, env_value)
    config = _make_config({"exec_command": "Replacement wording."})

    assert resolve_tool_description_overrides(config) is None


def test_env_config_with_empty_table_resolves_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ENV, "config")

    assert resolve_tool_description_overrides(_make_config({})) is None
    assert resolve_tool_description_overrides(None) is None


def test_env_config_override_lands_verbatim_and_keeps_scratch_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ENV, "config")
    registry = _make_registry()
    override_text = "Run one command and report its full output."
    resolved = resolve_tool_description_overrides(
        _make_config({"exec_command": override_text})
    )
    assert resolved is not None
    assert resolved[1] == "config"

    ctx = _ctx_with_resolved(
        ToolContext(is_owner=True, scratch_dir="/tmp/scratch"), resolved
    )
    by_name = {d.name: d for d in registry.to_tool_definitions(ctx)}
    assert by_name["exec_command"].description.startswith(override_text)
    assert "/tmp/scratch" in by_name["exec_command"].description
    assert by_name["list_dir"].description == "List directory contents with type and size."


def test_param_override_lands_in_input_schema_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ENV, "config")
    param_text = "Full command line to run, as one string."
    resolved = resolve_tool_description_overrides(
        _make_config({"exec_command.command": param_text})
    )
    assert resolved is not None

    registry = _make_registry()
    ctx = _ctx_with_resolved(ToolContext(is_owner=True), resolved)
    by_name = {d.name: d for d in registry.to_tool_definitions(ctx)}
    properties = by_name["exec_command"].input_schema.properties
    assert properties["command"]["description"] == param_text
    # The registered spec itself stays untouched (deepcopy boundary).
    assert (
        registry.get("exec_command").spec.parameters["command"]["description"]
        == "Command to run."
    )


def test_env_file_wins_over_config_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    override_file = tmp_path / "overrides.toml"
    override_file.write_text(
        'exec_command = "File wording."\n'
        '"exec_command.command" = "File command wording."\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV, str(override_file))
    resolved = resolve_tool_description_overrides(
        _make_config({"exec_command": "Config wording."})
    )
    assert resolved == (
        {
            "exec_command": "File wording.",
            "exec_command.command": "File command wording.",
        },
        "env_file",
    )


def test_env_file_accepts_gateway_config_shape_and_nested_dotted_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    override_file = tmp_path / "config.toml"
    override_file.write_text(
        "[tools.description_overrides]\n"
        'exec_command = "Nested table wording."\n'
        "[tools.description_overrides.list_dir]\n"
        'path = "Nested param wording."\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV, str(override_file))
    resolved = resolve_tool_description_overrides(None)
    assert resolved == (
        {
            "exec_command": "Nested table wording.",
            "list_dir.path": "Nested param wording.",
        },
        "env_file",
    )


def test_unrecognized_env_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "enabled")

    with pytest.raises(ValueError, match=_ENV):
        resolve_tool_description_overrides(_make_config({}))


def test_unreadable_override_file_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv(_ENV, str(tmp_path / "missing.toml"))
    with pytest.raises(ValueError, match=_ENV):
        resolve_tool_description_overrides(None)

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(_ENV, str(malformed))
    with pytest.raises(ValueError, match=_ENV):
        resolve_tool_description_overrides(None)


def test_non_string_override_value_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    override_file = tmp_path / "overrides.json"
    override_file.write_text('{"exec_command": 7}', encoding="utf-8")
    monkeypatch.setenv(_ENV, str(override_file))

    with pytest.raises(ValueError, match="non-empty string"):
        resolve_tool_description_overrides(None)


def test_runtime_event_emitted_once_with_applied_and_requested_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ENV, "config")
    resolved = resolve_tool_description_overrides(
        _make_config(
            {
                "exec_command": "Override wording.",
                "exec_command.command": "Override command wording.",
                "unknown_tool": "Ignored wording.",
            }
        )
    )
    assert resolved is not None

    events: list[dict] = []
    registry = _make_registry()
    ctx = _ctx_with_resolved(
        ToolContext(
            is_owner=True,
            session_key="agent:main:test",
            agent_id="main",
            on_runtime_event=events.append,
        ),
        resolved,
    )
    registry.to_tool_definitions(ctx)
    registry.to_tool_definitions(ctx)

    assert len(events) == 1
    event = events[0]
    assert event["feature"] == "tool_description_overrides"
    assert event["name"] == "tool_description_overrides.applied"
    assert event["action"] == "rewrite_tool_descriptions"
    assert event["source"] == "config"
    assert event["tools"] == ["exec_command"]
    assert event["params"] == ["exec_command.command"]
    assert event["requested"] == [
        "exec_command",
        "exec_command.command",
        "unknown_tool",
    ]
    assert event["session_key"] == "agent:main:test"
    assert event["agent_id"] == "main"


def test_runtime_event_falls_back_to_env_resolved_sink(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    events_path = tmp_path / "runtime_events.jsonl"
    monkeypatch.setenv(_EVENTS_PATH_ENV, str(events_path))
    monkeypatch.setenv(_ENV, "config")
    resolved = resolve_tool_description_overrides(
        _make_config({"exec_command": "Override wording."})
    )
    assert resolved is not None

    registry = _make_registry()
    ctx = _ctx_with_resolved(ToolContext(is_owner=True), resolved)
    registry.to_tool_definitions(ctx)
    registry.to_tool_definitions(ctx)

    lines = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    matching = [
        event for event in lines
        if event.get("name") == "tool_description_overrides.applied"
    ]
    assert len(matching) == 1
    assert matching[0]["source"] == "config"
    assert matching[0]["tools"] == ["exec_command"]


def test_no_runtime_event_when_mechanism_off(tmp_path, monkeypatch) -> None:
    events_path = tmp_path / "runtime_events.jsonl"
    monkeypatch.setenv(_EVENTS_PATH_ENV, str(events_path))
    events: list[dict] = []
    registry = _make_registry()
    ctx = ToolContext(is_owner=True, on_runtime_event=events.append)

    registry.to_tool_definitions(ctx)

    assert events == []
    assert not events_path.exists()


def test_unknown_tool_key_reported_in_requested_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ENV, "config")
    resolved = resolve_tool_description_overrides(
        _make_config(
            {
                "list_dir": "Override wording.",
                "unknown_tool": "Ignored wording.",
                "list_dir.nonexistent_param": "Ignored param wording.",
            }
        )
    )
    assert resolved is not None

    events: list[dict] = []
    registry = _make_registry()
    ctx = _ctx_with_resolved(
        ToolContext(is_owner=True, on_runtime_event=events.append), resolved
    )
    by_name = {d.name: d for d in registry.to_tool_definitions(ctx)}
    assert by_name["list_dir"].description == "Override wording."
    assert "nonexistent_param" not in by_name["list_dir"].input_schema.properties

    assert len(events) == 1
    assert events[0]["tools"] == ["list_dir"]
    assert events[0]["params"] == []
    assert events[0]["requested"] == [
        "list_dir",
        "list_dir.nonexistent_param",
        "unknown_tool",
    ]


def test_dotted_plugin_tool_name_counts_as_whole_description_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Plugin tools may carry dotted names; _description_for applies overrides
    # by exact name lookup, so the event must attribute "web.search" as a tool
    # override — not silently drop it as a parameter of a nonexistent "web".
    monkeypatch.setenv(_ENV, "config")
    resolved = resolve_tool_description_overrides(
        _make_config({"web.search": "Override wording."})
    )
    assert resolved is not None

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="web.search",
            description="Search the web.",
            parameters={"query": {"type": "string", "description": "Query."}},
            required=["query"],
        ),
        _noop,
    )
    events: list[dict] = []
    ctx = _ctx_with_resolved(
        ToolContext(is_owner=True, on_runtime_event=events.append),
        resolved,
    )
    definitions = registry.to_tool_definitions(ctx)

    assert definitions[0].description.startswith("Override wording.")
    assert len(events) == 1
    assert events[0]["tools"] == ["web.search"]
    assert events[0]["params"] == []


def test_same_keys_different_wording_emits_fresh_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The once-per-process guard fingerprints values, not just keys: a
    # repointed table with identical keys but new wording must re-emit so
    # attribution ties each turn to the wording that was actually live.
    monkeypatch.setenv(_ENV, "config")
    events: list[dict] = []
    registry = _make_registry()
    base_ctx = ToolContext(
        is_owner=True,
        session_key="agent:main:test",
        on_runtime_event=events.append,
    )

    first = resolve_tool_description_overrides(
        _make_config({"exec_command": "Wording A."})
    )
    registry.to_tool_definitions(_ctx_with_resolved(base_ctx, first))
    registry.to_tool_definitions(_ctx_with_resolved(base_ctx, first))

    second = resolve_tool_description_overrides(
        _make_config({"exec_command": "Wording B."})
    )
    registry.to_tool_definitions(_ctx_with_resolved(base_ctx, second))

    assert len(events) == 2
    assert events[0]["overrides_sha256"] != events[1]["overrides_sha256"]


def test_failed_emission_does_not_permanently_suppress_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The guard is only set after a successful emit: a callback that throws on
    # the first definition build (sink not wired yet at boot) must not swallow
    # the .applied event for the rest of the process.
    monkeypatch.setenv(_ENV, "config")
    resolved = resolve_tool_description_overrides(
        _make_config({"exec_command": "Override wording."})
    )
    assert resolved is not None

    registry = _make_registry()
    events: list[dict] = []
    calls = {"count": 0}

    def flaky_sink(event: dict) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("sink unavailable at boot")
        events.append(event)

    ctx = _ctx_with_resolved(
        ToolContext(is_owner=True, on_runtime_event=flaky_sink),
        resolved,
    )
    registry.to_tool_definitions(ctx)
    assert events == []
    registry.to_tool_definitions(ctx)
    assert len(events) == 1
    registry.to_tool_definitions(ctx)
    assert len(events) == 1


def test_empty_override_table_absent_from_written_config() -> None:
    # A default (empty) description_overrides table must not serialize: written
    # configs and RPC dumps stay byte-identical to pre-mechanism output when
    # the mechanism is unused.
    data = GatewayConfig().to_toml_dict()
    tools_table = data.get("tools", {})
    assert "description_overrides" not in tools_table


def test_populated_override_table_survives_written_config() -> None:
    config = GatewayConfig(
        tools=ToolsConfig(description_overrides={"exec_command": "Override wording."})
    )
    data = config.to_toml_dict()
    assert data["tools"]["description_overrides"] == {"exec_command": "Override wording."}


def test_second_session_in_same_process_still_emits_applied_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The emit guard is keyed per session: in a multi-session gateway process
    # the first session must not swallow attribution for every later session,
    # or a per-instance delivery gate would read absence as delivery failure.
    monkeypatch.setenv(_ENV, "config")
    resolved = resolve_tool_description_overrides(
        _make_config({"exec_command": "Override wording."})
    )
    assert resolved is not None

    registry = _make_registry()
    events: list[dict] = []
    for session_key in ("agent:main:s1", "agent:main:s2"):
        ctx = _ctx_with_resolved(
            ToolContext(
                is_owner=True,
                session_key=session_key,
                on_runtime_event=events.append,
            ),
            resolved,
        )
        registry.to_tool_definitions(ctx)
        registry.to_tool_definitions(ctx)

    assert [event["session_key"] for event in events] == [
        "agent:main:s1",
        "agent:main:s2",
    ]


def test_dotted_registered_tool_name_does_not_rewrite_param_of_prefix_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A whole-description key for registered tool "web.search" must not ALSO
    # rewrite parameter "search" of a tool named "web": exact-name matches are
    # excluded from the dotted split, mirroring the event accounting.
    monkeypatch.setenv(_ENV, "config")
    resolved = resolve_tool_description_overrides(
        _make_config({"web.search": "Override wording."})
    )
    assert resolved is not None

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="web",
            description="Web multiplexer.",
            parameters={"search": {"type": "string", "description": "Query."}},
            required=["search"],
        ),
        _noop,
    )
    registry.register(
        ToolSpec(
            name="web.search",
            description="Search the web.",
            parameters={"query": {"type": "string", "description": "Query."}},
            required=["query"],
        ),
        _noop,
    )
    ctx = _ctx_with_resolved(ToolContext(is_owner=True), resolved)
    by_name = {d.name: d for d in registry.to_tool_definitions(ctx)}

    assert by_name["web.search"].description.startswith("Override wording.")
    assert by_name["web"].input_schema.properties["search"]["description"] == "Query."
