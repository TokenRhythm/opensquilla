from __future__ import annotations

from typing import Any

import pytest

from opensquilla.sandbox.operation_runtime import SandboxToolDescriptor
from opensquilla.tools.presentation import (
    project_tool_arguments,
    project_tool_arguments_payload,
    resolve_tool_presentation,
)
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolSpec


async def _handler(**_: Any) -> str:
    return "ok"


def _spec(
    name: str,
    parameters: dict[str, Any],
    *,
    sandbox_kind: str = "",
    presentation_category: str | None = None,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} description",
        parameters=parameters,
        sandbox=SandboxToolDescriptor.custom(kind=sandbox_kind),
        presentation_category=presentation_category,
    )


def test_specific_tool_override_precedes_declared_category() -> None:
    rule = resolve_tool_presentation(
        _spec(
            "web_search",
            {"query": {"type": "string"}},
            presentation_category="mutation",
        )
    )

    assert rule.category == "search"
    assert rule.primary_arguments == ()
    assert rule.argument_display == "primary"
    assert rule.lifecycle_display == "boundary"


def test_web_search_does_not_publish_query_as_display_arguments() -> None:
    arguments = {"query": "private search terms", "mode": "news"}
    rule = resolve_tool_presentation(_spec("web_search", arguments))

    assert project_tool_arguments(rule, arguments) == {}


def test_declared_category_does_not_invent_a_missing_primary_argument() -> None:
    rule = resolve_tool_presentation(
        _spec(
            "mcp_custom_replace",
            {
                "url": {"type": "string"},
                "replacement": {"type": "string"},
            },
            sandbox_kind="network.http",
            presentation_category="mutation",
        )
    )

    assert rule.category == "mutation"
    assert rule.primary_arguments == ()
    assert rule.argument_display == "all"
    assert rule.lifecycle_display == "default"


@pytest.mark.parametrize(
    ("name", "sandbox_kind", "parameters", "category", "primary_arguments"),
    [
        (
            "custom_reader",
            "fs.read",
            {"path": {"type": "string"}},
            "file_read",
            ("path",),
        ),
        (
            "custom_fetcher",
            "network.http",
            {"url": {"type": "string"}},
            "network_read",
            ("url",),
        ),
        (
            "custom_runner",
            "shell.exec",
            {"command": {"type": "string"}},
            "command",
            ("command",),
        ),
    ],
)
def test_sandbox_kind_selects_category_defaults(
    name: str,
    sandbox_kind: str,
    parameters: dict[str, Any],
    category: str,
    primary_arguments: tuple[str, ...],
) -> None:
    rule = resolve_tool_presentation(_spec(name, parameters, sandbox_kind=sandbox_kind))

    assert rule.category == category
    assert rule.primary_arguments == primary_arguments


@pytest.mark.parametrize(
    ("name", "parameters", "category", "primary_arguments"),
    [
        (
            "plugin_bulk_lookup",
            {"queries": {"type": "array"}},
            "search",
            ("queries",),
        ),
        (
            "plugin_download",
            {"url": {"type": "string"}},
            "network_read",
            ("url",),
        ),
        (
            "plugin_read",
            {"paths": {"type": "array"}},
            "file_read",
            ("paths",),
        ),
        (
            "plugin_write",
            {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "mutation",
            ("path",),
        ),
        (
            "plugin_delegate",
            {"task": {"type": "string"}},
            "subagent",
            ("task",),
        ),
        (
            "plugin_unknown",
            {"value": {"type": "string"}},
            "generic",
            (),
        ),
    ],
)
def test_parameter_shape_is_the_last_classification_fallback(
    name: str,
    parameters: dict[str, Any],
    category: str,
    primary_arguments: tuple[str, ...],
) -> None:
    rule = resolve_tool_presentation(_spec(name, parameters))

    assert rule.category == category
    assert rule.primary_arguments == primary_arguments


def test_tool_specific_primary_argument_exception_overrides_category_default() -> None:
    rule = resolve_tool_presentation(
        _spec(
            "grep_search",
            {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
        )
    )

    assert rule.category == "search"
    assert rule.primary_arguments == ("pattern", "path")


def test_argument_projection_keeps_only_primary_fields_without_mutating_input() -> None:
    arguments = {
        "url": "https://example.test/report",
        "headers": {"Authorization": "secret"},
        "body": "private request body",
    }
    rule = resolve_tool_presentation(
        _spec("http_request", arguments, sandbox_kind="network.http")
    )

    projected = project_tool_arguments(rule, arguments)

    assert projected == {"url": "https://example.test/report"}
    assert arguments["body"] == "private request body"


def test_argument_projection_preserves_all_fields_for_mutations() -> None:
    arguments = {"path": "src/app.py", "content": "print('ok')"}
    rule = resolve_tool_presentation(
        _spec("write_file", arguments, sandbox_kind="fs.write")
    )

    assert project_tool_arguments(rule, arguments) == arguments
    assert project_tool_arguments(rule, arguments) is not arguments


def test_serialized_projection_fails_closed_for_malformed_metadata() -> None:
    arguments = {"url": "https://example.test", "token": "secret"}

    assert project_tool_arguments_payload({}, arguments) == {}
    assert project_tool_arguments_payload(
        {"argumentDisplay": "primary"}, arguments
    ) == {}
    assert project_tool_arguments_payload(None, arguments) == arguments


@pytest.mark.parametrize(
    ("name", "parameters", "category", "primary_arguments"),
    [
        (
            "cron",
            {
                "action": {"type": "string"},
                "task": {"type": "string"},
            },
            "generic",
            (),
        ),
        (
            "retrieve_tool_result",
            {
                "handle": {"type": "string"},
                "query": {"type": "string"},
            },
            "generic",
            (),
        ),
        (
            "document_browser_reload",
            {},
            "generic",
            (),
        ),
        (
            "memory_delete",
            {"path": {"type": "string"}},
            "mutation",
            ("path",),
        ),
        (
            "skill_view",
            {
                "name": {"type": "string"},
                "file_path": {"type": "string"},
            },
            "file_read",
            ("name", "file_path"),
        ),
    ],
)
def test_semantic_exceptions_beat_misleading_parameter_names(
    name: str,
    parameters: dict[str, Any],
    category: str,
    primary_arguments: tuple[str, ...],
) -> None:
    rule = resolve_tool_presentation(_spec(name, parameters))

    assert rule.category == category
    assert rule.primary_arguments == primary_arguments


@pytest.mark.asyncio
async def test_tool_catalog_exposes_presentation_rule_without_changing_schema() -> None:
    registry = ToolRegistry()
    registry.register(
        _spec(
            "custom_reader",
            {
                "path": {"type": "string"},
                "offset": {"type": "integer"},
            },
            sandbox_kind="fs.read",
        ),
        _handler,
    )

    tools = await registry.list_tools()

    assert tools == [
        {
            "name": "custom_reader",
            "description": "custom_reader description",
            "schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                },
                "required": [],
            },
            "source": "builtin",
            "enabled": True,
            "presentation": {
                "category": "file_read",
                "primaryArguments": ["path"],
                "argumentDisplay": "primary",
                "lifecycleDisplay": "boundary",
            },
        }
    ]
