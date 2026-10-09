from __future__ import annotations

from opensquilla.orchestration.models import (
    ActivationPhase,
    AgentActivationRecord,
    AgentSessionRecord,
    AgentState,
    OrchestrationMode,
    OrchestrationRunRecord,
    SessionLifecycle,
)
from opensquilla.orchestration.state import derive_agent_state


def _session() -> AgentSessionRecord:
    return AgentSessionRecord(
        session_id="session-1",
        run_id="run-1",
        profile="inherit",
        lifecycle=SessionLifecycle.ACTIVE,
    )


def _activation(
    phase: ActivationPhase,
    *,
    live_model_call: bool = False,
    live_tool_call: bool = False,
    external_wait_id: str | None = None,
) -> AgentActivationRecord:
    return AgentActivationRecord(
        activation_id="activation-1",
        session_id="session-1",
        task_id="task-1",
        phase=phase,
        live_model_call=live_model_call,
        live_tool_call=live_tool_call,
        external_wait_id=external_wait_id,
    )


def test_run_freezes_complex_mode_and_worker_template() -> None:
    run = OrchestrationRunRecord(
        run_id="run-1",
        root_session_id="root-1",
        root_task_id="task-root",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=frozenset({"read_file", "apply_patch"}),
    )

    assert run.mode is OrchestrationMode.COMPLEX
    assert run.worker_template_tools == frozenset({"read_file", "apply_patch"})


def test_stopping_has_precedence_over_live_execution() -> None:
    activation = _activation(
        ActivationPhase.STOPPING,
        live_model_call=True,
        live_tool_call=True,
    )

    assert (
        derive_agent_state(
            _session(),
            activation,
            inbox_count=1,
            live_descendants=1,
        )
        is AgentState.STOPPING
    )


def test_live_model_or_tool_execution_is_working() -> None:
    assert (
        derive_agent_state(
            _session(),
            _activation(ActivationPhase.RUNNING, live_tool_call=True),
            inbox_count=0,
            live_descendants=0,
        )
        is AgentState.WORKING
    )


def test_agent_without_own_execution_waits_for_live_descendants() -> None:
    assert (
        derive_agent_state(
            _session(),
            _activation(ActivationPhase.RUNNING),
            inbox_count=0,
            live_descendants=2,
        )
        is AgentState.WAITING_CHILDREN
    )


def test_external_wait_is_derived_from_a_real_wait_target() -> None:
    assert (
        derive_agent_state(
            _session(),
            _activation(
                ActivationPhase.RUNNING,
                external_wait_id="approval-1",
            ),
            inbox_count=0,
            live_descendants=0,
        )
        is AgentState.WAITING_EXTERNAL
    )


def test_queued_activation_or_inbox_work_is_queued() -> None:
    assert (
        derive_agent_state(
            _session(),
            _activation(ActivationPhase.QUEUED),
            inbox_count=0,
            live_descendants=0,
        )
        is AgentState.QUEUED
    )
    assert (
        derive_agent_state(
            _session(),
            None,
            inbox_count=1,
            live_descendants=0,
        )
        is AgentState.QUEUED
    )


def test_released_activation_leaves_persistent_session_idle() -> None:
    session = _session()
    activation = _activation(ActivationPhase.RELEASED)

    assert (
        derive_agent_state(
            session,
            activation,
            inbox_count=0,
            live_descendants=0,
        )
        is AgentState.IDLE
    )
    assert session.session_id == "session-1"


def test_running_without_work_wait_target_or_descendant_is_invalid() -> None:
    assert (
        derive_agent_state(
            _session(),
            _activation(ActivationPhase.RUNNING),
            inbox_count=0,
            live_descendants=0,
        )
        is AgentState.INVALID_STATE
    )
