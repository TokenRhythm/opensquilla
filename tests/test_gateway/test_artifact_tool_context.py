from __future__ import annotations

from dataclasses import replace

import pytest

from opensquilla.artifact_session import (
    ArtifactCandidateLoopController,
    ArtifactMutationAttemptController,
    ArtifactSessionService,
)
from opensquilla.gateway.artifact_contexts import (
    DOCUMENT_CONTEXT_TOOL_NAMES,
    DOCUMENT_CONTEXT_WORKSPACE_MUTATOR_DENY,
    PROMPT_ANNOTATION_TOOL_NAMES,
    BoundDocumentContext,
    BoundPromptAnnotationContext,
)
from opensquilla.gateway.routing import (
    SourceKind,
    build_subagent_route_envelope,
    build_web_route_envelope,
    tool_context_from_envelope,
)
from opensquilla.tool_boundary import ToolCall
from opensquilla.tools.builtin.artifact_range_grants import registry_for_context
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import get_default_registry


def _prompt_annotation_context() -> BoundPromptAnnotationContext:
    return BoundPromptAnnotationContext(
        session_key="agent:main:web",
        session_id="session-1",
        document_id="document-1",
        revision_id="revision-1",
        annotation_ids=("annotation-1",),
        anchor_ids=("anchor-1",),
        snapshots=({"annotationId": "annotation-1"},),
        artifact_format="html",
        tool_names=PROMPT_ANNOTATION_TOOL_NAMES,
        operation_class="selection_edit",
        request_context_prompt="annotation context",
    )


def _document_context() -> BoundDocumentContext:
    return BoundDocumentContext(
        session_key="agent:main:web",
        session_id="session-1",
        document_id="document-1",
        revision_id="revision-1",
        artifact_format="html",
        tool_names=DOCUMENT_CONTEXT_TOOL_NAMES,
        operation_class="document_edit",
        request_context_prompt="current document context",
    )


def test_prompt_annotation_turn_gets_exclusive_tool_ceiling() -> None:
    envelope = build_web_route_envelope(session_key="agent:main:web")
    context = _prompt_annotation_context()
    envelope.runtime_services.update(
        {
            "artifact_context": context,
            "artifact_session": object(),
        }
    )

    result = tool_context_from_envelope(envelope, is_owner=True)

    expected = set(PROMPT_ANNOTATION_TOOL_NAMES)
    assert result.artifact_context is context
    assert result.surfaced_tools == expected
    assert result.exclusive_tools == expected
    assert isinstance(result.exclusive_tools, frozenset)
    assert result.allowed_tools == expected
    assert result.artifact_mutation_attempt_controller is None


def test_accepted_prompt_annotation_turn_gets_candidate_loop_controller() -> None:
    envelope = build_web_route_envelope(session_key="agent:main:web")
    context = _prompt_annotation_context()
    service = ArtifactSessionService(repository=object())  # type: ignore[arg-type]
    envelope.metadata["task_id"] = "turn-accepted-1"
    envelope.runtime_services.update(
        {
            "artifact_context": context,
            "artifact_session": service,
        }
    )

    result = tool_context_from_envelope(envelope, is_owner=True)

    assert isinstance(result.artifact_candidate_loop_controller, ArtifactCandidateLoopController)
    assert result.artifact_mutation_attempt_controller is None
    assert result.turn_cleanup_callbacks == []
    registry_for_context(result)
    assert len(result.turn_cleanup_callbacks) == 1
    result.turn_cleanup_callbacks[0]()
    assert getattr(result, "_artifact_range_grant_registry", None) is None


def test_bound_document_context_adds_tools_and_denies_workspace_mutator_substitutes() -> None:
    envelope = build_web_route_envelope(session_key="agent:main:web")
    context = _document_context()
    service = ArtifactSessionService(repository=object())  # type: ignore[arg-type]
    envelope.metadata["task_id"] = "turn-document-1"
    envelope.runtime_services.update(
        {
            "artifact_context": context,
            "artifact_session": service,
        }
    )

    result = tool_context_from_envelope(envelope, is_owner=True)

    assert result.artifact_context is context
    assert result.surfaced_tools == set(DOCUMENT_CONTEXT_TOOL_NAMES)
    assert result.exclusive_tools is None
    assert result.allowed_tools is None
    assert result.denied_tools == set(DOCUMENT_CONTEXT_WORKSPACE_MUTATOR_DENY)
    assert "read_file" not in result.denied_tools
    visible_names = {
        definition.name for definition in get_default_registry().to_tool_definitions(result)
    }
    assert DOCUMENT_CONTEXT_TOOL_NAMES <= visible_names
    assert not (DOCUMENT_CONTEXT_WORKSPACE_MUTATOR_DENY & visible_names)
    assert isinstance(
        result.artifact_mutation_attempt_controller,
        ArtifactMutationAttemptController,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("apply_patch", {"patch": "*** Begin Patch\n*** End Patch"}),
        ("edit_file", {"path": "current.html", "old_text": "old", "new_text": "new"}),
        ("write_file", {"path": "current.html", "content": "replacement"}),
    ],
)
async def test_bound_document_context_dispatch_rejects_workspace_mutators(
    tool_name: str,
    arguments: dict[str, str],
) -> None:
    envelope = build_web_route_envelope(session_key="agent:main:web")
    envelope.metadata["task_id"] = "turn-document-denied-writer"
    envelope.runtime_services.update(
        {
            "artifact_context": _document_context(),
            "artifact_session": ArtifactSessionService(repository=object()),  # type: ignore[arg-type]
        }
    )
    context = tool_context_from_envelope(envelope, is_owner=True)
    handler = build_tool_handler(get_default_registry(), context)

    result = await handler(
        ToolCall(
            tool_use_id=f"denied-{tool_name}",
            tool_name=tool_name,
            arguments=arguments,
        )
    )

    assert result.is_error is True
    assert f"Tool '{tool_name}' not available in this context." in result.content


def test_non_owner_cannot_surface_prompt_annotation_tools() -> None:
    async def emitter(_payload):
        return None

    envelope = build_web_route_envelope(session_key="agent:main:web")
    envelope.runtime_services.update(
        {
            "artifact_context": _prompt_annotation_context(),
            "artifact_session": object(),
            "artifact_event_emitter": emitter,
            "desktop_artifact_bridge": object(),
        }
    )

    result = tool_context_from_envelope(envelope, is_owner=False)

    assert result.surfaced_tools is None
    assert result.artifact_event_emitter is None
    assert result.desktop_artifact_bridge is None


def test_subagent_cannot_inherit_prompt_annotation_authority() -> None:
    async def emitter(_payload):
        return None

    envelope = build_subagent_route_envelope(
        parent_session_key="agent:main:web",
        session_key="agent:main:subagent:test",
        agent_id="main",
        run_id="run-1",
        parent_task_id="task-1",
    )
    envelope = replace(
        envelope,
        runtime_services={
            "artifact_context": _prompt_annotation_context(),
            "artifact_session": object(),
            "artifact_event_emitter": emitter,
            "desktop_artifact_bridge": object(),
        },
    )
    assert envelope.source_kind is SourceKind.SUBAGENT

    result = tool_context_from_envelope(envelope, is_owner=True)

    assert result.surfaced_tools is None
    assert result.artifact_event_emitter is None
    assert result.desktop_artifact_bridge is None


def test_generated_artifact_adopter_is_bound_only_to_interactive_owner_web_turn() -> None:
    async def adopter(_event):
        return None

    envelope = build_web_route_envelope(session_key="agent:main:web")
    envelope.runtime_services["generated_artifact_adopter"] = adopter

    owner = tool_context_from_envelope(envelope, is_owner=True)
    non_owner = tool_context_from_envelope(envelope, is_owner=False)
    assert owner.generated_artifact_adopter is adopter
    assert non_owner.generated_artifact_adopter is None

    subagent = build_subagent_route_envelope(
        parent_session_key="agent:main:web",
        session_key="agent:main:subagent:artifact-adopter",
        agent_id="main",
        run_id="run-artifact-adopter",
        parent_task_id="task-artifact-adopter",
    )
    subagent.runtime_services["generated_artifact_adopter"] = adopter
    assert (
        tool_context_from_envelope(
            subagent,
            is_owner=True,
        ).generated_artifact_adopter
        is None
    )
