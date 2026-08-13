"""Evidence contract for turns that execute action-capable tools."""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from pathlib import PurePosixPath
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

_COMMON_QUERY_FLAGS = frozenset({"--help", "--version"})


def _simple_grammar(
    short_flags: str = "",
    short_values: str = "",
    long_flags: tuple[str, ...] = (),
    long_values: tuple[str, ...] = (),
) -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]:
    return (
        frozenset(short_flags),
        frozenset(short_values),
        _COMMON_QUERY_FLAGS.union(long_flags),
        frozenset(long_values),
    )


# Every allowlisted query command that accepts options has an explicit grammar.
# The table intentionally covers common portable query forms; unlisted options
# fail closed as action-capable instead of inheriting trust from the basename.
_SIMPLE_OPTION_GRAMMARS = {
    "basename": _simple_grammar("az", "s", ("--multiple", "--zero"), ("--suffix",)),
    "cat": _simple_grammar(
        "AbEenstTuv",
        long_flags=(
            "--show-all",
            "--number-nonblank",
            "--show-ends",
            "--number",
            "--squeeze-blank",
            "--show-tabs",
            "--show-nonprinting",
        ),
    ),
    "dirname": _simple_grammar("z", long_flags=("--zero",)),
    "du": _simple_grammar(
        "0abcdhHkLlmsx",
        "Btd",
        (
            "--all",
            "--apparent-size",
            "--bytes",
            "--count-links",
            "--dereference",
            "--human-readable",
            "--inodes",
            "--one-file-system",
            "--summarize",
            "--total",
        ),
        ("--block-size", "--max-depth", "--exclude", "--files0-from"),
    ),
    "file": _simple_grammar(
        "0bcdEiklNnprsSvzZ",
        "eFfmP",
        ("--brief", "--mime", "--mime-type", "--mime-encoding", "--raw"),
        ("--exclude", "--separator", "--files-from", "--magic-file", "--parameter"),
    ),
    "grep": _simple_grammar(
        "EFGPUVabcdhHhIiLlmnorsvwxZz",
        "ABCeefm",
        (
            "--fixed-strings",
            "--ignore-case",
            "--line-number",
            "--no-filename",
            "--only-matching",
            "--quiet",
            "--recursive",
            "--text",
            "--with-filename",
            "--word-regexp",
        ),
        ("--regexp", "--file", "--max-count", "--context", "--color"),
    ),
    "head": _simple_grammar("qvz#", "cn", ("--quiet", "--verbose"), ("--bytes", "--lines")),
    "id": _simple_grammar("Ggnruz", long_flags=("--group", "--groups", "--name", "--user")),
    "ls": _simple_grammar(
        "ABCDFGHLOPRSTUWabcdefghiklmnopqrstuwx1",
        "I",
        ("--all", "--almost-all", "--human-readable", "--recursive", "--size"),
        ("--block-size", "--color", "--format", "--ignore", "--sort", "--time"),
    ),
    "md5": _simple_grammar("pqrstx", "s"),
    "md5sum": _simple_grammar("bctwz", long_flags=("--check", "--quiet", "--status")),
    "ps": _simple_grammar(
        "AaCcdefgGhjLlMmnNoprsSTuvwxX",
        "OqUpt",
        ("--all", "--forest", "--no-headers"),
        ("--format", "--pid", "--ppid", "--sort", "--user"),
    ),
    "readlink": _simple_grammar("efmnqsvz", long_flags=("--canonicalize", "--zero")),
    "realpath": _simple_grammar(
        "eLmqsz",
        long_flags=("--canonicalize-existing", "--canonicalize-missing", "--zero"),
        long_values=("--relative-to", "--relative-base"),
    ),
    "rg": _simple_grammar(
        "aFhHiIlLnNoPqsSuvUwx0",
        "ABCdefgjMmrtT",
        (
            "--files",
            "--fixed-strings",
            "--heading",
            "--hidden",
            "--ignore-case",
            "--json",
            "--line-number",
            "--no-heading",
            "--no-ignore",
            "--no-messages",
            "--only-matching",
            "--pcre2",
            "--quiet",
            "--smart-case",
            "--stats",
            "--text",
            "--type-list",
            "--vimgrep",
            "--with-filename",
        ),
        (
            "--context",
            "--encoding",
            "--engine",
            "--glob",
            "--ignore-file",
            "--max-count",
            "--max-depth",
            "--regexp",
            "--replace",
            "--sort",
            "--type",
            "--type-add",
            "--type-not",
        ),
    ),
    "sha1sum": _simple_grammar("bctwz", long_flags=("--check", "--quiet", "--status")),
    "sha256sum": _simple_grammar("bctwz", long_flags=("--check", "--quiet", "--status")),
    "stat": _simple_grammar(
        "Lfct",
        long_flags=("--dereference", "--file-system", "--terse"),
        long_values=("--format", "--printf"),
    ),
    "tail": _simple_grammar(
        "qvzf#",
        "cn",
        ("--quiet", "--verbose"),
        ("--bytes", "--lines", "--pid", "--sleep-interval"),
    ),
    "tree": _simple_grammar(
        "aCdDfFghilnpqrstuvx",
        "L",
        ("--dirsfirst", "--filesfirst", "--noreport"),
        ("--charset", "--filelimit", "--sort", "--timefmt"),
    ),
    "uname": _simple_grammar(
        "amnoprsv",
        long_flags=("--all", "--kernel-name", "--kernel-release", "--machine"),
    ),
    "wc": _simple_grammar("clLmw", long_values=("--files0-from",)),
    "whereis": _simple_grammar("bBfLlmMsSu", "BMS"),
    "which": _simple_grammar("as", long_flags=("--all",)),
}
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

_POSIX_TRUSTED_EXECUTABLE_DIRS = frozenset(
    {"/bin", "/sbin", "/usr/bin", "/usr/sbin"}
)
_WINDOWS_TRUSTED_EXECUTABLE_DIRS = frozenset(
    {"c:/windows", "c:/windows/system32"}
)


def _trusted_command_name(token: str) -> str | None:
    """Return a command identity only when the shell lookup is trustworthy."""

    normalized = token.replace("\\", "/")
    if "/" not in normalized:
        return normalized.casefold()
    if normalized.startswith("/"):
        path = PurePosixPath(normalized)
        return path.name.casefold() if str(path.parent) in _POSIX_TRUSTED_EXECUTABLE_DIRS else None
    folded = normalized.casefold()
    if len(folded) >= 3 and folded[1:3] == ":/":
        path = PurePosixPath(folded)
        return (
            path.name.casefold()
            if str(path.parent) in _WINDOWS_TRUSTED_EXECUTABLE_DIRS
            else None
        )
    return None


def _has_active_shell_syntax(source: str) -> bool:
    """Detect executable shell syntax while ignoring quoted literal operators."""

    quote: str | None = None
    escaped = False
    index = 0
    while index < len(source):
        char = source[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if char == '"':
                quote = None
            elif char == "`" or source.startswith("$(", index):
                return True
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if (
            char == "`"
            or source.startswith("$(", index)
            or source.startswith("<(", index)
            or source.startswith(">(", index)
            or char in {"<", ">", "(", ")"}
        ):
            return True
        index += 1
    return False


def _outer_shell_argv(source: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(source, posix=os.name != "nt"))
    except ValueError:
        return ()


def _nested_shell_command(argv: tuple[str, ...]) -> str | None:
    if not argv:
        return None
    command = _trusted_command_name(argv[0])
    if command in {"bash", "sh", "zsh", "fish"}:
        for index, token in enumerate(argv[1:], start=1):
            if token in {"-c", "-lc"} and index + 1 < len(argv):
                return argv[index + 1]
        return None
    if command == "cmd":
        if len(argv) >= 3 and argv[1].casefold() in {"/c", "/k"}:
            return argv[2]
        return None
    if command in {"powershell", "pwsh"}:
        for index, token in enumerate(argv[1:], start=1):
            if token.casefold() in {"-c", "-command"} and index + 1 < len(argv):
                return argv[index + 1]
    return None


def _git_subcommand(argv: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    index = 1
    value_options = {"-C", "--git-dir", "--work-tree", "--namespace"}
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
    if "-c" in argv[1:] or any(arg.startswith("-c=") for arg in argv[1:]):
        return False
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


def _date_call_is_read_only(args: tuple[str, ...]) -> bool:
    safe_exact = {
        "-u",
        "--utc",
        "--universal",
        "-R",
        "--rfc-email",
        "--resolution",
        "--help",
        "--version",
    }
    safe_value_options = {"-d", "--date", "-r", "--reference", "-I", "--iso-8601"}
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("+") or token in safe_exact:
            index += 1
            continue
        if token in safe_value_options:
            if index + 1 >= len(args):
                return False
            index += 2
            continue
        if token.startswith(("--date=", "--reference=", "--iso-8601=")):
            index += 1
            continue
        return False
    return True


def _args_match_simple_option_grammar(
    args: tuple[str, ...],
    grammar: tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]],
    *,
    stop_options_at_first_operand: bool = False,
) -> bool:
    """Accept only audited options; operands themselves remain read-only inputs."""

    short_flags, short_value_options, long_flags, long_value_options = grammar
    index = 0
    options = True
    while index < len(args):
        token = args[index]
        if options and token == "--":
            options = False
            index += 1
            continue
        if not options or token == "-" or not token.startswith("-"):
            if stop_options_at_first_operand:
                options = False
            index += 1
            continue
        if token.startswith("--"):
            name, separator, _value = token.partition("=")
            if name in long_flags and not separator:
                index += 1
                continue
            if name not in long_value_options:
                return False
            if separator:
                index += 1
                continue
            if index + 1 >= len(args):
                return False
            index += 2
            continue
        if re.fullmatch(r"-\d+", token):
            # Historical count shorthand is valid only for head/tail; callers
            # admit it by including the synthetic '#' short flag.
            if "#" not in short_flags:
                return False
            index += 1
            continue
        cluster = token[1:]
        cluster_index = 0
        while cluster_index < len(cluster):
            option = cluster[cluster_index]
            if option in short_flags:
                cluster_index += 1
                continue
            if option not in short_value_options:
                return False
            if cluster_index + 1 < len(cluster):
                cluster_index = len(cluster)
                continue
            if index + 1 >= len(args):
                return False
            index += 1
            cluster_index += 1
        index += 1
    return True


def _simple_call_is_read_only(command: str, args: tuple[str, ...]) -> bool:
    """Resolve an allowlisted query command by its own option/operand grammar."""

    if command in {"[", "test"}:
        return command != "[" or bool(args) and args[-1] == "]"
    if command == "arch":
        # macOS arch executes an optional program. Only identity/help queries
        # are portable enough to classify as read-only.
        return not args or args in {("--help",), ("--version",)}
    if command == "date":
        return _date_call_is_read_only(args)
    if command in {"true", "false"}:
        return not args or args in {("--help",), ("--version",)}
    if command == "pwd":
        return all(
            arg in {"-L", "-P", "--logical", "--physical", "--help", "--version"} for arg in args
        )
    if command == "echo":
        return _args_match_simple_option_grammar(
            args,
            (frozenset("neE"), frozenset(), frozenset({"--help", "--version"}), frozenset()),
            stop_options_at_first_operand=True,
        )
    if command == "printf":
        return _args_match_simple_option_grammar(
            args,
            (frozenset(), frozenset(), frozenset({"--help", "--version"}), frozenset()),
            stop_options_at_first_operand=True,
        )
    if command == "type":
        return _args_match_simple_option_grammar(
            args,
            (frozenset("afptP"), frozenset(), frozenset(), frozenset()),
            stop_options_at_first_operand=True,
        )
    if command == "where":
        # Windows where /R executes no program but requires a directory value.
        index = 0
        while index < len(args):
            token = args[index].casefold()
            if token == "/r":
                if index + 1 >= len(args):
                    return False
                index += 2
                continue
            if token in {"/q", "/f", "/t", "/?"} or not token.startswith("/"):
                index += 1
                continue
            return False
        return True
    grammar = _SIMPLE_OPTION_GRAMMARS.get(command)
    return grammar is not None and _args_match_simple_option_grammar(args, grammar)


def _shell_segment_is_read_only(source: str, argv: tuple[str, ...], *, depth: int) -> bool:
    # Parsing alone does not make redirection or nested commands read-only.
    if depth > 4 or _has_active_shell_syntax(source) or not argv:
        return False
    outer_argv = _outer_shell_argv(source)
    if not outer_argv:
        return False
    outer_command = _trusted_command_name(outer_argv[0])
    if outer_command is None:
        return False
    nested = _nested_shell_command(outer_argv)
    if outer_command in {"bash", "sh", "zsh", "fish", "cmd", "powershell", "pwsh"}:
        return nested is not None and _exec_command_is_read_only(nested, depth=depth + 1)
    # Environment/privilege wrappers can change executable lookup and behavior.
    if outer_command in {"env", "sudo", "command", "nohup", "time"}:
        return False
    command = _trusted_command_name(argv[0])
    if command is None:
        return False
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
    if os.name == "nt" and command in _WINDOWS_READ_COMMANDS:
        return True
    if command in _READ_ONLY_SIMPLE_COMMANDS:
        return _simple_call_is_read_only(command, argv[1:])
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


def _exec_command_is_read_only(command: str, *, depth: int = 0) -> bool:
    try:
        segments = parse_shell_segments(
            command,
            platform="windows" if os.name == "nt" else None,
        )
    except ValueError:
        return False
    return all(
        _shell_segment_is_read_only(segment.source, segment.argv, depth=depth)
        for segment in segments
    )


def resolve_exec_command_effect(arguments: Mapping[str, Any]) -> CompletionEffect:
    """Classify an executed shell call; uncertainty remains action-capable."""

    command = arguments.get("command")
    env = arguments.get("env")
    if (
        not isinstance(command, str)
        or not command.strip()
        or (isinstance(env, Mapping) and bool(env))
    ):
        return "action"
    return "read_only" if _exec_command_is_read_only(command) else "action"


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
    if resolver == "http_request":
        method = str(arguments.get("method") or "GET").strip().upper()
        output_path = arguments.get("output_path")
        return (
            "read_only"
            if method in {"GET", "HEAD", "OPTIONS"} and not output_path
            else "action"
        )
    if resolver == "cron":
        action = str(arguments.get("action") or "").strip().casefold()
        return "read_only" if action == "list" else "action"
    if resolver == "subagents":
        action = str(arguments.get("action") or "").strip().casefold()
        return "read_only" if action == "list" else "action"
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
        return False
    status = normalize_execution_status(raw_status)
    if status["status"] == "success":
        return status["source"] in {"tool_runtime", "adapter"}
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
