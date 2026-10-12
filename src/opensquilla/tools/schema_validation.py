"""Small JSON-schema subset validator for tool call arguments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return True


def _path_label(path: str) -> str:
    return path or "arguments"


def _validate_value(value: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    enum = schema.get("enum")
    if isinstance(enum, Sequence) and not isinstance(enum, (str, bytes)):
        if value not in enum:
            errors.append(f"{_path_label(path)} must be one of {list(enum)!r}")
            return errors

    expected_type = schema.get("type")
    expected_types: list[str] = []
    if isinstance(expected_type, str):
        expected_types = [expected_type]
    elif isinstance(expected_type, Sequence) and not isinstance(expected_type, (str, bytes)):
        expected_types = [item for item in expected_type if isinstance(item, str)]
    if expected_types and not any(_matches_type(value, item) for item in expected_types):
        expected = "|".join(expected_types)
        errors.append(f"{_path_label(path)} expected {expected}, got {_type_name(value)}")
        return errors

    if isinstance(value, dict):
        nested_properties = schema.get("properties")
        nested_required = schema.get("required")
        additional = schema.get("additionalProperties")
        if isinstance(nested_properties, Mapping):
            errors.extend(
                validate_tool_arguments(
                    value,
                    properties=nested_properties,
                    required=(
                        [item for item in nested_required if isinstance(item, str)]
                        if isinstance(nested_required, Sequence)
                        and not isinstance(nested_required, (str, bytes))
                        else []
                    ),
                    additional_properties=additional,
                    path_prefix=path,
                )
            )
    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                errors.extend(_validate_value(item, item_schema, f"{path}[{index}]"))
    return errors


def validate_tool_arguments(
    arguments: Mapping[str, Any],
    *,
    properties: Mapping[str, Any] | None,
    required: Sequence[str] | None = None,
    additional_properties: bool | Mapping[str, Any] | None = None,
    path_prefix: str = "",
) -> list[str]:
    """Validate executable tool arguments against the supported schema subset."""

    errors: list[str] = []
    property_map = properties or {}
    required_names = [name for name in required or [] if isinstance(name, str)]
    for name in required_names:
        if name not in arguments:
            errors.append(f"{_path_label(f'{path_prefix}.{name}'.strip('.'))} is required")

    for name, value in arguments.items():
        path = f"{path_prefix}.{name}".strip(".")
        schema = property_map.get(name)
        if isinstance(schema, Mapping):
            errors.extend(_validate_value(value, schema, path))
            continue
        if additional_properties is False:
            errors.append(f"{_path_label(path)} is not allowed")
        elif isinstance(additional_properties, Mapping):
            errors.extend(_validate_value(value, additional_properties, path))
    return errors


def tool_spec_schema_parts(spec: Any) -> tuple[Mapping[str, Any], list[str], Any]:
    raw_parameters = getattr(spec, "parameters", None) or {}
    required = list(getattr(spec, "required", None) or [])
    additional_properties = None
    if isinstance(raw_parameters, Mapping) and raw_parameters.get("type") == "object":
        properties = raw_parameters.get("properties") or {}
        raw_required = raw_parameters.get("required")
        if isinstance(raw_required, Sequence) and not isinstance(raw_required, (str, bytes)):
            required = [item for item in raw_required if isinstance(item, str)]
        additional_properties = raw_parameters.get("additionalProperties")
    else:
        properties = raw_parameters
    if not isinstance(properties, Mapping):
        properties = {}
    return properties, required, additional_properties
