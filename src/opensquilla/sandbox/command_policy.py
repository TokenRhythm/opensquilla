"""Token-prefix command policy for Safe mode."""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath
from typing import Literal

from opensquilla.sandbox.policy_models import SandboxPolicy


class CommandAction(StrEnum):
    AUTO = "auto"
    APPROVAL = "approval"
    DENY = "deny"


@dataclass(frozen=True)
class CommandSegment:
    source: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class CommandDecision:
    action: CommandAction
    code: str
    argv: tuple[str, ...] = ()
    matched_prefix: tuple[str, ...] = ()


_WINDOWS_SYSTEM_TOOLS = frozenset({"wsl", "wmic", "sc", "reg", "schtasks"})
_DARWIN_SYSTEM_TOOLS = frozenset({"launchctl", "crontab", "sudo"})
_LINUX_SYSTEM_TOOLS = frozenset({"systemctl", "crontab", "sudo"})
_BOUNDARY_RE = re.compile(r"(?:&&|\|\||[;&|\n])")
_UNSAFE_RULE_TOKEN_RE = re.compile(r"(?:&&|\|\||[;|<>`$()]|\r|\n)")
_HEREDOC_RE = re.compile(
    r"(?<!<)<<(?P<strip_tabs>-?)(?!<)\s*"
    r"(?P<quote>['\"]?)(?P<delimiter>[A-Za-z_]\w*)(?P=quote)"
    r"(?=$|[\s;&|()<>])"
)


def _platform_name(platform: str | None) -> Literal["windows", "darwin", "linux"]:
    raw = (platform or os.name).lower()
    if raw in {"nt", "win32", "windows"}:
        return "windows"
    if raw in {"darwin", "mac", "macos"}:
        return "darwin"
    return "linux"


def _normalize_executable(value: str, *, platform: str) -> str:
    name = PurePath(value.replace("\\", "/")).name
    if platform == "windows":
        name = name.casefold()
        if name.endswith((".exe", ".cmd", ".bat", ".com")):
            name = name.rsplit(".", 1)[0]
    return name


def normalize_argv(
    argv: Sequence[str],
    *,
    platform: str | None = None,
) -> tuple[str, ...]:
    target = _platform_name(platform)
    tokens = tuple(str(token) for token in argv if str(token))
    if not tokens:
        return ()
    normalized = (_normalize_executable(tokens[0], platform=target), *tokens[1:])
    if target == "windows":
        return tuple(token.casefold() for token in normalized)
    return tuple(normalized)


def validate_command_prefix(prefix: Sequence[str]) -> tuple[str, ...]:
    cleaned = tuple(str(token).strip() for token in prefix)
    if not cleaned or any(not token for token in cleaned):
        raise ValueError("command prefix requires non-empty tokens")
    if any(_UNSAFE_RULE_TOKEN_RE.search(token) for token in cleaned):
        raise ValueError("command prefix may not contain shell control operators")
    return cleaned


def _strip_wrappers(argv: tuple[str, ...], *, platform: str) -> tuple[str, ...]:
    current = argv
    while current:
        executable = _normalize_executable(current[0], platform=platform)
        if executable in {"env", "sudo"}:
            index = 1
            while index < len(current) and (
                current[index].startswith("-") or "=" in current[index]
            ):
                index += 1
            current = current[index:]
            continue
        if executable == "cmd" and len(current) >= 3 and current[1].casefold() in {
            "/c",
            "/k",
        }:
            return _tokenize(current[2], platform=platform)
        if executable in {"powershell", "pwsh"}:
            for index, token in enumerate(current[1:], start=1):
                if token.casefold() in {"-c", "-command"} and index + 1 < len(current):
                    return _tokenize(current[index + 1], platform=platform)
        if executable in {"bash", "sh", "zsh", "fish"}:
            for index, token in enumerate(current[1:], start=1):
                if token in {"-c", "-lc"} and index + 1 < len(current):
                    return _tokenize(current[index + 1], platform=platform)
        break
    return current


def _tokenize(source: str, *, platform: str) -> tuple[str, ...]:
    tokens = tuple(shlex.split(source, posix=platform != "windows"))
    return _strip_wrappers(tokens, platform=platform)


def _heredoc_delimiters(
    line: str,
    quote: str | None,
    arithmetic_closers: tuple[str, ...],
) -> tuple[list[tuple[str, bool]], str | None, tuple[str, ...]]:
    delimiters: list[tuple[str, bool]] = []
    index = 0
    word_start = quote is None and not arithmetic_closers
    while index < len(line):
        char = line[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\":
            word_start = False
            index += 2
            continue
        if quote == '"':
            if char == '"':
                quote = None
            index += 1
            continue
        if arithmetic_closers:
            if char in "([":
                arithmetic_closers = (
                    *arithmetic_closers,
                    ")" if char == "(" else "]",
                )
            elif char == arithmetic_closers[-1]:
                arithmetic_closers = arithmetic_closers[:-1]
            index += 1
            continue
        if line.startswith("$((", index):
            arithmetic_closers = (")", ")")
            word_start = False
            index += 3
            continue
        if line.startswith("$[", index):
            arithmetic_closers = ("]",)
            word_start = False
            index += 2
            continue
        if word_start and line.startswith("((", index):
            arithmetic_closers = (")", ")")
            word_start = False
            index += 2
            continue
        if char == "#" and word_start:
            break
        if char in {"'", '"'}:
            quote = char
            word_start = False
            index += 1
            continue
        if char == "<":
            match = _HEREDOC_RE.match(line, index)
            if match is not None:
                delimiters.append(
                    (match.group("delimiter"), bool(match.group("strip_tabs")))
                )
                word_start = False
                index = match.end()
                continue
        if char.isspace() or char in ";&|()<>":
            word_start = True
            index += 1
            continue
        word_start = False
        index += 1
    return delimiters, quote, arithmetic_closers


def strip_shell_heredoc_bodies(command: str) -> str:
    """Remove POSIX heredoc data while retaining executable command lines."""

    if "<<" not in command:
        return command
    output_lines: list[str] = []
    pending: list[tuple[str, bool]] = []
    quote: str | None = None
    arithmetic_closers: tuple[str, ...] = ()
    for line in command.split("\n"):
        if pending:
            delimiter, strip_tabs = pending[0]
            candidate = line.lstrip("\t") if strip_tabs else line
            if candidate == delimiter:
                pending.pop(0)
            continue
        delimiters, quote, arithmetic_closers = _heredoc_delimiters(
            line,
            quote,
            arithmetic_closers,
        )
        pending.extend(delimiters)
        output_lines.append(line)
    return "\n".join(output_lines)


def _split_segments(command: str, *, strip_heredocs: bool) -> tuple[str, ...]:
    if strip_heredocs:
        command = strip_shell_heredoc_bodies(command)
    segments: list[str] = []
    start = 0
    quote = ""
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote:
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        match = _BOUNDARY_RE.match(command, index)
        if match:
            segment = command[start:index].strip()
            if segment:
                segments.append(segment)
            index = match.end()
            start = index
            continue
        index += 1
    if quote:
        raise ValueError("unbalanced shell quote")
    tail = command[start:].strip()
    if tail:
        segments.append(tail)
    return tuple(segments)


def parse_shell_segments(
    command: str,
    *,
    platform: str | None = None,
) -> tuple[CommandSegment, ...]:
    target = _platform_name(platform)
    parsed: list[CommandSegment] = []
    for source in _split_segments(command, strip_heredocs=target != "windows"):
        argv = _tokenize(source, platform=target)
        if not argv:
            raise ValueError("empty shell command segment")
        parsed.append(CommandSegment(source=source, argv=argv))
    if not parsed:
        raise ValueError("empty shell command")
    return tuple(parsed)


def _matches_prefix(
    argv: tuple[str, ...],
    prefix: Sequence[str],
    *,
    platform: str,
) -> bool:
    normalized_prefix = normalize_argv(
        validate_command_prefix(prefix),
        platform=platform,
    )
    normalized_argv = normalize_argv(argv, platform=platform)
    return normalized_argv[: len(normalized_prefix)] == normalized_prefix


def _best_prefix(
    argv: tuple[str, ...],
    prefixes: Sequence[Sequence[str]],
    *,
    platform: str,
) -> tuple[str, ...]:
    matches = [
        tuple(prefix)
        for prefix in prefixes
        if _matches_prefix(argv, prefix, platform=platform)
    ]
    return max(matches, key=len, default=())


def _is_system_tool(argv: tuple[str, ...], *, platform: str) -> bool:
    if not argv:
        return False
    executable = _normalize_executable(argv[0], platform=platform)
    catalog = {
        "windows": _WINDOWS_SYSTEM_TOOLS,
        "darwin": _DARWIN_SYSTEM_TOOLS,
        "linux": _LINUX_SYSTEM_TOOLS,
    }[platform]
    return executable in catalog


def decide_command(
    argv: Sequence[str],
    policy: SandboxPolicy,
    *,
    platform: str | None = None,
) -> CommandDecision:
    target = _platform_name(platform)
    tokens = _strip_wrappers(tuple(str(token) for token in argv), platform=target)
    if not tokens:
        return CommandDecision(CommandAction.APPROVAL, "command_parse_failed")
    auto = _best_prefix(
        tokens,
        policy.commands.auto_allow_prefixes,
        platform=target,
    )
    if auto:
        return CommandDecision(CommandAction.AUTO, "user_auto_allow", tokens, auto)
    approval = _best_prefix(
        tokens,
        policy.commands.require_approval_prefixes,
        platform=target,
    )
    if approval:
        return CommandDecision(
            CommandAction.APPROVAL,
            "user_approval_prefix",
            tokens,
            approval,
        )
    if _is_system_tool(tokens, platform=target):
        system_tools = policy.commands.system_tools
        if system_tools == "disabled":
            return CommandDecision(CommandAction.DENY, "system_tool_disabled", tokens)
        if system_tools == "prompt":
            return CommandDecision(CommandAction.APPROVAL, "system_tool_prompt", tokens)
    normalized = normalize_argv(tokens, platform=target)
    if normalized[:2] == ("git", "push"):
        return CommandDecision(CommandAction.APPROVAL, "builtin_git_push", tokens)
    return CommandDecision(CommandAction.AUTO, "default_auto", tokens)


def decide_shell_command(
    command: str,
    policy: SandboxPolicy,
    *,
    platform: str | None = None,
) -> CommandDecision:
    target = _platform_name(platform)
    try:
        segments = parse_shell_segments(command, platform=target)
    except ValueError:
        return CommandDecision(CommandAction.APPROVAL, "command_parse_failed")
    decisions = [
        decide_command(segment.argv, policy, platform=target) for segment in segments
    ]
    for action in (CommandAction.DENY, CommandAction.APPROVAL):
        for decision in decisions:
            if decision.action is action:
                return decision
    return CommandDecision(CommandAction.AUTO, "default_auto")


__all__ = [
    "CommandAction",
    "CommandDecision",
    "CommandSegment",
    "decide_command",
    "decide_shell_command",
    "normalize_argv",
    "parse_shell_segments",
    "validate_command_prefix",
]
