"""Evidence contract for turns that execute action-capable tools."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Literal

from opensquilla.execution_status import normalize_execution_status
from opensquilla.provider import ToolDefinition, ToolInputSchema
from opensquilla.sandbox.command_policy import parse_shell_segments
from opensquilla.sandbox.operation_profile import classify_command

CompletionEffect = Literal["unknown", "read_only", "action", "control"]

_READ_ONLY_PROCESS_ACTIONS = frozenset({"list", "poll", "wait", "log"})
_WINDOWS_READ_COMMANDS = frozenset(
    {"dir", "gc", "gci", "get-childitem", "get-content", "test-path"}
)
_READ_ONLY_SIMPLE_COMMANDS = frozenset(
    {
        "[",
        "arch",
        "basename",
        "cat",
        "date",
        "dirname",
        "du",
        "echo",
        "false",
        "file",
        "grep",
        "head",
        "id",
        "ls",
        "md5",
        "md5sum",
        "printf",
        "ps",
        "pwd",
        "readlink",
        "realpath",
        "rg",
        "sha1sum",
        "sha256sum",
        "stat",
        "tail",
        "test",
        "tree",
        "true",
        "type",
        "uname",
        "wc",
        "where",
        "whereis",
        "which",
    }
)
_READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {
        "blame",
        "cat-file",
        "count-objects",
        "describe",
        "diff",
        "for-each-ref",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "merge-base",
        "name-rev",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "show-ref",
        "status",
        "verify-commit",
        "verify-tag",
    }
)
def _command_name(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _git_subcommand(argv: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    index = 1
    value_options = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
    while index < len(argv):
        token = argv[index]
        if token in value_options:
            index += 2
            continue
        if token.startswith(("--git-dir=", "--work-tree=", "--namespace=")):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token.casefold(), argv[index + 1 :]
    return "", ()


def _git_call_is_read_only(argv: tuple[str, ...]) -> bool:
    subcommand, args = _git_subcommand(argv)
    unsafe_flags = {"--ext-diff", "--output", "--textconv"}
    if any(
        arg in unsafe_flags or arg.startswith("--output=")
        for arg in args
    ):
        return False
    if subcommand in _READ_ONLY_GIT_SUBCOMMANDS:
        return True
    if subcommand == "branch":
        safe_flags = {
            "-a",
            "--all",
            "-r",
            "--remotes",
            "--list",
            "--show-current",
            "-v",
            "-vv",
        }
        safe_prefixes = (
            "--list=",
            "--contains=",
            "--no-contains=",
            "--points-at=",
            "--format=",
        )
        return not args or all(
            arg in safe_flags or arg.startswith(safe_prefixes) for arg in args
        )
    if subcommand == "remote":
        return not args or args[0] in {"-v", "show", "get-url"}
    if subcommand == "config":
        return bool(args) and args[0] in {
            "--get",
            "--get-all",
            "--get-regexp",
            "--get-urlmatch",
            "--list",
            "-l",
        }
    if subcommand == "reflog":
        return not args or args[0] in {"show", "exists"}
    return False


def _shell_segment_is_read_only(source: str, argv: tuple[str, ...]) -> bool:
    # Parsing alone does not make redirection or nested commands read-only.
    if any(marker in source for marker in (">", "`", "$(")) or not argv:
        return False
    command = _command_name(argv[0])
    if command == "git":
        return _git_call_is_read_only(argv)
    if command == "find":
        mutating_flags = {
            "-delete",
            "-exec",
            "-execdir",
            "-fls",
            "-fprint",
            "-fprint0",
            "-fprintf",
            "-ok",
            "-okdir",
        }
        return not any(arg.casefold() in mutating_flags for arg in argv[1:])
    if command == "rg" and any(
        arg == "--pre" or arg.startswith("--pre=") for arg in argv[1:]
    ):
        return False
    if os.name == "nt" and command in _WINDOWS_READ_COMMANDS:
        return True
    if command in _READ_ONLY_SIMPLE_COMMANDS:
        return True
    profile = classify_command(argv)
    return (
        (
            profile.name in {"workspace_read", "package_query"}
            or profile.host_effect == "host_probe"
        )
        and not profile.requested_write_paths
        and not profile.high_impact
        and profile.host_effect in {None, "host_probe"}
    )


def resolve_exec_command_effect(arguments: Mapping[str, Any]) -> CompletionEffect:
    """Classify an executed shell call; uncertainty remains action-capable."""

    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return "action"
    try:
        segments = parse_shell_segments(
            command,
            platform="windows" if os.name == "nt" else None,
        )
    except ValueError:
        return "action"
    if all(
        _shell_segment_is_read_only(segment.source, segment.argv)
        for segment in segments
    ):
        return "read_only"
    return "action"


def resolve_tool_completion_effect(
    definition: ToolDefinition | None,
    arguments: Mapping[str, Any],
) -> CompletionEffect:
    """Resolve per-call effects, defaulting unknown successful tools to action."""

    resolver = definition.completion_effect_resolver if definition else None
    if resolver == "exec_command":
        return resolve_exec_command_effect(arguments)
    if resolver == "process":
        action = str(arguments.get("action") or "").strip().casefold()
        return "read_only" if action in _READ_ONLY_PROCESS_ACTIONS else "action"
    effect: CompletionEffect = (
        definition.completion_effect if definition else "unknown"
    )
    return "action" if effect == "unknown" else effect


def tool_result_confirms_success(result: Any) -> bool:
    """Return whether the dispatcher confirms this call actually succeeded."""

    if bool(getattr(result, "is_error", False)):
        return False
    raw_status = getattr(result, "execution_status", None)
    if raw_status is None:
        # Legacy/custom handlers only return after dispatch, so a non-error
        # result with no sidecar is still an executed success receipt.
        return True
    status = normalize_execution_status(raw_status)
    if status["status"] == "success":
        return True
    if status["status"] != "unknown":
        return False
    reason = str(status.get("reason") or "")
    return status["source"] == "adapter" and reason == "background_running"

ACTION_COMPLETION_TOOL_NAME = "complete_action_task"
ACTION_COMPLETION_RECOVERY_LIMIT = 1
ACTION_COMPLETION_INCOMPLETE_CODE = "action_completion_incomplete"

ACTION_COMPLETION_CONTRACT_MESSAGE = (
    "[Action completion contract]\n"
    "This turn executed an action-capable tool. Do not end the turn with prose "
    "alone. If the user's requested action is complete, call "
    "complete_action_task with a concise user-visible summary. If work remains, "
    "call the next necessary tool. Do not repeat a successful tool merely to "
    "satisfy this contract."
)

ACTION_COMPLETION_RECOVERY_MESSAGE = (
    "[Action completion recovery]\n"
    "The previous response contained text but no completion evidence. Make one "
    "final decision now: call complete_action_task with the final user-visible "
    "summary if the requested action is complete, or call the next necessary "
    "tool if it is not. Do not repeat or replay a successful action."
)

ACTION_COMPLETION_INCOMPLETE_MESSAGE = (
    "The action task stopped without verifiable completion evidence after one "
    "recovery attempt. No tool was replayed automatically."
)


def action_completion_tool_definition() -> ToolDefinition:
    """Return the internal, side-effect-free completion evidence tool."""

    return ToolDefinition(
        name=ACTION_COMPLETION_TOOL_NAME,
        description=(
            "Declare that the user's requested action is complete. This tool has "
            "no side effects and must only be called after all requested actions "
            "and required verification are finished. Include the same final "
            "user-visible answer as ordinary text in this response."
        ),
        input_schema=ToolInputSchema(
            properties={
                "summary": {
                    "type": "string",
                    "description": "Concise user-visible summary of the completed action.",
                }
            },
            required=["summary"],
        ),
        completion_effect="control",
    )


class ActionCompletionIncompleteError(RuntimeError):
    """Durable terminal signal for an action turn lacking completion evidence."""

    code = ACTION_COMPLETION_INCOMPLETE_CODE
    terminal_reason = ACTION_COMPLETION_INCOMPLETE_CODE

    def __init__(self, message: str = ACTION_COMPLETION_INCOMPLETE_MESSAGE) -> None:
        super().__init__(message)


__all__ = [
    "ACTION_COMPLETION_CONTRACT_MESSAGE",
    "ACTION_COMPLETION_INCOMPLETE_CODE",
    "ACTION_COMPLETION_INCOMPLETE_MESSAGE",
    "ACTION_COMPLETION_RECOVERY_LIMIT",
    "ACTION_COMPLETION_RECOVERY_MESSAGE",
    "ACTION_COMPLETION_TOOL_NAME",
    "ActionCompletionIncompleteError",
    "action_completion_tool_definition",
    "resolve_exec_command_effect",
    "resolve_tool_completion_effect",
    "tool_result_confirms_success",
]
