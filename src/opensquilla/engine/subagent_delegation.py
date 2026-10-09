"""Prompt and root-capability policy for delegated agent work."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from opensquilla.orchestration.profiles import PRESET_PROFILES

if TYPE_CHECKING:
    from opensquilla.tools.types import ToolContext

AGENT_CONTROL_TOOL_NAMES = frozenset({"delegate_task", "interrupt_agent", "task_board"})
PARENT_RETRIEVAL_TOOL_NAMES = frozenset({"retrieve_tool_result"})
PARENT_COORDINATION_TOOL_NAMES = AGENT_CONTROL_TOOL_NAMES | PARENT_RETRIEVAL_TOOL_NAMES

DELEGATION_OWNERSHIP_PROMPT = (
    "Set acceptance_criteria to the user's requested outcome, not a list of files; "
    "do not add requirements the user did not ask for. "
    "Delegate independent, non-overlapping tasks concurrently. "
    "Reuse a child session for related follow-up. If an explorer already has the relevant "
    "context and execution is next, continue that session as worker instead of transferring "
    "source or creating another agent."
)

COMPLEX_ROOT_PROMPT = (
    "Complex Task Mode is active. You are the team lead: decompose the user's goal, "
    "choose and dispatch capable child agents, track progress, and report it to the user; "
    "do not perform delegated work yourself. Child agents retain source code and execution "
    "details in their own sessions and return only summary results: completion status, "
    "key conclusions or artifacts, and unresolved items. Decide from those summaries "
    "whether to continue, ask the same child for missing information, "
    "reroute, or finish; do not request or read whole files or source code. "
    "If a child's report format is invalid, ask that child to restate it; "
    "do not open a verifier without a concrete unmet criterion."
)

SINGLE_ROOT_PROMPT = (
    "Complete Task Mode is active. Coordinate; do not do delegated work yourself. "
    "Treat only the latest user message as the work order. Old tasks are context, "
    "never backlog. Reuse an old child only when the latest message asks for the "
    "same task or a related change. "
    "Assign one complete task per child and preserve the user's wording where possible; "
    "include only short relevant prior context when needed. "
    "You may delegate independent complete tasks separately. Do not split one task "
    "into investigation, editing, and verification assignments. For each task, the "
    "first assignment must request the user's final outcome; never delegate preparation "
    "alone. Each child owns its "
    "task and necessary checks with inherited worker capabilities. Keep only status "
    "and short summaries in your context; full answers are shown to the user separately. "
    "Do not ask a completed child to paste its answer into the summary or copy it "
    "into your reply; report completion briefly. "
    "Dispatch independent complete tasks before waiting for results. For this user "
    "request, do not follow up, retry, or replace a child after any child returns; "
    "report terminal results and finish. "
    "Finish only after all requested tasks have terminal results."
)


def complete_root_public_reply(user_message: str) -> str:
    """Leave the full answer in the child's durable result, not the root's prose."""

    if any("\u4e00" <= char <= "\u9fff" for char in user_message):
        return "子智能体已返回结果；完整内容请查看下方结果卡。"
    return "The child agent has returned its result; see the result card below."

SUBAGENT_EXECUTION_PROMPT = (
    "Your role: complete the assigned task. You own the concrete work; the parent only "
    "coordinates. Keep working details in this session. Meet every assigned acceptance "
    "criterion, add useful substeps to the "
    "shared task list, record start, completion, or blockers briefly, and do not repeat "
    "completed work. For a requested file, write a minimally complete version "
    "immediately after obtaining the required facts; only then consider optional "
    "research or checks. Stop when the criteria are met.\n"
    "Return only a concise outcome to the parent: what you did, the key result or artifact, "
    "unmet criteria, and the next action if needed. Cover each assigned criterion in deliverable, "
    "but do not copy whole files, source code, or raw search results. Keep technical details "
    "here for follow-up. The parent decides whether the overall user request is done.\n"
    "Finish with exactly one JSON object using these fields: "
    '{"status":"completed|failed","summary":"brief progress and next action for parent",'
    '"deliverable":"key task output","error":"failure reason or null",'
    '"retry_same_agent":true|false,"unresolved":["specific unanswered question"]}.\n'
    "Use status=completed only when every criterion is met; set error=null, "
    "retry_same_agent=false, and unresolved=[]. If any criterion cannot be met, use "
    'status=failed, error="Unable to complete: <exact reason>", and list only the missing '
    "criteria in unresolved. If a required tool or capability is unavailable, use "
    'error="Capability not supported: <capability or tool>". Set retry_same_agent=true only '
    "if this session can make progress after a follow-up; otherwise false."
)

SINGLE_SUBAGENT_EXECUTION_PROMPT = (
    "Your role: complete the assigned task. You own the concrete "
    "work and necessary checks; the parent only coordinates. Reuse completed work "
    "and stop when the requested result is ready. Return a short summary for the "
    "parent and the full final answer for the user in deliverable. Do not send the "
    "parent raw working notes or ask it to perform your task. Finish with exactly "
    "one JSON object using these fields: "
    '{"status":"completed|failed","summary":"short summary",'
    '"deliverable":"full final answer","error":"failure reason or null",'
    '"retry_same_agent":true|false,"unresolved":["specific missing result"]}. '
    "Set retry_same_agent=false for this assignment. For completion use "
    "status=completed, error=null, and unresolved=[]. If you cannot complete it, "
    "report the exact blocker; the parent will not retry within the same user request."
)


def completed_task_follow_up(
    *,
    session_id: str,
    unresolved: list[str] | None,
    profile: str,
) -> str:
    """Tell the parent how to consume one completed child assignment."""

    if not unresolved:
        handoff = (
            "If related execution is the next planned step, use a new task_key and call "
            f'delegate_task with session_id="{session_id}" and agent=worker. '
            "Do not request source files or create a replacement agent."
            if profile == "explorer"
            else (
                "Move to the next planned task; if none remains, summarize. Reuse this "
                "session when its context helps."
            )
        )
        return (
            "Report this step as completed with its short summary and current status. "
            f"{handoff}"
        )
    return (
        "Report this step as incomplete with its listed unresolved items. Continue "
        f'session_id="{session_id}" only for those items with a new task_key; do not '
        "add unrelated work or create a replacement agent."
    )


def failed_task_follow_up(
    *,
    task_key: str,
    session_id: str,
    retry_same_agent: bool,
) -> str:
    """Tell the parent exactly how to recover one failed child assignment."""

    if retry_same_agent:
        return (
            "If this task still needs work, you may retry it with delegate_task using "
            f'task_key="{task_key}" and session_id="{session_id}", asking only for the missing '
            "acceptance criterion. Otherwise reroute with the same task_key and "
            f'replace_session_id="{session_id}", or stop if the result is no longer needed.'
        )
    return (
        f'Do not retry this failed task in session_id="{session_id}". '
        "If the result is still required, reroute by calling delegate_task with the same "
        f'task_key="{task_key}" and replace_session_id="{session_id}"; otherwise stop.'
    )


_INHERITED_CAPABILITY_GROUPS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "file reading",
        frozenset(
            {
                "document_inspect",
                "document_locate",
                "document_read",
                "glob_search",
                "grep_search",
                "list_dir",
                "read_file",
                "read_source",
                "source_symbols",
            }
        ),
    ),
    (
        "file editing",
        frozenset(
            {
                "apply_patch",
                "create_source",
                "document_apply",
                "document_finish",
                "document_patch",
                "edit_file",
                "edit_source",
                "write_file",
            }
        ),
    ),
    (
        "command execution",
        frozenset({"background_process", "exec_command", "execute_code", "process"}),
    ),
    (
        "web access",
        frozenset(
            {
                "document_browser_act",
                "document_browser_inspect",
                "http_request",
                "web_discover",
                "web_fetch",
                "web_search",
            }
        ),
    ),
    (
        "document and artifact creation",
        frozenset(
            {
                "create_csv",
                "create_pdf_report",
                "create_pptx",
                "create_xlsx",
                "publish_artifact",
            }
        ),
    ),
    ("image generation", frozenset({"image_generate"})),
    ("delegation", AGENT_CONTROL_TOOL_NAMES),
)


def is_complex_root(context: ToolContext | None) -> bool:
    caller_kind = getattr(context, "caller_kind", None)
    caller_kind_value = getattr(caller_kind, "value", caller_kind)
    return bool(
        context is not None
        and context.orchestration_complex_mode
        and caller_kind_value != "subagent"
        and int(context.subagent_depth or 0) == 0
    )


def apply_complex_root_tool_ceiling(
    context: ToolContext,
    *,
    authorized_tool_names: frozenset[str],
) -> ToolContext:
    """Freeze the worker template before masking a Complex-mode root."""

    from opensquilla.tools.visibility import apply_exclusive_tool_ceiling

    if not is_complex_root(context):
        return context
    missing = AGENT_CONTROL_TOOL_NAMES - authorized_tool_names
    if missing:
        raise ValueError(
            "Complex Task Mode requires registered agent controls: " + ", ".join(sorted(missing))
        )
    context.orchestration_worker_template_tools = authorized_tool_names
    context.exclusive_tools = (
        frozenset({"delegate_task", "interrupt_agent"})
        if context.orchestration_single_mode
        else AGENT_CONTROL_TOOL_NAMES
        | (PARENT_RETRIEVAL_TOOL_NAMES & authorized_tool_names)
    )
    return apply_exclusive_tool_ceiling(context)


def resolve_subagent_delegation_policy(config: Any) -> str:
    """Compatibility-free projection used by older diagnostics."""

    del config
    return "optional"


def _summarize_inherited_capabilities(tool_names: frozenset[str]) -> str:
    capabilities = [
        label for label, members in _INHERITED_CAPABILITY_GROUPS if tool_names & members
    ]
    if not capabilities:
        return "the worker capabilities available in this session"
    return ", ".join(capabilities)


def _render_child_agent_capability_prompt(context: ToolContext | None) -> str:
    inherited = frozenset(getattr(context, "orchestration_worker_template_tools", None) or ())
    lines = [
        "Available child agents:",
        "The tools shown in your Available Tools list are only root orchestration "
        "controls and do not describe child-agent capabilities. Child agents start "
        "from the worker "
        "capabilities captured before the root orchestrator was restricted.",
        "Child agents do the concrete work, including content changes when their "
        "profile permits them; the root does not.",
    ]
    for profile in PRESET_PROFILES.values():
        if profile.name != "inherit" and not profile.required <= inherited:
            continue
        summary = profile.capability_summary
        if profile.name == "inherit":
            summary = (
                "inherits all available worker capabilities: "
                f"{_summarize_inherited_capabilities(inherited)}"
            )
        lines.append(f"- {profile.name}: {summary}.")
    lines.append(
        "Preset agents inherit from the same worker capabilities first, then apply "
        "their fixed capability filter."
    )
    if "image_generate" not in inherited:
        lines.append(
            "No image_generate tool is available; image viewing and artifact publishing "
            "do not generate an image."
        )
    lines.append(
        "Choose a child whose listed capabilities cover every acceptance criterion. "
        "Use worker or inherit for command execution, tests, or file edits; explorer, "
        "researcher, and reviewer cannot run commands, tests, or edit files."
    )
    return "\n".join(lines)


def render_subagent_delegation_prompt(
    config: Any,
    tool_defs: Sequence[Any] | None,
    *,
    context: ToolContext | None = None,
) -> str:
    del config
    if not tool_defs or not any(
        str(getattr(tool, "name", "") or "") == "delegate_task" for tool in tool_defs
    ):
        return ""
    if is_complex_root(context):
        if context is not None and context.orchestration_single_mode:
            return SINGLE_ROOT_PROMPT
        capability_prompt = _render_child_agent_capability_prompt(context)
        return f"{COMPLEX_ROOT_PROMPT}\n\n{capability_prompt}\n\n{DELEGATION_OWNERSHIP_PROMPT}"
    return ""


__all__ = [
    "AGENT_CONTROL_TOOL_NAMES",
    "COMPLEX_ROOT_PROMPT",
    "DELEGATION_OWNERSHIP_PROMPT",
    "PARENT_COORDINATION_TOOL_NAMES",
    "PARENT_RETRIEVAL_TOOL_NAMES",
    "SUBAGENT_EXECUTION_PROMPT",
    "SINGLE_SUBAGENT_EXECUTION_PROMPT",
    "apply_complex_root_tool_ceiling",
    "completed_task_follow_up",
    "failed_task_follow_up",
    "is_complex_root",
    "render_subagent_delegation_prompt",
    "resolve_subagent_delegation_policy",
]
