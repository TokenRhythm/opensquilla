from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from opensquilla.engine.types import ToolCall
from opensquilla.session.plans import PlanRunConflictError
from opensquilla.tools.dispatch import build_tool_handler, preflight_tool_call
from opensquilla.tools.registry import ToolRegistry, get_default_registry, tool
from opensquilla.tools.types import (
    InteractionMode,
    PlanAccess,
    ToolContext,
    ToolSpec,
)


async def _ok() -> str:
    return "ok"


def _names(registry: ToolRegistry, ctx: ToolContext) -> set[str]:
    return {definition.name for definition in registry.to_tool_definitions(ctx)}


def test_plan_access_defaults_to_deny_and_decorator_preserves_metadata() -> None:
    registry = ToolRegistry()

    @tool(
        name="inspect",
        description="inspect",
        registry=registry,
        plan_access=PlanAccess.READ_ONLY,
        terminates_turn=True,
    )
    async def inspect() -> str:
        return "ok"

    assert ToolSpec(name="implicit", description="", parameters={}).plan_access is PlanAccess.DENY
    registered = registry.get("inspect")
    assert registered is not None
    assert registered.spec.plan_access is PlanAccess.READ_ONLY
    assert registered.spec.terminates_turn is True


def test_plan_visibility_is_fail_closed_but_default_visibility_is_unchanged() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="read",
            description="read",
            parameters={},
            plan_access=PlanAccess.READ_ONLY,
        ),
        _ok,
    )
    registry.register(
        ToolSpec(
            name="control",
            description="control",
            parameters={},
            plan_access=PlanAccess.CONTROL,
        ),
        _ok,
    )
    registry.register(ToolSpec(name="write", description="write", parameters={}), _ok)
    registry.register(
        ToolSpec(
            name="plugin.write",
            description="plugin",
            parameters={},
            exposed_by_default=False,
        ),
        _ok,
    )

    default_ctx = ToolContext(
        allowed_tools={"read", "control", "write", "plugin.write"},
        surfaced_tools={"plugin.write"},
        run_mode="full",
        elevated="full",
    )
    assert _names(registry, default_ctx) == {"read", "control", "write", "plugin.write"}

    plan_ctx = ToolContext(
        collaboration_mode="plan",
        allowed_tools={"read", "control", "write", "plugin.write"},
        surfaced_tools={"write", "plugin.write"},
        run_mode="full",
        elevated="full",
    )
    assert _names(registry, plan_ctx) == {"read", "control"}


@pytest.mark.asyncio
async def test_plan_dispatch_denial_precedes_validation_hooks_and_handler() -> None:
    registry = ToolRegistry()
    handler_calls: list[str] = []
    hook_calls: list[str] = []

    async def _write(path: str) -> str:
        handler_calls.append(path)
        return "wrote"

    class Hook:
        name = "recorder"

        def before_tool(self, _call) -> None:
            hook_calls.append("before")

        def after_tool(self, _call, _result) -> None:
            hook_calls.append("after")

    registry.register(
        ToolSpec(
            name="write",
            description="write",
            parameters={"path": {"type": "string"}},
            required=["path"],
        ),
        _write,
    )
    ctx = ToolContext(
        collaboration_mode="plan",
        allowed_tools={"write"},
        surfaced_tools={"write"},
        run_mode="full",
        elevated="full",
    )
    result = await build_tool_handler(registry, ctx, tool_hooks=[Hook()])(
        ToolCall(tool_use_id="p1", tool_name="write", arguments={})
    )

    assert result.is_error is True
    assert result.execution_status is not None
    assert result.execution_status["reason"] == "plan_mode_denied"
    assert json.loads(result.content)["error_class"] == "PolicyDenied"
    assert hook_calls == []
    assert handler_calls == []


@pytest.mark.asyncio
async def test_standalone_preflight_uses_same_plan_boundary() -> None:
    registry = ToolRegistry()
    registry.register(ToolSpec(name="plugin.write", description="plugin", parameters={}), _ok)

    result = await preflight_tool_call(
        registry=registry,
        ctx=ToolContext(
            collaboration_mode="plan",
            allowed_tools={"plugin.write"},
            surfaced_tools={"plugin.write"},
        ),
        tool_call=ToolCall(
            tool_use_id="p2",
            tool_name="plugin.write",
            arguments={},
        ),
    )

    assert result is not None
    assert result.execution_status is not None
    assert result.execution_status["reason"] == "plan_mode_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("access", [PlanAccess.READ_ONLY, PlanAccess.CONTROL])
async def test_explicit_plan_access_continues_through_existing_policy(
    access: PlanAccess,
) -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="allowed",
            description="allowed",
            parameters={},
            plan_access=access,
        ),
        _ok,
    )

    result = await build_tool_handler(
        registry,
        ToolContext(collaboration_mode="plan"),
    )(ToolCall(tool_use_id="p3", tool_name="allowed", arguments={}))

    assert result.is_error is False
    assert result.content == "ok"


@pytest.mark.asyncio
async def test_spec_terminates_turn_is_applied_by_finalizer() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="finish",
            description="finish",
            parameters={},
            terminates_turn=True,
        ),
        _ok,
    )

    result = await build_tool_handler(registry, ToolContext())(
        ToolCall(tool_use_id="p4", tool_name="finish", arguments={})
    )

    assert result.terminates_turn is True


@pytest.mark.asyncio
async def test_terminating_control_ends_turn_only_after_success() -> None:
    registry = ToolRegistry()

    @tool(
        name="finish",
        description="finish",
        registry=registry,
        terminates_turn=True,
    )
    async def finish(value: str) -> str:
        if value == "fail":
            raise ValueError("rejected")
        return "ok"

    failed = await build_tool_handler(registry, ToolContext())(
        ToolCall(
            tool_use_id="p5",
            tool_name="finish",
            arguments={"value": "fail"},
        )
    )

    assert failed.is_error is True
    assert failed.terminates_turn is False


@pytest.mark.asyncio
async def test_request_user_input_emits_canonical_interactive_protocol() -> None:
    # Import registers the built-in control in the process default registry.
    from opensquilla.tools.builtin import plan_control as _plan_control  # noqa: F401

    registered = get_default_registry().get("request_user_input")
    assert registered is not None
    registry = ToolRegistry()
    registry.register(registered.spec, registered.handler)
    ctx = ToolContext(
        collaboration_mode="plan",
        interaction_mode=InteractionMode.INTERACTIVE,
        task_id="plan-turn-1",
        session_key="agent:main:webchat:plan-input",
        allowed_tools={"request_user_input"},
        surfaced_tools={"request_user_input"},
    )

    result = await build_tool_handler(registry, ctx)(
        ToolCall(
            tool_use_id="p6",
            tool_name="request_user_input",
            arguments={
                "questions": [
                    {
                        "id": "scope",
                        "header": "Scope",
                        "question": "Which scope should the plan cover?",
                        "options": [
                            {
                                "label": "Core",
                                "description": "Implement only the shared runtime.",
                            },
                            {"label": "Full"},
                        ],
                    }
                ]
            },
        )
    )

    assert result.is_error is False
    assert result.terminates_turn is True
    payload = json.loads(result.content)
    assert payload["kind"] == "user_input"
    assert payload["paused"] is True
    assert payload["run_id"] == "plan-turn-1"
    assert payload["step"] == "plan"
    assert payload["clarify_schema"]["fields"] == [
        {
            "name": "scope",
            "prompt": "Which scope should the plan cover?",
                "type": "enum",
                "required": True,
                "choices": ["Core", "Full"],
                "header": "Scope",
                "options": [
                    {
                        "label": "Core",
                        "description": "Implement only the shared runtime.",
                    },
                    {"label": "Full"},
                ],
                "allow_other": True,
            }
        ]
    assert payload["questions"][0]["header"] == "Scope"
    assert payload["questions"][0]["options"][0]["description"].startswith(
        "Implement only"
    )


@pytest.mark.asyncio
async def test_request_user_input_failure_does_not_end_plan_turn() -> None:
    from opensquilla.tools.builtin import plan_control as _plan_control  # noqa: F401

    registered = get_default_registry().get("request_user_input")
    assert registered is not None
    registry = ToolRegistry()
    registry.register(registered.spec, registered.handler)
    ctx = ToolContext(
        collaboration_mode="plan",
        interaction_mode=InteractionMode.UNATTENDED,
        allowed_tools={"request_user_input"},
        surfaced_tools={"request_user_input"},
    )

    result = await build_tool_handler(registry, ctx)(
        ToolCall(
            tool_use_id="p7",
            tool_name="request_user_input",
            arguments={
                "questions": [
                    {"id": "scope", "question": "Which scope should be used?"}
                ]
            },
        )
    )

    assert result.is_error is True
    assert result.terminates_turn is False


@pytest.mark.asyncio
async def test_out_of_order_plan_checkpoint_returns_actionable_retry_contract() -> None:
    from opensquilla.tools.builtin import plan_control as _plan_control  # noqa: F401

    registered = get_default_registry().get("plan_run_checkpoint")
    assert registered is not None
    registry = ToolRegistry()
    registry.register(registered.spec, registered.handler)

    current = SimpleNamespace(
        state_revision=3,
        status="running",
        current_step_id="step-3",
        active_task_id="implementation-task",
        step_states=[
            {"step_id": "step-1", "status": "completed"},
            {"step_id": "step-2", "status": "completed"},
            {"step_id": "step-3", "status": "in_progress"},
            {"step_id": "step-4", "status": "pending"},
            {"step_id": "step-5", "status": "pending"},
        ],
    )

    class OutOfOrderStorage:
        async def get_plan_run(self, run_id: str) -> SimpleNamespace:
            assert run_id == "run-1"
            return current

        async def checkpoint_plan_run(self, *_args, **_kwargs) -> None:
            raise PlanRunConflictError(
                "only the current plan step may be checkpointed; storage-secret"
            )

    ctx = ToolContext(
        collaboration_mode="default",
        task_id="implementation-task",
        session_key="agent:main:webchat:checkpoint-recovery",
        plan_run_id="run-1",
        plan_storage=OutOfOrderStorage(),
        allowed_tools={"plan_run_checkpoint"},
        surfaced_tools={"plan_run_checkpoint"},
    )
    result = await build_tool_handler(registry, ctx)(
        ToolCall(
            tool_use_id="checkpoint-out-of-order",
            tool_name="plan_run_checkpoint",
            arguments={
                "step_id": "step-5",
                "step_status": "completed",
            },
        )
    )

    assert result.is_error is True
    assert result.terminates_turn is False
    envelope = json.loads(result.content)
    assert envelope["error_class"] == "RetryableToolInputError"
    assert envelope["retry_allowed"] is True
    recovery = json.loads(envelope["user_message"])
    assert recovery["error"] == "plan_checkpoint_conflict"
    assert recovery["requested_step_id"] == "step-5"
    assert recovery["plan_run_status"] == "running"
    assert recovery["current_step"] == {
        "step_id": "step-3",
        "status": "in_progress",
    }
    assert recovery["recovery"]["action"] == "checkpoint_current_step"
    assert recovery["recovery"]["step_id"] == "step-3"
    assert "one at a time in plan order" in recovery["recovery"]["instruction"]
    assert "storage-secret" not in result.content
    assert "internal error" not in result.content


@pytest.mark.asyncio
async def test_plan_checkpoint_event_failure_does_not_undo_committed_state() -> None:
    from opensquilla.tools.builtin import plan_control as _plan_control  # noqa: F401

    registered = get_default_registry().get("plan_run_checkpoint")
    assert registered is not None
    registry = ToolRegistry()
    registry.register(registered.spec, registered.handler)
    updated = SimpleNamespace(
        run_id="run-committed",
        plan_revision_id="revision-1",
        status="running",
        current_step_id=None,
        step_states=[
            {
                "step_id": "step-1",
                "title": "Finish",
                "status": "completed",
            }
        ],
        state_revision=8,
        driver_kind="manual",
        driver_id=None,
        active_task_id="implementation-task",
        pause_reason=None,
        terminal_reason=None,
        created_at=100,
        updated_at=200,
        started_at=120,
        finished_at=None,
    )

    class Storage:
        def __init__(self) -> None:
            self.checkpoint_calls = 0

        async def get_plan_run(self, run_id: str) -> SimpleNamespace:
            assert run_id == updated.run_id
            return SimpleNamespace(state_revision=7)

        async def checkpoint_plan_run(
            self,
            run_id: str,
            **kwargs: object,
        ) -> SimpleNamespace:
            assert run_id == updated.run_id
            assert kwargs["expected_state_revision"] == 7
            self.checkpoint_calls += 1
            return updated

    storage = Storage()
    emitted: list[tuple[str, str, dict[str, object]]] = []

    async def failing_emitter(
        session_key: str,
        event_name: str,
        payload: dict[str, object],
    ) -> None:
        emitted.append((session_key, event_name, payload))
        raise RuntimeError("subscriber disconnected")

    ctx = ToolContext(
        collaboration_mode="default",
        task_id="implementation-task",
        session_key="agent:main:webchat:checkpoint-committed",
        plan_run_id=updated.run_id,
        plan_storage=storage,
        plan_event_emitter=failing_emitter,
        allowed_tools={"plan_run_checkpoint"},
        surfaced_tools={"plan_run_checkpoint"},
    )
    result = await build_tool_handler(registry, ctx)(
        ToolCall(
            tool_use_id="checkpoint-committed",
            tool_name="plan_run_checkpoint",
            arguments={
                "step_id": "step-1",
                "step_status": "completed",
            },
        )
    )

    assert result.is_error is False
    assert storage.checkpoint_calls == 1
    assert len(emitted) == 1
    payload = json.loads(result.content)
    assert payload["status"] == "checkpoint_recorded"
    assert payload["plan_run"]["status"] == "running"
    assert payload["plan_run"]["currentStepId"] is None
    assert payload["plan_run"]["stateRevision"] == 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_status", "terminates_turn"),
    [
        ("running", False),
        ("blocked", True),
        ("completed", False),
    ],
)
async def test_only_blocked_plan_checkpoint_terminates_turn(
    run_status: str,
    terminates_turn: bool,
) -> None:
    registry = ToolRegistry()

    async def checkpoint() -> str:
        return json.dumps(
            {
                "status": "checkpoint_recorded",
                "plan_run": {"status": run_status},
            }
        )

    registry.register(
        ToolSpec(
            name="plan_run_checkpoint",
            description="checkpoint",
            parameters={},
        ),
        checkpoint,
    )

    result = await build_tool_handler(registry, ToolContext())(
        ToolCall(
            tool_use_id=f"checkpoint-{run_status}",
            tool_name="plan_run_checkpoint",
            arguments={},
        )
    )

    assert result.is_error is False
    assert result.terminates_turn is terminates_turn
