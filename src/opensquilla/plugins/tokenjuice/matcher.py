from __future__ import annotations

import os
import re
import shlex
from typing import Any

from .types import Rule

# Strict matching is the safe default: every declared rule criterion must
# match.  An explicit falsy value retains the old permissive matcher as an
# emergency rollback.  cd unwrapping remains opt-in and is only meaningful
# together with strict matching.
_MATCHER_STRICT_ENV = "OPENSQUILLA_TOOLCOMP_MATCHER_STRICT"
_CD_UNWRAP_ENV = "OPENSQUILLA_TOOLCOMP_CD_UNWRAP"
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})
_FALSE_ENV_VALUES = frozenset({"", "0", "false", "off", "no", "disabled"})

# OpenSquilla's command-bearing shell tool.  A field named ``command`` on an
# arbitrary tool is not proof that its result is shell output.
_SHELL_TOOL_NAMES = frozenset({"exec_command"})

# Specialized reducers assume that the result belongs to one command.  Shell
# composition can mix unrelated output into that result, so composite syntax
# must use the generic fallback instead.  Quoted and escaped characters are
# literals and do not make a command composite.
_COMPOSITE_SHELL_CHARS = frozenset({"|", "&", ";", "<", ">", "(", ")", "\n", "\r"})
_SHELL_REPARSE_BUILTINS = frozenset({"call", "eval", "iex", "invoke-expression"})
_SHELL_LAUNCHER_BUILTINS = frozenset({"builtin", "command", "exec"})
_POSIX_SHELL_EXECUTABLES = frozenset(
    {"ash", "bash", "csh", "dash", "fish", "ksh", "mksh", "nu", "sh", "tcsh", "yash", "zsh"}
)
_POWERSHELL_EXECUTABLES = frozenset({"powershell", "pwsh"})
_ENV_OPTIONS_WITH_VALUE = frozenset(
    {
        "-P",
        "-a",
        "-C",
        "-u",
        "--argv0",
        "--block-signal",
        "--chdir",
        "--default-signal",
        "--ignore-signal",
        "--unset",
    }
)
_ENV_OPTIONS_WITHOUT_VALUE = frozenset(
    {"-0", "-i", "-v", "--debug", "--ignore-environment", "--null", "--verbose"}
)
_ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Git global options that consume the next argv entry; subcommand extraction
# must skip them (and their inline `--opt=value` forms) to find the verb.
_GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
        "--config-env",
    }
)
_GIT_GLOBAL_OPTION_INLINE_PREFIXES = (
    "--git-dir=",
    "--work-tree=",
    "--namespace=",
    "--exec-path=",
    "--super-prefix=",
    "--config-env=",
)
_GIT_GLOBAL_OPTIONS_WITHOUT_SUBCOMMAND = frozenset(
    {
        "-h",
        "-v",
        "--",
        "--help",
        "--html-path",
        "--info-path",
        "--man-path",
        "--exec-path",
        "--version",
    }
)

# Only horizontal whitespace may separate the keyword from its argument: a
# real shell terminates a bare `cd` statement at an unquoted newline.
_LEADING_CD_PATTERN = re.compile(r"^\s*(?:cd|pushd)[ \t]+")
_CD_ARG_STOP_CHARS = frozenset({"&", "|", ";", "<", ">", "\n"})
_WINDOWS_DRIVE_PATH_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")


def _matcher_strict_enabled() -> bool:
    raw_value = os.environ.get(_MATCHER_STRICT_ENV)
    if raw_value is None:
        return True
    raw = raw_value.strip().lower()
    # Unknown values fail safe.  A typo must not silently restore the
    # permissive matcher; only an explicit falsy value is a rollback request.
    return raw not in _FALSE_ENV_VALUES


def _cd_unwrap_enabled() -> bool:
    raw = os.environ.get(_CD_UNWRAP_ENV, "").strip().lower()
    return raw in _TRUE_ENV_VALUES


def command_argv(command: str | None, argv: list[str] | None = None) -> list[str]:
    if argv:
        return argv
    if not command:
        return []
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _is_simple_shell_command(command: str) -> bool:
    """Return whether *command* is one parseable, non-composite shell command."""

    quote: str | None = None
    escaping = False
    index = 0
    while index < len(command):
        char = command[index]

        # In POSIX shell syntax, every character inside single quotes is
        # literal, including backslashes.
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue

        if escaping:
            escaping = False
            index += 1
            continue
        if char == "\\":
            escaping = True
            index += 1
            continue

        if quote == '"':
            if char == '"':
                quote = None
            elif char == "`" or command[index : index + 2] == "$(":
                return False
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char in _COMPOSITE_SHELL_CHARS:
            return False
        if char == "`" or command[index : index + 2] == "$(":
            return False
        index += 1

    if quote is not None or escaping:
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return bool(argv) and not _shell_dispatch_reparses(argv)


def _generic_fallback_rule(rules: tuple[Rule, ...]) -> Rule | None:
    return next((rule for rule in rules if rule.id == "generic/fallback"), None)


def _list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return []
    return [item for item in value]


def _list_of_string_lists(value: Any) -> list[list[str]]:
    if not isinstance(value, list):
        return []
    return [
        [item for item in entry if isinstance(item, str)]
        for entry in value
        if isinstance(entry, list)
    ]


def _contains_all(argv: list[str], needles: list[str]) -> bool:
    return all(needle in argv for needle in needles)


def _starts_with(argv: list[str], prefix: list[str]) -> bool:
    return bool(prefix) and argv[: len(prefix)] == prefix


def _contains_command_text(command: str, needles: list[str]) -> bool:
    lowered = command.lower()
    return all(needle.lower() in lowered for needle in needles)


def _command_name(argv: list[str]) -> str | None:
    first = argv[0] if argv else None
    if not first:
        return None
    if first[:1] in {"'", '"'}:
        first = first[1:]
    if first[-1:] in {"'", '"'}:
        first = first[:-1]
    return os.path.basename(first)


def _variable_executable_token(executable: str) -> bool:
    return executable.startswith("$") or (
        len(executable) > 2 and executable[0] in {"%", "!"} and executable[-1] == executable[0]
    )


def _normalized_executable_token(raw_token: str) -> str:
    name = raw_token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _matches_executable_name(executable: str, names: frozenset[str]) -> bool:
    if executable in names:
        return True
    # POSIX shlex removes backslashes from an unquoted Windows drive path.
    return ":" in executable and any(executable.endswith(name) for name in names)


def _runner_command_index(tokens: list[str], index: int) -> int | None:
    if index < len(tokens) and tokens[index] == "--":
        index += 1
    if index >= len(tokens) or tokens[index].startswith("-"):
        return None
    return index


def _first_executable_index(tokens: list[str]) -> int | None:
    index = 0
    while index < len(tokens) and _ENV_ASSIGNMENT_PATTERN.match(tokens[index]):
        index += 1
    return index if index < len(tokens) else None


def _effective_command_tokens(tokens: list[str]) -> list[str]:
    executable_index = _first_executable_index(tokens)
    return tokens[executable_index:] if executable_index is not None else []


def _invoked_command_and_args(tokens: list[str]) -> tuple[str, list[str]] | None:
    effective_tokens = _effective_command_tokens(tokens)
    if not effective_tokens:
        return None
    executable = _normalized_executable_token(effective_tokens[0])
    if re.fullmatch(r"(?:py|python(?:\d+(?:\.\d+)*)?)", executable):
        if len(effective_tokens) > 2 and effective_tokens[1] == "-m":
            return _normalized_executable_token(effective_tokens[2]), effective_tokens[3:]
        return executable, effective_tokens[1:]
    if executable in {"uv", "poetry", "pipenv"}:
        if len(effective_tokens) > 1 and effective_tokens[1] == "run":
            command_index = _runner_command_index(effective_tokens, 2)
            if command_index is None:
                return None
            return (
                _normalized_executable_token(effective_tokens[command_index]),
                effective_tokens[command_index + 1 :],
            )
        return executable, effective_tokens[1:]
    if executable in {"npm", "pnpm", "yarn"}:
        if len(effective_tokens) > 1 and effective_tokens[1] in {"dlx", "exec"}:
            command_index = _runner_command_index(effective_tokens, 2)
            if command_index is None:
                return None
            return (
                _normalized_executable_token(effective_tokens[command_index]),
                effective_tokens[command_index + 1 :],
            )
        return executable, effective_tokens[1:]
    if executable in {"bunx", "npx", "uvx"}:
        command_index = _runner_command_index(effective_tokens, 1)
        if command_index is None:
            return None
        return (
            _normalized_executable_token(effective_tokens[command_index]),
            effective_tokens[command_index + 1 :],
        )
    return executable, effective_tokens[1:]


def _invoked_command_basename(tokens: list[str]) -> str | None:
    invoked = _invoked_command_and_args(tokens)
    return invoked[0] if invoked is not None else None


def _command_basename_matches(tokens: list[str], candidates: list[str]) -> bool:
    actual = _invoked_command_basename(tokens)
    if actual is None:
        return False
    names = frozenset(_normalized_executable_token(candidate) for candidate in candidates)
    # Do not suffix-match a POSIX-shlexed Windows path here: backslashes are
    # removed, so ``C:\\tools\\notpytest.exe`` becomes
    # ``c:toolsnotpytest`` and cannot be distinguished from a real pytest
    # path.  Ambiguous paths must use the generic fallback.
    return actual in names


def _command_args_start_with_any(tokens: list[str], prefixes: list[list[str]]) -> bool:
    invoked = _invoked_command_and_args(tokens)
    return invoked is not None and any(_starts_with(invoked[1], prefix) for prefix in prefixes)


def _env_launches_command(argv: list[str]) -> bool:
    index = 1
    while index < len(argv):
        raw_arg = argv[index]
        if raw_arg == "--":
            return index + 1 < len(argv)
        if raw_arg in {"-S", "--split-string"}:
            return True
        if raw_arg.startswith(("-S=", "--split-string=")) or (
            raw_arg.startswith("-S") and len(raw_arg) > 2
        ):
            return True
        if _ENV_ASSIGNMENT_PATTERN.match(raw_arg):
            index += 1
            continue
        if raw_arg in _ENV_OPTIONS_WITH_VALUE:
            if index + 1 >= len(argv):
                return True
            index += 2
            continue
        if raw_arg in _ENV_OPTIONS_WITHOUT_VALUE or raw_arg.startswith(
            (
                "--argv0=",
                "--block-signal=",
                "--chdir=",
                "--default-signal=",
                "--ignore-signal=",
                "--unset=",
            )
        ):
            index += 1
            continue
        # Unknown options fail closed; a non-option is env's launched command.
        return True
    return False


def _shell_dispatch_reparses(argv: list[str]) -> bool:
    """Return whether argv0 is a shell or a launcher hiding the real command.

    Operators inside a quoted shell payload are opaque to the outer scan.
    Specialized reducers therefore cannot prove that all output belongs to one
    command and must use the generic fallback.  Only the actual executable
    position is inspected, so ordinary arguments named ``bash`` or ``eval`` do
    not degrade a correctly identified command.
    """

    effective_argv = _effective_command_tokens(argv)
    if not effective_argv:
        return False
    executable = _normalized_executable_token(effective_argv[0])
    if (
        _variable_executable_token(executable)
        or executable in _SHELL_REPARSE_BUILTINS
        or executable in _SHELL_LAUNCHER_BUILTINS
        or _matches_executable_name(executable, _POSIX_SHELL_EXECUTABLES)
        or _matches_executable_name(executable, frozenset({"cmd"}))
        or _matches_executable_name(executable, _POWERSHELL_EXECUTABLES)
    ):
        return True
    if _matches_executable_name(executable, frozenset({"env"})):
        return _env_launches_command(effective_argv)
    return False


def _git_subcommand_position(argv: list[str]) -> tuple[str, int] | None:
    if _command_name(argv) != "git":
        return None
    index = 1
    while index < len(argv):
        arg = argv[index]
        if not arg:
            index += 1
            continue
        if arg in _GIT_GLOBAL_OPTIONS_WITHOUT_SUBCOMMAND or arg.startswith("--list-cmds="):
            return None
        if arg in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if arg.startswith(_GIT_GLOBAL_OPTION_INLINE_PREFIXES):
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg, index
    return None


def _git_subcommand(argv: list[str]) -> str | None:
    located = _git_subcommand_position(argv)
    return located[0] if located is not None else None


def _git_subcommand_args(argv: list[str]) -> list[str]:
    located = _git_subcommand_position(argv)
    return argv[located[1] + 1 :] if located is not None else []


def _before_double_dash(argv: list[str]) -> list[str]:
    try:
        return argv[: argv.index("--")]
    except ValueError:
        return argv


def strip_leading_cd_prefix(command: str) -> str:
    current = command.strip()
    for _ in range(8):
        unwrapped = _match_leading_cd_chain(current)
        if unwrapped is None:
            return current
        current = unwrapped
    return current


def _looks_like_windows_cd_arg(raw_arg: str) -> bool:
    value = raw_arg.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return bool(_WINDOWS_DRIVE_PATH_PATTERN.match(value) or value.startswith("\\\\"))


def _match_leading_cd_chain(command: str) -> str | None:
    keyword = _LEADING_CD_PATTERN.match(command)
    if keyword is None:
        return None

    # The cd argument must be a single shell token: quoting and escapes are
    # honoured, but an unquoted operator or redirection makes the prefix
    # unsafe to strip, so the command is left untouched.
    index = keyword.end()
    arg_start = index
    quote: str | None = None
    escaping = False
    saw_arg = False
    while index < len(command):
        char = command[index]
        if escaping:
            escaping = False
        elif char == "\\":
            escaping = True
        elif quote == "'":
            if char == "'":
                quote = None
        elif char == "`" or command[index : index + 2] == "$(":
            # Command substitution in the directory token may itself emit
            # output, so stripping the prefix would falsely attribute that
            # output to the command after ``&&``.
            return None
        elif quote == '"':
            if char == '"':
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char in _CD_ARG_STOP_CHARS:
            return None
        elif char.isspace():
            break
        saw_arg = True
        index += 1

    if not saw_arg:
        return None
    if _looks_like_windows_cd_arg(command[arg_start:index]):
        return None

    while index < len(command) and command[index] in " \t":
        index += 1

    if command[index : index + 2] != "&&":
        return None
    tail = command[index + 2 :].strip()
    return tail or None


def rule_matches(
    rule: Rule,
    *,
    tool_name: str,
    command: str | None,
    argv: list[str] | None,
    content: str,
    exit_code: int,
) -> bool:
    match = rule.match
    if not match:
        return True

    normalized_tool = "exec" if command and tool_name in _SHELL_TOOL_NAMES else tool_name
    tool_names = _list_of_strings(match.get("toolNames"))
    if tool_names:
        if tool_name in _SHELL_TOOL_NAMES:
            tool_matches = normalized_tool in tool_names or tool_name in tool_names
        else:
            # ``exec`` is the rule namespace for the canonical shell tool, not
            # an accepted compatibility alias for a runtime tool named exec.
            tool_matches = tool_name in (set(tool_names) - {"exec"})
        if not tool_matches:
            return False

    # A non-shell tool may legitimately define its own command/argv matcher,
    # but only when the rule explicitly names that tool.  Unscoped git rules
    # must not consume an arbitrary tool's command-shaped argument.
    command_match_allowed = tool_name in _SHELL_TOOL_NAMES or bool(tool_names)
    tokens = command_argv(command, argv) if command_match_allowed else []
    strict_enabled = _matcher_strict_enabled()
    position_aware_criterion = any(
        match.get(name)
        for name in (
            "commandBasenames",
            "commandArgsStartsWithAny",
            "gitSubcommands",
            "argvStartsWithAny",
            "gitSubcommandArgsStartsWithAny",
            "argvIncludesBeforeDoubleDash",
        )
    )
    # Only rules that opt into a strict position-aware criterion may look
    # through leading NAME=value assignments.  Applying this to every legacy
    # argv0 rule would expand their known broad token matching surface.
    positional_tokens = (
        _effective_command_tokens(tokens) if strict_enabled and position_aware_criterion else tokens
    )
    argv0 = _list_of_strings(match.get("argv0"))
    if argv0 and (not positional_tokens or positional_tokens[0] not in argv0):
        return False

    command_basenames = _list_of_strings(match.get("commandBasenames"))
    if (
        command_basenames
        and strict_enabled
        and not _command_basename_matches(tokens, command_basenames)
    ):
        return False

    command_args_starts_with_any = _list_of_string_lists(match.get("commandArgsStartsWithAny"))
    if (
        command_args_starts_with_any
        and strict_enabled
        and not _command_args_start_with_any(tokens, command_args_starts_with_any)
    ):
        return False

    git_subcommands = _list_of_strings(match.get("gitSubcommands"))
    if (
        git_subcommands
        and strict_enabled
        and (_git_subcommand(positional_tokens) or "") not in git_subcommands
    ):
        return False

    git_args_starts_with_any = _list_of_string_lists(match.get("gitSubcommandArgsStartsWithAny"))
    if (
        git_args_starts_with_any
        and strict_enabled
        and not any(
            _starts_with(_git_subcommand_args(positional_tokens), entry)
            for entry in git_args_starts_with_any
        )
    ):
        return False

    argv_starts_with_any = _list_of_string_lists(match.get("argvStartsWithAny"))
    if (
        argv_starts_with_any
        and strict_enabled
        and not any(_starts_with(positional_tokens, entry) for entry in argv_starts_with_any)
    ):
        return False

    argv_includes = _list_of_string_lists(match.get("argvIncludes"))
    if argv_includes and not any(
        _contains_all(positional_tokens, entry) for entry in argv_includes
    ):
        return False

    argv_includes_before_double_dash = _list_of_string_lists(
        match.get("argvIncludesBeforeDoubleDash")
    )
    if (
        argv_includes_before_double_dash
        and strict_enabled
        and not any(
            _contains_all(_before_double_dash(positional_tokens), entry)
            for entry in argv_includes_before_double_dash
        )
    ):
        return False

    argv_includes_any = _list_of_string_lists(match.get("argvIncludesAny"))
    if (
        argv_includes_any
        and strict_enabled
        and not any(_contains_all(positional_tokens, entry) for entry in argv_includes_any)
    ):
        return False

    command_text = (command or " ".join(tokens)) if command_match_allowed else ""
    command_includes = _list_of_strings(match.get("commandIncludes"))
    if command_includes and not _contains_command_text(command_text, command_includes):
        return False

    command_includes_any = _list_of_strings(match.get("commandIncludesAny"))
    if command_includes_any and not any(
        needle.lower() in command_text.lower() for needle in command_includes_any
    ):
        return False

    command_regex = match.get("commandRegex")
    if isinstance(command_regex, str) and not re.search(command_regex, command_text):
        return False

    exit_codes = match.get("exitCodes")
    if isinstance(exit_codes, list) and exit_codes and exit_code not in exit_codes:
        return False

    output_regex = match.get("outputRegex")
    if isinstance(output_regex, str) and not re.search(output_regex, content, re.MULTILINE):
        return False

    strict_output_regex = match.get("strictOutputRegex")
    if (
        strict_enabled
        and isinstance(strict_output_regex, str)
        and not re.search(strict_output_regex, content, re.MULTILINE)
    ):
        return False

    return True


def select_rule(
    rules: tuple[Rule, ...],
    *,
    tool_name: str,
    command: str | None,
    argv: list[str] | None,
    content: str,
    exit_code: int,
) -> Rule | None:
    if tool_name in _SHELL_TOOL_NAMES:
        # The canonical shell tool always carries a command.  Missing command
        # data cannot safely select a command-specific reducer.
        if not command:
            return _generic_fallback_rule(rules)

        # cd unwrapping remains opt-in.  Refuse the unsafe historical
        # combination where unwrapping fed a permissive matcher and selected
        # an unrelated rule.
        if _cd_unwrap_enabled() and _matcher_strict_enabled():
            unwrapped = strip_leading_cd_prefix(command)
            if unwrapped != command.strip():
                command = unwrapped
                argv = command_argv(unwrapped, None)

        # A specialized reducer is safe only when all output belongs to one
        # parseable command.  This also guards malformed tails produced by the
        # optional cd unwrapping path.
        if not _is_simple_shell_command(command) or (
            argv and _shell_dispatch_reparses(command_argv(command, argv))
        ):
            return _generic_fallback_rule(rules)

    ordered_rules = rules
    if _matcher_strict_enabled():
        # High-priority cross-cutting rules (for example explicit help) keep
        # precedence.  Exact invoked-command rules then beat broad task-family
        # fallbacks such as `task/python` for `python -m pytest`.
        strict_specificity_keys = (
            "commandBasenames",
            "commandArgsStartsWithAny",
            "argvIncludesBeforeDoubleDash",
            "gitSubcommandArgsStartsWithAny",
        )
        ordered_rules = (
            tuple(rule for rule in rules if rule.priority > 0)
            + tuple(
                rule
                for rule in rules
                if rule.priority <= 0
                and any(rule.match.get(key) for key in strict_specificity_keys)
            )
            + tuple(
                rule
                for rule in rules
                if rule.priority <= 0
                and not any(rule.match.get(key) for key in strict_specificity_keys)
            )
        )

    for rule in ordered_rules:
        if rule_matches(
            rule,
            tool_name=tool_name,
            command=command,
            argv=argv,
            content=content,
            exit_code=exit_code,
        ):
            return rule
    return None
