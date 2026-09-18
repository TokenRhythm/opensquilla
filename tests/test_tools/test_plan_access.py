from __future__ import annotations

import json

import pytest

from opensquilla.engine.types import ToolCall
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


def test_plan_visibility_uses_ordinary_tool_policy() -> None:
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
            default_access="deny",
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
    assert _names(registry, plan_ctx) == _names(registry, default_ctx)
    plan_ctx.denied_tools.add("write")
    assert "write" not in _names(registry, plan_ctx)


@pytest.mark.asyncio
async def test_plan_dispatch_preserves_explicit_denial_before_handler() -> None:
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
        denied_tools={"write"},
        surfaced_tools={"write"},
        run_mode="full",
        elevated="full",
    )
    result = await build_tool_handler(registry, ctx, tool_hooks=[Hook()])(
        ToolCall(tool_use_id="p1", tool_name="write", arguments={"path": "example.txt"})
    )

    assert result.is_error is True
    assert result.execution_status is not None
    assert result.execution_status["reason"] != "plan_mode_denied"
    assert json.loads(result.content)["error_class"] == "PolicyDenied"
    assert hook_calls == ["before", "after"]
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

    assert result is None


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


def test_plan_control_schema_exposes_runtime_limits_and_server_owned_next_step() -> None:
    from opensquilla.tools.builtin import plan_control as _plan_control  # noqa: F401

    request = get_default_registry().get("request_user_input")
    submit = get_default_registry().get("submit_plan")
    checkpoint = get_default_registry().get("plan_run_checkpoint")
    assert request is not None
    assert submit is not None
    assert checkpoint is not None

    questions = request.spec.parameters["questions"]
    assert questions["minItems"] == 1
    assert questions["maxItems"] == 3
    assert questions["items"]["properties"]["options"]["minItems"] == 2
    assert questions["items"]["properties"]["options"]["maxItems"] == 3

    steps = submit.spec.parameters["steps"]
    assert steps["minItems"] == 1
    assert steps["maxItems"] == 64
    assert steps["items"]["properties"]["step_id"]["maxLength"] == 128
    assert "next_step_id" not in checkpoint.spec.parameters
    assert checkpoint.spec.parameters["step_id"]["maxLength"] == 128
    assert checkpoint.spec.parameters["reason"]["maxLength"] == 2_000


@pytest.mark.asyncio
async def test_duplicate_user_input_option_labels_return_retryable_correction() -> None:
    from opensquilla.tools.builtin import plan_control as _plan_control  # noqa: F401

    registered = get_default_registry().get("request_user_input")
    assert registered is not None
    registry = ToolRegistry()
    registry.register(registered.spec, registered.handler)
    ctx = ToolContext(
        collaboration_mode="plan",
        interaction_mode=InteractionMode.INTERACTIVE,
        task_id="plan-turn-duplicate-options",
        session_key="agent:main:webchat:plan-input-duplicates",
        allowed_tools={"request_user_input"},
        surfaced_tools={"request_user_input"},
    )

    result = await build_tool_handler(registry, ctx)(
        ToolCall(
            tool_use_id="duplicate-options",
            tool_name="request_user_input",
            arguments={
                "questions": [
                    {
                        "id": "scope",
                        "question": "Which scope should be used?",
                        "options": [
                            {"label": "Core"},
                            {"label": "Core"},
                        ],
                    }
                ]
            },
        )
    )

    assert result.is_error is True
    assert result.terminates_turn is False
    envelope = json.loads(result.content)
    assert envelope["error_class"] == "RetryableToolInputError"
    assert envelope["retry_allowed"] is True


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
@pytest.mark.parametrize(
    ("run_status", "terminates_turn"),
    [
        ("running", False),
        ("blocked", False),
        ("completed", False),
    ],
)
async def test_checkpoint_progress_never_terminates_turn(
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
