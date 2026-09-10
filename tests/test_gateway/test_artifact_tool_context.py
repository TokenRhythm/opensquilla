from opensquilla.gateway.routing import (
    build_subagent_route_envelope,
    build_web_route_envelope,
    tool_context_from_envelope,
)


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
