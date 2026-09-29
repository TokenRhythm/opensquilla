from __future__ import annotations

import opensquilla.tools.builtin.browser  # noqa: F401 - registers the builtin tool
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.schema_validation import tool_spec_schema_parts
from opensquilla.tools.types import ToolContext


def test_builtin_browser_root_required_field_is_visible_and_validated() -> None:
    registry = get_default_registry()
    browser = registry.get("browser")
    assert browser is not None
    _, required, additional_properties = tool_spec_schema_parts(browser.spec)
    assert required == ["operation"]
    assert additional_properties is False

    definitions = registry.to_tool_definitions(ToolContext(is_owner=True))
    definition = next(tool for tool in definitions if tool.name == "browser")
    assert definition.input_schema.required == ["operation"]
    assert definition.input_schema.model_dump(exclude_none=True, by_alias=True)[
        "additionalProperties"
    ] is False
