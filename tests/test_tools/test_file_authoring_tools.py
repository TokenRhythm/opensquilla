"""The generic workspace tools replace the retired format-specific tools."""

from __future__ import annotations

import json

import pytest

from opensquilla.engine.types import ToolCall
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import CallerKind, ToolContext

_RETIRED_TOOLS = frozenset({"create_csv", "create_xlsx", "create_pptx", "create_pdf_report"})


@pytest.mark.parametrize("caller_kind", [CallerKind.WEB, CallerKind.CLI, CallerKind.CHANNEL])
def test_retired_file_tools_are_absent_from_catalog_model_and_search(
    caller_kind: CallerKind,
) -> None:
    registry = get_default_registry()
    ctx = ToolContext(is_owner=caller_kind is not CallerKind.CHANNEL, caller_kind=caller_kind)
    authorized = registry.to_tool_definitions(ctx)
    model_tools = registry.to_model_tool_definitions(authorized, ctx)

    assert _RETIRED_TOOLS.isdisjoint(registry.list_names())
    assert _RETIRED_TOOLS.isdisjoint(tool.name for tool in authorized)
    assert _RETIRED_TOOLS.isdisjoint(tool.name for tool in model_tools)
    assert ctx.tool_search_index is not None
    for name in _RETIRED_TOOLS:
        assert name not in {hit.name for hit in ctx.tool_search_index.search(name)}
    assert "publish_artifact" in {tool.name for tool in authorized}


@pytest.mark.parametrize("tool_name", sorted(_RETIRED_TOOLS))
@pytest.mark.parametrize("is_owner", [True, False])
async def test_retired_file_tools_cannot_be_called(tool_name: str, is_owner: bool) -> None:
    ctx = ToolContext(is_owner=is_owner, caller_kind=CallerKind.CHANNEL)
    result = await build_tool_handler(get_default_registry(), ctx)(
        ToolCall(tool_use_id="retired-tool", tool_name=tool_name, arguments={})
    )

    assert result.is_error is True
    payload = json.loads(result.content)
    assert payload["error_class"] == ("ToolNotFound" if is_owner else "PolicyDenied")
    assert ctx.published_artifacts == []
