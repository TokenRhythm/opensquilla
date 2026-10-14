from __future__ import annotations

import json

import pytest

from opensquilla.artifact_session import ArtifactSessionService
from opensquilla.artifacts import ArtifactStore
from opensquilla.gateway.generated_artifact_adoption import GeneratedArtifactAdopter
from opensquilla.tools.builtin.show_widget import show_widget
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import CallerKind, ToolContext, ToolError, current_tool_context


@pytest.fixture
async def widget_context(tmp_path):
    service = await ArtifactSessionService.open(tmp_path / "state.db")
    store = ArtifactStore(tmp_path / "media")
    adopter = GeneratedArtifactAdopter(
        service=service,
        store=store,
        session_key="agent:main:webchat:show-widget",
        session_id="show-widget",
    )
    context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        session_key=adopter.session_key,
        session_id=adopter.session_id,
        artifact_session_id=adopter.session_id,
        generated_artifact_adopter=adopter,
    )
    try:
        yield context, adopter
    finally:
        await service.close()


async def _call(context: ToolContext, **kwargs):
    token = current_tool_context.set(context)
    try:
        return json.loads(await show_widget(**kwargs))
    finally:
        current_tool_context.reset(token)


def test_show_widget_is_model_visible_and_schema_matches_upstream_contract():
    registry = get_default_registry()
    registered = registry.get("show_widget")
    assert registered is not None
    assert {"title", "widget_code", "loading_messages"} <= set(registered.spec.parameters)
    assert registered.spec.required == ["title", "widget_code"]


async def test_show_widget_publishes_immutable_artifact_receipt(widget_context):
    context, adopter = widget_context
    result = await _call(
        context,
        title="capacity_simulator",
        loading_messages=["正在准备控件"],
        widget_code='<style>button{color:navy}</style><button id="go">Go</button>',
    )
    assert result["ok"] is True
    assert result["previewStatus"] == "inline-ready"
    assert result["presentation"] == "inline"
    assert result["name"] == "capacity_simulator"
    assert result["artifactName"].startswith("capacity_simulator-")
    assert result["artifactName"].endswith(".html")
    assert result["marker"] == f"visualize:{result['documentId']}:{result['revisionId']}"
    assert result["loadingMessages"] == ["正在准备控件"]
    head = await adopter.service.get_document_head(result["documentId"])
    assert head.revision.revision_id == result["revisionId"]
    assert head.revision.artifact_sha256 == result["sha256"]
    assert head.revision.artifact_id == result["artifactId"]


@pytest.mark.parametrize(
    ("title", "widget_code", "message"),
    [
        ("", "<p>ok</p>", "title"),
        ("ok", "", "widget_code"),
        ("ok", "```html\n<p>bad</p>\n```", "VISUALIZE_HTML"),
    ],
)
async def test_show_widget_rejects_invalid_input_without_creating_document(
    widget_context, title, widget_code, message,
):
    context, adopter = widget_context
    with pytest.raises(ToolError, match=message):
        await _call(context, title=title, widget_code=widget_code)
    assert not await adopter.service.list_documents(
        session_key=context.session_key,
        session_id=context.session_id,
    )


async def test_show_widget_rejects_guest_or_unbound_context(widget_context):
    context, _ = widget_context
    context.guest_safe = True
    with pytest.raises(ToolError, match="context"):
        await _call(context, title="capacity", widget_code="<p>ok</p>")
