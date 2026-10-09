from __future__ import annotations

from types import SimpleNamespace

from opensquilla.engine.subagent_delegation import (
    AGENT_CONTROL_TOOL_NAMES,
    COMPLEX_ROOT_PROMPT,
    DELEGATION_OWNERSHIP_PROMPT,
    PARENT_RETRIEVAL_TOOL_NAMES,
    SINGLE_SUBAGENT_EXECUTION_PROMPT,
    SUBAGENT_EXECUTION_PROMPT,
    apply_complex_root_tool_ceiling,
    completed_task_follow_up,
    render_subagent_delegation_prompt,
    resolve_subagent_delegation_policy,
)
from opensquilla.tools.types import CallerKind, ToolContext


def _tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def test_ordinary_mode_keeps_execution_tools_and_delegation_optional() -> None:
    context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        subagent_depth=0,
        orchestration_complex_mode=False,
    )
    authorized = frozenset({"read_file", "apply_patch", *AGENT_CONTROL_TOOL_NAMES})

    resolved = apply_complex_root_tool_ceiling(
        context,
        authorized_tool_names=authorized,
    )

    assert resolved.exclusive_tools is None
    assert resolved.orchestration_worker_template_tools is None
    assert resolve_subagent_delegation_policy(None) == "optional"


def test_complex_root_freezes_worker_template_before_two_tool_mask() -> None:
    context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        subagent_depth=0,
        orchestration_complex_mode=True,
    )
    authorized = frozenset({"read_file", "apply_patch", *AGENT_CONTROL_TOOL_NAMES})

    resolved = apply_complex_root_tool_ceiling(
        context,
        authorized_tool_names=authorized,
    )

    assert resolved.orchestration_worker_template_tools == authorized
    assert resolved.exclusive_tools == AGENT_CONTROL_TOOL_NAMES
    assert resolved.allowed_tools == set(AGENT_CONTROL_TOOL_NAMES)


def test_complex_root_keeps_authorized_result_retrieval() -> None:
    assert PARENT_RETRIEVAL_TOOL_NAMES == frozenset({"retrieve_tool_result"})
    context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        subagent_depth=0,
        orchestration_complex_mode=True,
    )
    authorized = frozenset(
        {
            "read_file",
            *AGENT_CONTROL_TOOL_NAMES,
            *PARENT_RETRIEVAL_TOOL_NAMES,
        }
    )

    resolved = apply_complex_root_tool_ceiling(
        context,
        authorized_tool_names=authorized,
    )

    expected = AGENT_CONTROL_TOOL_NAMES | PARENT_RETRIEVAL_TOOL_NAMES
    assert resolved.orchestration_worker_template_tools == authorized
    assert resolved.exclusive_tools == expected
    assert resolved.allowed_tools == set(expected)


def test_single_agent_root_has_only_delegate_and_interrupt_controls() -> None:
    context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        subagent_depth=0,
        orchestration_complex_mode=True,
    )
    context.orchestration_single_mode = True
    authorized = frozenset({"read_file", "apply_patch", *AGENT_CONTROL_TOOL_NAMES})

    resolved = apply_complex_root_tool_ceiling(
        context,
        authorized_tool_names=authorized,
    )

    assert resolved.orchestration_worker_template_tools == authorized
    assert resolved.exclusive_tools == frozenset({"delegate_task", "interrupt_agent"})


def test_single_agent_root_prompt_assigns_one_complete_task_per_child() -> None:
    context = ToolContext(
        caller_kind=CallerKind.AGENT,
        subagent_depth=0,
        orchestration_complex_mode=True,
    )
    context.orchestration_single_mode = True

    prompt = render_subagent_delegation_prompt(
        None,
        [_tool("delegate_task"), _tool("interrupt_agent")],
        context=context,
    )

    assert prompt.startswith("Complete Task Mode is active")
    assert "one complete task per child" in prompt
    assert "independent complete tasks" in prompt
    assert "Do not split" in prompt
    assert "first assignment must request the user's final outcome" in prompt
    assert "never delegate preparation alone" in prompt
    assert "preserve the user's wording" in prompt
    assert "short relevant prior context" in prompt
    assert "all requested tasks" in prompt
    assert "original user request to one child" not in prompt
    assert "Treat only the latest user message as the work order" in prompt
    assert "Old tasks are context, never backlog" in prompt
    assert (
        "Reuse an old child only when the latest message asks for the same task "
        "or a related change"
    ) in prompt
    assert "do not follow up, retry, or replace a child" in prompt
    assert "pass that child's session_id with a new task_key" not in prompt
    assert "Do not ask a completed child to paste its answer into the summary" in prompt
    assert "full deliverable" not in prompt


def test_single_child_reports_terminal_result_without_same_query_retry() -> None:
    assert "retry_same_agent=false" in SINGLE_SUBAGENT_EXECUTION_PROMPT
    assert "same user request" in SINGLE_SUBAGENT_EXECUTION_PROMPT
    assert "whether this same session can continue" not in SINGLE_SUBAGENT_EXECUTION_PROMPT


def test_complex_mask_does_not_propagate_to_child_context() -> None:
    child = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.SUBAGENT,
        subagent_depth=1,
        orchestration_complex_mode=True,
    )
    inherited = frozenset({"read_file", "apply_patch", *AGENT_CONTROL_TOOL_NAMES})

    resolved = apply_complex_root_tool_ceiling(
        child,
        authorized_tool_names=inherited,
    )

    assert resolved.exclusive_tools is None
    assert resolved.allowed_tools is None


def test_prompts_require_parallel_independent_work_without_duplicates() -> None:
    ordinary = render_subagent_delegation_prompt(
        None,
        [_tool("delegate_task"), _tool("read_file")],
        context=ToolContext(orchestration_complex_mode=False),
    )
    complex_prompt = render_subagent_delegation_prompt(
        None,
        [_tool("delegate_task"), _tool("interrupt_agent")],
        context=ToolContext(orchestration_complex_mode=True),
    )

    assert ordinary == ""
    assert "You are the team lead" in COMPLEX_ROOT_PROMPT
    assert "do not perform delegated work yourself" in COMPLEX_ROOT_PROMPT
    assert "return only summary results" in COMPLEX_ROOT_PROMPT
    assert "Decide from those summaries" in COMPLEX_ROOT_PROMPT
    assert "ask that child to restate it" in COMPLEX_ROOT_PROMPT
    assert "do not open a verifier without a concrete unmet criterion" in COMPLEX_ROOT_PROMPT
    assert "do not request or read whole files or source code" in COMPLEX_ROOT_PROMPT
    assert "Delegate independent, non-overlapping tasks concurrently" in (
        DELEGATION_OWNERSHIP_PROMPT
    )
    assert "do not add requirements the user did not ask for" in (
        DELEGATION_OWNERSHIP_PROMPT
    )
    assert "Reuse a child session for related follow-up" in DELEGATION_OWNERSHIP_PROMPT
    assert "one worker session owns reading" not in complex_prompt
    assert complex_prompt.startswith(f"{COMPLEX_ROOT_PROMPT}\n\nAvailable child agents:")
    assert complex_prompt.endswith(DELEGATION_OWNERSHIP_PROMPT)
    assert COMPLEX_ROOT_PROMPT in complex_prompt
    assert render_subagent_delegation_prompt(None, [_tool("read_file")]) == ""


def test_child_keeps_technical_details_out_of_parent_progress_summary() -> None:
    assert "write a minimally complete version immediately after obtaining the required facts" in (
        SUBAGENT_EXECUTION_PROMPT
    )
    assert '"summary":"brief progress and next action for parent"' in (
        SUBAGENT_EXECUTION_PROMPT
    )
    assert "Return only a concise outcome to the parent" in SUBAGENT_EXECUTION_PROMPT
    assert "Keep technical details here for follow-up" in SUBAGENT_EXECUTION_PROMPT
    assert "do not copy whole files, source code, or raw search results" in (
        SUBAGENT_EXECUTION_PROMPT
    )


def test_completed_investigation_follow_up_offers_same_session_reuse() -> None:
    follow_up = completed_task_follow_up(
        session_id="agent-session-1",
        unresolved=[],
        profile="explorer",
    )

    assert "Report this step as completed" in follow_up
    assert "related execution" in follow_up
    assert 'delegate_task with session_id="agent-session-1" and agent=worker' in follow_up
    assert "new task_key" in follow_up
    assert "Do not request source files or create a replacement agent" in follow_up
    assert "Compare this result" not in follow_up
    assert "Never delegate a planned verification item" not in follow_up


def test_completed_worker_follow_up_reports_progress_without_technical_review() -> None:
    follow_up = completed_task_follow_up(
        session_id="agent-session-1",
        unresolved=[],
        profile="worker",
    )

    assert follow_up.startswith("Report this step as completed")
    assert "next planned task" in follow_up
    assert "Compare" not in follow_up


def test_complex_root_receives_a_source_free_investigation_to_worker_recipe() -> None:
    prompt = render_subagent_delegation_prompt(
        None,
        [_tool("delegate_task")],
        context=ToolContext(
            caller_kind=CallerKind.AGENT,
            subagent_depth=0,
            orchestration_complex_mode=True,
        ),
    )

    assert "return only summary results" in prompt
    assert "continue that session as worker" in prompt
    assert "instead of transferring source" in prompt


def test_complex_prompt_describes_available_child_agent_capabilities() -> None:
    context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        subagent_depth=0,
        orchestration_complex_mode=True,
        orchestration_worker_template_tools=frozenset(
            {
                "apply_patch",
                "delegate_task",
                "edit_file",
                "exec_command",
                "git_diff",
                "grep_search",
                "interrupt_agent",
                "read_file",
                "web_fetch",
                "web_search",
                "write_file",
            }
        ),
    )

    prompt = render_subagent_delegation_prompt(
        None,
        [_tool("delegate_task"), _tool("interrupt_agent")],
        context=context,
    )

    assert "Available child agents:" in prompt
    assert (
        "Child agents do the concrete work, including content changes when their "
        "profile permits them; the root does not"
    ) in prompt
    assert "- inherit:" in prompt
    assert "- worker:" in prompt
    assert "- explorer:" in prompt
    assert "- researcher:" in prompt
    assert "- reviewer:" in prompt
    assert "captured before the root orchestrator was restricted" in prompt
    assert "No image_generate tool is available" in prompt
    assert "image viewing and artifact publishing do not generate an image" in prompt
    assert "do not describe child-agent capabilities" in prompt
    assert "file reading" in prompt
    assert "file editing" in prompt
    assert "command execution" in prompt
    assert "web access" in prompt
    assert "delegation" in prompt
    assert "Choose a child whose listed capabilities cover every acceptance criterion" in prompt
    assert "Use worker or inherit for command execution, tests, or file edits" in prompt
    assert "explorer, researcher, and reviewer cannot run commands, tests, or edit files" in prompt
    assert "smallest profile" not in prompt


def test_complex_prompt_names_image_generation_only_when_tool_exists() -> None:
    context = ToolContext(
        caller_kind=CallerKind.AGENT,
        subagent_depth=0,
        orchestration_complex_mode=True,
        orchestration_worker_template_tools=frozenset(
            {"delegate_task", "image_generate", "publish_artifact"}
        ),
    )

    prompt = render_subagent_delegation_prompt(
        None,
        [_tool("delegate_task")],
        context=context,
    )

    assert "image generation" in prompt
    assert "No image_generate tool is available" not in prompt


def test_complex_prompt_lists_only_profiles_supported_by_worker_template() -> None:
    context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        subagent_depth=0,
        orchestration_complex_mode=True,
        orchestration_worker_template_tools=frozenset({"delegate_task", "interrupt_agent"}),
    )

    prompt = render_subagent_delegation_prompt(
        None,
        [_tool("delegate_task"), _tool("interrupt_agent")],
        context=context,
    )

    assert "- inherit:" in prompt
    assert "- worker:" not in prompt
    assert "- explorer:" not in prompt
    assert "- researcher:" not in prompt
    assert "- reviewer:" not in prompt


def test_child_prompt_orders_role_summary_and_result_contract() -> None:
    assert SUBAGENT_EXECUTION_PROMPT.startswith(
        "Your role: complete the assigned task. You own the concrete work; "
        "the parent only coordinates."
    )
    assert "investigation, changes, and checks your capabilities allow" not in (
        SUBAGENT_EXECUTION_PROMPT
    )
    assert "record start, completion, or blockers briefly" in SUBAGENT_EXECUTION_PROMPT
    assert "do not repeat completed work" in SUBAGENT_EXECUTION_PROMPT
    assert "Cover each assigned criterion in deliverable" in SUBAGENT_EXECUTION_PROMPT
    assert SUBAGENT_EXECUTION_PROMPT.index("Your role:") < (
        SUBAGENT_EXECUTION_PROMPT.index("Return only a concise outcome")
    ) < SUBAGENT_EXECUTION_PROMPT.index("Finish with exactly one JSON object")
    assert '"status":"completed|failed"' in SUBAGENT_EXECUTION_PROMPT
    assert '"deliverable":"key task output"' in SUBAGENT_EXECUTION_PROMPT
    assert '"unresolved":["specific unanswered question"]' in SUBAGENT_EXECUTION_PROMPT
    assert "Use status=completed only when every criterion is met" in SUBAGENT_EXECUTION_PROMPT
    assert "If any criterion cannot be met, use status=failed" in SUBAGENT_EXECUTION_PROMPT
    assert "Unable to complete: <exact reason>" in SUBAGENT_EXECUTION_PROMPT
    assert "Capability not supported: <capability or tool>" in SUBAGENT_EXECUTION_PROMPT
    assert "Set retry_same_agent=true only if this session can make progress" in (
        SUBAGENT_EXECUTION_PROMPT
    )


def test_turn_runner_masks_only_complex_root_after_authority_is_frozen() -> None:
    from opensquilla.engine.runtime import TurnRunner
    from opensquilla.tools.registry import ToolRegistry
    from opensquilla.tools.types import ToolSpec

    async def handler() -> str:
        return "ok"

    registry = ToolRegistry()
    for name in ("read_file", "delegate_task", "interrupt_agent", "task_board"):
        registry.register(
            ToolSpec(name=name, description=f"{name} tool", parameters={}),
            handler,
        )
    runner = TurnRunner(
        provider_selector=None,
        tool_registry=registry,
        session_manager=object(),
        config=SimpleNamespace(skills=SimpleNamespace(coding_mode=False)),
    )
    runner._apply_runtime_capability_denies = lambda context: context

    complex_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        subagent_depth=0,
        orchestration_complex_mode=True,
    )
    complex_definitions, _ = runner._build_tools(complex_context)
    ordinary_definitions, _ = runner._build_tools(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.AGENT,
            subagent_depth=0,
            orchestration_complex_mode=False,
        )
    )

    assert {tool.name for tool in complex_definitions} == AGENT_CONTROL_TOOL_NAMES
    assert complex_context.orchestration_worker_template_tools == frozenset(
        {"read_file", "delegate_task", "interrupt_agent", "task_board"}
    )
    assert {tool.name for tool in ordinary_definitions} == {
        "read_file",
        "delegate_task",
        "interrupt_agent",
        "task_board",
    }
