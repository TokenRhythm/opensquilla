from __future__ import annotations

import pytest

from opensquilla.sandbox.command_policy import (
    CommandAction,
    decide_command,
    decide_shell_command,
    parse_shell_segments,
    validate_command_prefix,
)
from opensquilla.sandbox.policy_models import SandboxPolicy


def test_auto_allow_beats_approval_and_builtin_high_risk() -> None:
    policy = SandboxPolicy()
    policy.commands.auto_allow_prefixes = [["git", "push"]]
    policy.commands.require_approval_prefixes = [["git"]]

    decision = decide_command(["git", "push", "origin", "main"], policy)

    assert decision.action is CommandAction.AUTO
    assert decision.code == "user_auto_allow"


def test_git_push_requires_approval_by_default() -> None:
    decision = decide_command(["git", "push"], SandboxPolicy())

    assert decision.action is CommandAction.APPROVAL
    assert decision.code == "builtin_git_push"


def test_compound_command_uses_strictest_segment() -> None:
    decision = decide_shell_command(
        "python build.py && git push origin main",
        SandboxPolicy(),
        platform="linux",
    )

    assert decision.action is CommandAction.APPROVAL
    assert decision.code == "builtin_git_push"


def test_quoted_control_character_does_not_split_segment() -> None:
    segments = parse_shell_segments(
        'python -c "print(\'a;b\')" && node build.js',
        platform="linux",
    )

    assert len(segments) == 2
    assert segments[0].argv[0] == "python"
    assert segments[1].argv == ("node", "build.js")


def test_heredoc_body_is_not_split_into_shell_segments() -> None:
    segments = parse_shell_segments(
        "cat > test_bug.php << 'EOF'\n<?php echo 'debug';\nEOF\n",
        platform="linux",
    )

    assert len(segments) == 1
    assert segments[0].argv[0] == "cat"


def test_command_after_heredoc_terminator_is_still_split() -> None:
    segments = parse_shell_segments(
        "cat <<'EOF'\nrm body-only.txt\nEOF\nrm real-target.txt",
        platform="linux",
    )

    assert len(segments) == 2
    assert segments[1].argv == ("rm", "real-target.txt")


def test_heredoc_marker_inside_posix_comment_does_not_hide_later_command() -> None:
    segments = parse_shell_segments(
        "echo ok # <<EOF\nrm real-target.txt\nEOF",
        platform="linux",
    )

    assert len(segments) == 3
    assert segments[1].argv == ("rm", "real-target.txt")


@pytest.mark.parametrize(
    "command",
    (
        ": $((1 << EOF))\nrm real-target.txt\nEOF",
        ": $[1 << EOF ]\nrm real-target.txt\nEOF",
        "((1 << EOF))\nrm real-target.txt\nEOF",
    ),
)
def test_arithmetic_shift_does_not_hide_later_command(command: str) -> None:
    segments = parse_shell_segments(command, platform="linux")

    assert len(segments) == 3
    assert segments[1].argv == ("rm", "real-target.txt")


def test_windows_segment_parser_does_not_apply_posix_heredoc_rules() -> None:
    segments = parse_shell_segments(
        "echo <<EOF\ndel real-target.txt",
        platform="windows",
    )

    assert len(segments) == 2
    assert segments[1].argv == ("del", "real-target.txt")


def test_shell_wrapper_is_unwrapped_for_matching() -> None:
    decision = decide_shell_command(
        'bash -lc "git push origin main"',
        SandboxPolicy(),
        platform="linux",
    )

    assert decision.action is CommandAction.APPROVAL


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("auto", "auto"), ("prompt", "approval"), ("disabled", "deny")],
)
def test_system_tool_tri_state(mode: str, expected: str) -> None:
    policy = SandboxPolicy()
    policy.commands.system_tools = mode  # type: ignore[assignment]

    assert decide_command(["wsl", "--status"], policy, platform="windows").action == expected


def test_explicit_auto_prefix_overrides_system_tool_default() -> None:
    policy = SandboxPolicy()
    policy.commands.system_tools = "disabled"
    policy.commands.auto_allow_prefixes = [["wsl"]]

    assert (
        decide_command(["wsl", "--status"], policy, platform="windows").action
        is CommandAction.AUTO
    )


def test_windows_executable_matching_is_case_insensitive_and_strips_exe() -> None:
    policy = SandboxPolicy()
    policy.commands.require_approval_prefixes = [["Git", "status"]]

    decision = decide_command(
        [r"C:\Program Files\Git\bin\GIT.EXE", "STATUS"],
        policy,
        platform="windows",
    )

    assert decision.action is CommandAction.APPROVAL


def test_rule_rejects_shell_control_tokens() -> None:
    with pytest.raises(ValueError):
        validate_command_prefix(["git", "status;"])
