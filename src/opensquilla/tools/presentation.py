"""Tool-registry adapter for the shared presentation contract."""

from __future__ import annotations

from opensquilla.contracts.tool_presentation import (
    ToolPresentationRule,
    project_tool_arguments,
    project_tool_arguments_payload,
    resolve_tool_presentation_fields,
)
from opensquilla.tools.types import ToolSpec


def _parameter_names(spec: ToolSpec) -> tuple[str, ...]:
    parameters = spec.parameters
    if parameters.get("type") == "object" and isinstance(
        parameters.get("properties"), dict
    ):
        parameters = parameters["properties"]
    return tuple(str(name) for name in parameters)


def resolve_tool_presentation(spec: ToolSpec) -> ToolPresentationRule:
    """Resolve a registered tool without leaking ToolSpec into lower layers."""

    return resolve_tool_presentation_fields(
        name=spec.name,
        parameter_names=_parameter_names(spec),
        sandbox_kind=spec.sandbox.kind,
        presentation_category=spec.presentation_category,
    )


__all__ = [
    "ToolPresentationRule",
    "project_tool_arguments",
    "project_tool_arguments_payload",
    "resolve_tool_presentation",
]
