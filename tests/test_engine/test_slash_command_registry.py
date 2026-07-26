from __future__ import annotations

import pytest

from opensquilla.engine.commands import (
    DEFAULT_REGISTRY,
    CommandBusyPolicy,
    CommandCategory,
    CommandDef,
    CommandExecution,
    CommandPresentation,
    ExecutionKind,
    SlashCommandRegistry,
    Surface,
    parse_surface,
)


@pytest.mark.parametrize(
    ("raw", "surface"),
    [
        ("web_chat", Surface.WEB_CHAT),
        ("web", Surface.WEB_CHAT),
        ("cli_gateway", Surface.CLI_GATEWAY),
        ("tui", Surface.CLI_GATEWAY),
        ("cli", Surface.CLI_GATEWAY),
        ("cli_standalone", Surface.CLI_STANDALONE),
        ("channel", Surface.CHANNEL),
    ],
)
def test_parse_surface_accepts_new_and_legacy_names(raw: str, surface: Surface) -> None:
    assert parse_surface(raw) is surface


def test_usage_execution_surfaces_and_methods() -> None:
    cmd = DEFAULT_REGISTRY.find("/usage")
    assert cmd is not None

    assert cmd.surfaces == frozenset(
        {Surface.WEB_CHAT, Surface.CLI_GATEWAY, Surface.CHANNEL}
    )
    assert cmd.execution_for(Surface.CLI_STANDALONE) is None

    for surface in (Surface.WEB_CHAT, Surface.CLI_GATEWAY, Surface.CHANNEL):
        execution = cmd.execution_for(surface)
        assert execution is not None
        assert execution.kind is ExecutionKind.RPC
        assert execution.action == "usage.status"
        assert execution.rpc_method == "usage.status"


def test_deprecated_rpc_properties_project_channel_execution() -> None:
    cmd = DEFAULT_REGISTRY.find("/history", Surface.CHANNEL)
    assert cmd is not None

    assert cmd.rpc_method == "chat.history"
    assert cmd.rpc_params is not None
    envelope = type("Envelope", (), {"session_key": "session-123"})()
    assert cmd.rpc_params(envelope) == {"sessionKey": "session-123"}


def test_file_is_cli_gateway_local_action_only() -> None:
    cmd = DEFAULT_REGISTRY.find("/file", Surface.CLI_GATEWAY)
    assert cmd is not None
    assert cmd.surfaces == frozenset({Surface.CLI_GATEWAY})

    execution = cmd.execution_for(Surface.CLI_GATEWAY)
    assert execution is not None
    assert execution.kind is ExecutionKind.LOCAL
    assert execution.action == "cli.file"
    assert execution.rpc_method is None

    assert DEFAULT_REGISTRY.find("/file", Surface.WEB_CHAT) is None
    assert DEFAULT_REGISTRY.find("/file", Surface.CHANNEL) is None
    assert DEFAULT_REGISTRY.find("/file", Surface.CLI_STANDALONE) is None


def test_aliases_resolve_for_visible_surface_only() -> None:
    assert DEFAULT_REGISTRY.find("/clear", Surface.WEB_CHAT).name == "/reset"  # type: ignore[union-attr]
    assert DEFAULT_REGISTRY.find("/clear", Surface.CHANNEL).name == "/reset"  # type: ignore[union-attr]
    assert DEFAULT_REGISTRY.find("/session", Surface.CHANNEL).name == "/status"  # type: ignore[union-attr]
    assert DEFAULT_REGISTRY.find("/session", Surface.WEB_CHAT) is None


def test_registry_preserves_declaration_order() -> None:
    execution = {Surface.CLI_GATEWAY: CommandExecution(ExecutionKind.LOCAL, "test.action")}
    registry = SlashCommandRegistry(
        (
            CommandDef("/zebra", "/zebra", "First.", execution),
            CommandDef("/alpha", "/alpha", "Second.", execution),
        )
    )

    assert [cmd.name for cmd in registry.for_surface(Surface.CLI_GATEWAY)] == [
        "/zebra",
        "/alpha",
    ]


def test_cli_gateway_command_metadata_has_curated_order_and_compatibility() -> None:
    commands = DEFAULT_REGISTRY.for_surface(Surface.CLI_GATEWAY)
    palette = sorted(
        (cmd for cmd in commands if cmd.visible_by_default),
        key=lambda cmd: cmd.order,
    )

    assert [cmd.name for cmd in palette[:12]] == [
        "/model",
        "/strategy",
        "/sessions",
        "/new",
        "/permissions",
        "/status",
        "/compact",
        "/usage",
        "/theme",
        "/help",
        "/keys",
        "/exit",
    ]

    model = DEFAULT_REGISTRY.find("/model", Surface.CLI_GATEWAY)
    assert model is not None
    assert model.description_for(Surface.CLI_GATEWAY) == "Choose or inspect the session model."
    assert model.description_for(Surface.CHANNEL) == "List available models."
    assert model.usage_for(Surface.CLI_GATEWAY) == "/model [auto|status|name]"
    assert model.usage_for(Surface.CHANNEL) == "/model [name]"
    assert model.category is CommandCategory.CONTROL
    assert model.busy_policy is CommandBusyPolicy.IMMEDIATE
    assert model.presentation is CommandPresentation.PICKER
    assert [
        choice.value for choice in model.argument_choices_for(Surface.CLI_GATEWAY)
    ] == ["auto", "status"]
    assert model.argument_choices_for(Surface.CHANNEL) == ()
    model_execution = model.execution_for(Surface.CLI_GATEWAY)
    assert model_execution is not None
    assert model_execution.kind is ExecutionKind.LOCAL
    assert model_execution.action == "model.select"

    strategy = DEFAULT_REGISTRY.find("/strategy", Surface.CLI_GATEWAY)
    assert strategy is not None
    assert strategy.execution_for(Surface.CLI_GATEWAY) == CommandExecution(
        ExecutionKind.LOCAL,
        "model.routing.strategy",
    )
    assert DEFAULT_REGISTRY.find("/strategy", Surface.CLI_STANDALONE) is None

    router = DEFAULT_REGISTRY.find("/router", Surface.CLI_GATEWAY)
    ensemble = DEFAULT_REGISTRY.find("/ensemble", Surface.CLI_GATEWAY)
    assert router is not None and not router.visible_by_default
    assert ensemble is not None and not ensemble.visible_by_default

    models = DEFAULT_REGISTRY.find("/models", Surface.CLI_GATEWAY)
    forget = DEFAULT_REGISTRY.find("/forget", Surface.CLI_GATEWAY)
    assert models is not None and models.deprecated and not models.visible_by_default
    assert forget is not None and forget.deprecated and not forget.visible_by_default
    assert models.execution_for(Surface.CLI_GATEWAY) == CommandExecution(
        ExecutionKind.LOCAL,
        "models.list",
    )

    # Existing compatibility names keep resolving to their canonical commands.
    assert DEFAULT_REGISTRY.find("/clear", Surface.CLI_GATEWAY).name == "/reset"  # type: ignore[union-attr]
    assert DEFAULT_REGISTRY.find("/session", Surface.CLI_GATEWAY).name == "/status"  # type: ignore[union-attr]
    assert DEFAULT_REGISTRY.find("/quit", Surface.CLI_GATEWAY).name == "/exit"  # type: ignore[union-attr]
