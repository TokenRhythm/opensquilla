from __future__ import annotations

import pytest

from opensquilla.plugins.tokenjuice.matcher import (
    command_argv,
    rule_matches,
    select_rule,
    strip_leading_cd_prefix,
)
from opensquilla.plugins.tokenjuice.plugin import reduce_tool_result
from opensquilla.plugins.tokenjuice.reducer import _summarize_window, reduce_with_rule
from opensquilla.plugins.tokenjuice.rules import load_rules
from opensquilla.plugins.tokenjuice.types import Rule

STRICT_ENV = "OPENSQUILLA_TOOLCOMP_MATCHER_STRICT"
CD_UNWRAP_ENV = "OPENSQUILLA_TOOLCOMP_CD_UNWRAP"
FAILURE_PRESERVE_ENV = "OPENSQUILLA_TOOLCOMP_FAILURE_PRESERVE"
ALL_LEVER_ENVS = (STRICT_ENV, CD_UNWRAP_ENV, FAILURE_PRESERVE_ENV)

TRUTHY_VALUES = ["1", "true", "TRUE", "yes", "on", "enabled", " 1 "]
FALSY_VALUES = ["", " ", "0", "false", "FALSE", "off", "no", "disabled"]
UNKNOWN_VALUES = ["2", "banana", "strict-ish"]

# Rule selections with the new strict default.  Composite commands and the
# still-opt-in cd path deliberately use the generic fallback.
DEFAULT_SELECTIONS = {
    "git status": "git/status",
    "git ls-files": "filesystem/git-ls-files",
    "git log --oneline": "git/log-oneline",
    "git worktree list": "git/worktree-list",
    "git -C /a worktree list": "git/worktree-list",
    "git stash list": "git/stash-list",
    "git -C /a ls-files": "filesystem/git-ls-files",
    "git --git-dir=/g/.git status": "git/status",
    "npm test": "tests/npm-test",
    "npm list": "package/npm-ls",
    "npm ls": "package/npm-ls",
    "cargo build": "generic/fallback",
    "cd /tmp/x && git status": "generic/fallback",
    "cd /a && cd b && cargo build": "generic/fallback",
    "cd /a && cd b && git ls-files": "generic/fallback",
    'cd "/tmp/some dir" && git status': "generic/fallback",
    "cd '/tmp/x' && npm ls": "generic/fallback",
    "pushd /tmp/x && git ls-files": "generic/fallback",
    "cd /tmp/x > /dev/null && git status": "generic/fallback",
    "cd /tmp/x | tee log && git status": "generic/fallback",
    "cd /a; git status": "generic/fallback",
    "cd && git status": "generic/fallback",
    "cd\n/tmp/build.sh && git status": "generic/fallback",
    "cd\xa0/a && git status": "generic/fallback",
    "cd /tmp\nmake && make install": "generic/fallback",
}

# Bare-command selections produced by the old permissive matcher.  Explicitly
# disabling strict matching keeps this rollback behavior, except that the new
# composite-command safety guard remains an invariant.
LEGACY_SELECTIONS = {
    "git status": "filesystem/git-ls-files",
    "git ls-files": "filesystem/git-ls-files",
    "git log --oneline": "filesystem/git-ls-files",
    "git worktree list": "filesystem/git-ls-files",
    "git -C /a worktree list": "filesystem/git-ls-files",
    "git stash list": "filesystem/git-ls-files",
    "git -C /a ls-files": "filesystem/git-ls-files",
    "git --git-dir=/g/.git status": "filesystem/git-ls-files",
    "npm test": "package/npm-ls",
    "npm list": "package/npm-ls",
    "npm ls": "package/npm-ls",
    "cargo build": "generic/fallback",
    "cd /tmp/x && git status": "generic/fallback",
    "cd /a && cd b && cargo build": "generic/fallback",
    "cd /a && cd b && git ls-files": "generic/fallback",
    'cd "/tmp/some dir" && git status': "generic/fallback",
    "cd '/tmp/x' && npm ls": "generic/fallback",
    "pushd /tmp/x && git ls-files": "generic/fallback",
    "cd /tmp/x > /dev/null && git status": "generic/fallback",
    "cd /tmp/x | tee log && git status": "generic/fallback",
    "cd /a; git status": "generic/fallback",
    "cd && git status": "generic/fallback",
    "cd\n/tmp/build.sh && git status": "generic/fallback",
    "cd\xa0/a && git status": "generic/fallback",
    "cd /tmp\nmake && make install": "generic/fallback",
}

# The generic/fallback windows are unchanged: failure keeps 50/50 lines while
# success keeps 200/200.
BASELINE_FALLBACK_FAILURE_WINDOW = (50, 50)
BASELINE_FALLBACK_SUCCESS_WINDOW = (200, 200)
BASELINE_NPM_LS_FAILURE_WINDOW = (18, 18)
BASELINE_NPM_LS_SUCCESS_WINDOW = (12, 10)


@pytest.fixture(autouse=True)
def _clear_lever_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ALL_LEVER_ENVS:
        monkeypatch.delenv(name, raising=False)


def _select(
    command: str,
    *,
    exit_code: int = 0,
    tool_name: str = "exec_command",
) -> str | None:
    rule = select_rule(
        load_rules(),
        tool_name=tool_name,
        command=command,
        argv=command_argv(command, None),
        content="one\ntwo\nthree",
        exit_code=exit_code,
    )
    return rule.id if rule else None


def _rule(rule_id: str) -> Rule:
    return next(rule for rule in load_rules() if rule.id == rule_id)


def _numbered_lines(count: int) -> str:
    return "\n".join(f"trace line {index:03d}" for index in range(1, count + 1))


def _synthetic_rule(match: dict[str, object]) -> Rule:
    return Rule(
        id="test/synthetic",
        family="test",
        match=match,
        transforms={},
        filters={},
        summarize={},
        failure={},
        counters=(),
        output_matches=(),
        on_empty=None,
        counter_source="postKeep",
        priority=100,
    )


def _matches(rule: Rule, command: str, *, tool_name: str = "exec_command") -> bool:
    return rule_matches(
        rule,
        tool_name=tool_name,
        command=command,
        argv=command_argv(command, None),
        content="output",
        exit_code=0,
    )


def test_default_selections_use_strict_matching() -> None:
    for command, expected in DEFAULT_SELECTIONS.items():
        assert _select(command) == expected, command


def test_explicit_strict_off_keeps_legacy_simple_command_selections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(STRICT_ENV, "0")
    for command, expected in LEGACY_SELECTIONS.items():
        assert _select(command) == expected, command


def test_default_failure_reduction_matches_baseline_golden() -> None:
    content = _numbered_lines(120)
    expected = (
        "exit 1\n"
        + "\n".join(f"trace line {index:03d}" for index in range(1, 51))
        + "\n... omitted 20 lines ...\n"
        + "\n".join(f"trace line {index:03d}" for index in range(71, 121))
    )
    reduction = reduce_tool_result(
        tool_name="exec_command",
        content=content,
        is_error=True,
        tool_use_id="tu-1",
        command="mystery-tool --verbose",
    )
    assert reduction is not None
    assert reduction.reducer == "generic/fallback"
    assert reduction.inline_text == expected


def test_default_reduction_windows_match_baseline() -> None:
    fallback = _rule("generic/fallback")
    npm_ls = _rule("package/npm-ls")
    assert _summarize_window(fallback, exit_code=1) == BASELINE_FALLBACK_FAILURE_WINDOW
    assert _summarize_window(fallback, exit_code=0) == BASELINE_FALLBACK_SUCCESS_WINDOW
    assert _summarize_window(npm_ls, exit_code=1) == BASELINE_NPM_LS_FAILURE_WINDOW
    assert _summarize_window(npm_ls, exit_code=0) == BASELINE_NPM_LS_SUCCESS_WINDOW


def test_strict_enforces_git_subcommand_criteria(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    assert _select("git status") == "git/status"
    assert _select("git log --oneline") == "git/log-oneline"
    assert _select("git --git-dir=/g/.git status") == "git/status"
    assert _select("git ls-files") == "filesystem/git-ls-files"
    assert _select("git -C /a ls-files") == "filesystem/git-ls-files"


def test_strict_enforces_argv_includes_any_criteria(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    assert _select("npm test") == "tests/npm-test"
    assert _select("npm ls") == "package/npm-ls"
    assert _select("npm list") == "package/npm-ls"


def test_matcher_criteria_compose_with_and_semantics() -> None:
    rule = _synthetic_rule(
        {
            "toolNames": ["exec"],
            "argv0": ["git"],
            "gitSubcommands": ["status"],
            "argvIncludes": [["--short"]],
            "argvIncludesAny": [["--branch"], ["--porcelain", "v2"]],
        }
    )

    assert _matches(rule, "git status --short --branch")
    assert _matches(rule, "git status --short --porcelain v2")
    assert not _matches(rule, "git status --branch")
    assert not _matches(rule, "git log --short --branch")
    assert not _matches(rule, "git status --short --porcelain")


def test_argv_includes_any_accepts_either_declared_alternative() -> None:
    rule = _synthetic_rule(
        {
            "toolNames": ["exec"],
            "argv0": ["tool"],
            "argvIncludesAny": [["alpha"], ["beta"]],
        }
    )

    assert _matches(rule, "tool alpha")
    assert _matches(rule, "tool beta")
    assert not _matches(rule, "tool gamma")


def test_strict_separates_worktree_and_stash_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    assert _select("git worktree list") == "git/worktree-list"
    assert _select("git -C /a worktree list") == "git/worktree-list"
    assert _select("git stash list") == "git/stash-list"


@pytest.mark.parametrize("tool_name", ["Bash", "bash", "exec", "background_process"])
def test_exec_rules_require_the_canonical_shell_tool(tool_name: str) -> None:
    assert _select("git status", tool_name=tool_name) == "generic/fallback"


def test_explicit_non_shell_tool_rules_still_match_their_own_command_field() -> None:
    rule = _synthetic_rule(
        {
            "toolNames": ["http_request"],
            "commandIncludes": ["GET", "/items"],
        }
    )
    command = "GET /items?filter=a|b"
    selected = select_rule(
        (rule, _rule("generic/fallback")),
        tool_name="http_request",
        command=command,
        argv=command_argv(command, None),
        content="response",
        exit_code=0,
    )

    assert selected is rule


def test_strict_does_not_unwrap_cd_prefixes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    assert _select("cd /tmp/x && git status") == "generic/fallback"


@pytest.mark.parametrize(
    "command",
    [
        "git status | tee status.log",
        "git status > status.txt",
        "git status 2>&1",
        "git status; pytest -q",
        "git status\npytest -q",
        "git status && pytest -q",
        "git status || pytest -q",
        "git status & pytest -q",
        "git status $(pytest -q)",
        "git status `pytest -q`",
        "(git status)",
        "git status '",
        "git status \\",
    ],
)
def test_composite_or_unparseable_shell_commands_use_generic_fallback(command: str) -> None:
    assert _select(command) == "generic/fallback"


@pytest.mark.parametrize(
    "command",
    [
        'git status "path|name"',
        r"git status path\|name",
        "git status 'path;name'",
        r"git status path\>name",
        'git status "left && right"',
        "git status '$(literal)'",
        'git status "line\nname"',
    ],
)
def test_quoted_or_escaped_shell_operators_remain_single_commands(command: str) -> None:
    assert _select(command) == "git/status"


@pytest.mark.parametrize(
    "command",
    [
        'cmd /c "cd /d C:\\repo && git status"',
        r"cd /d C:\repo && git status",
        r"cd C:\repo && git status",
        r'cd "C:\repo with spaces" && git status',
        r"pushd C:\repo && git status",
        r'powershell -NoProfile -Command "Set-Location C:\repo; git status"',
        r"Set-Location -LiteralPath C:\repo; git status",
    ],
)
def test_windows_shell_forms_are_not_reinterpreted_by_cd_unwrap(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    monkeypatch.setenv(CD_UNWRAP_ENV, "1")
    assert _select(command) == "generic/fallback"


def test_cd_unwrap_classifies_like_bare_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CD_UNWRAP_ENV, "1")
    pairs = [
        ("cd /tmp/x && git status", "git status"),
        ("cd /a && cd b && cargo build", "cargo build"),
        ("cd /a && cd b && git ls-files", "git ls-files"),
        ('cd "/tmp/some dir" && git status', "git status"),
        ("cd '/tmp/x' && npm ls", "npm ls"),
        ("pushd /tmp/x && git ls-files", "git ls-files"),
    ]
    for wrapped, bare in pairs:
        assert _select(wrapped) == _select(bare) == DEFAULT_SELECTIONS[bare], wrapped


def test_cd_unwrap_leaves_unsafe_prefixes_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CD_UNWRAP_ENV, "1")
    for command in [
        "cd /tmp/x > /dev/null && git status",
        "cd /tmp/x | tee log && git status",
        "cd /a; git status",
        "cd && git status",
        'cd "$(printf /tmp)" && git status',
        "cd `printf /tmp` && git status",
        'pushd "$(printf /tmp)" && pytest -q',
    ]:
        assert _select(command) == "generic/fallback", command


@pytest.mark.parametrize(
    "command",
    [
        "cd /tmp/x && git status | tee status.log",
        "cd /tmp/x && git status > status.txt",
        "cd /tmp/x && git status; pytest -q",
        "cd /tmp/x && git status\npytest -q",
        "cd /tmp/x && git status && pytest -q",
        "cd /tmp/x && git status || pytest -q",
        "cd /tmp/x && git status '",
    ],
)
def test_cd_unwrap_rejects_composite_or_unparseable_tails(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    monkeypatch.setenv(CD_UNWRAP_ENV, "1")
    assert _select(command) == "generic/fallback"


def test_cd_unwrap_requires_horizontal_keyword_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CD_UNWRAP_ENV, "1")
    for command in [
        "cd\n/tmp/build.sh && git status",
        "cd\xa0/a && git status",
        "cd /tmp\nmake && make install",
    ]:
        assert _select(command) == "generic/fallback", command


@pytest.mark.parametrize("strict_value", FALSY_VALUES)
def test_cd_unwrap_fails_safe_when_strict_is_explicitly_off(
    monkeypatch: pytest.MonkeyPatch,
    strict_value: str,
) -> None:
    monkeypatch.setenv(STRICT_ENV, strict_value)
    monkeypatch.setenv(CD_UNWRAP_ENV, "1")
    assert _select("cd /tmp/x && git status") == "generic/fallback"
    assert _select("git status") == "filesystem/git-ls-files"


def test_strict_and_cd_unwrap_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    monkeypatch.setenv(CD_UNWRAP_ENV, "1")
    assert _select("cd /tmp/x && git status") == "git/status"
    assert _select('cd "/tmp/some dir" && npm test') == "tests/npm-test"
    assert _select("pushd /tmp/x && git ls-files") == "filesystem/git-ls-files"
    assert _select("cd /tmp/x && git worktree list") == "git/worktree-list"


def test_strip_leading_cd_prefix_rules() -> None:
    assert strip_leading_cd_prefix("cd /tmp/x && git status") == "git status"
    assert strip_leading_cd_prefix("pushd /tmp/x && git status") == "git status"
    assert strip_leading_cd_prefix("cd /a && cd b && cargo build") == "cargo build"
    assert strip_leading_cd_prefix('cd "/tmp/some dir" && git status') == "git status"
    assert strip_leading_cd_prefix("cd '/tmp/x' && npm ls") == "npm ls"
    assert strip_leading_cd_prefix("cd /tmp/my\\ dir && ls") == "ls"
    assert strip_leading_cd_prefix("cd /tmp/x >out && ls") == "cd /tmp/x >out && ls"
    assert strip_leading_cd_prefix("cd /a; ls") == "cd /a; ls"
    assert strip_leading_cd_prefix("cd '/unterminated && ls") == "cd '/unterminated && ls"
    assert strip_leading_cd_prefix("echo cd /a && ls") == "echo cd /a && ls"
    assert strip_leading_cd_prefix("cd && ls") == "cd && ls"
    assert strip_leading_cd_prefix("cd /a &&") == "cd /a &&"
    assert strip_leading_cd_prefix("cd\n/tmp/build.sh && ls") == "cd\n/tmp/build.sh && ls"
    assert strip_leading_cd_prefix("cd\xa0/a && ls") == "cd\xa0/a && ls"
    assert strip_leading_cd_prefix("cd /a\n&& ls") == "cd /a\n&& ls"
    assert (
        strip_leading_cd_prefix('cd "$(printf /tmp)" && git status')
        == 'cd "$(printf /tmp)" && git status'
    )
    assert (
        strip_leading_cd_prefix("cd `printf /tmp` && git status")
        == "cd `printf /tmp` && git status"
    )
    assert strip_leading_cd_prefix("cd /a \n && ls") == "cd /a \n && ls"
    chained = "cd /a && " * 9 + "ls"
    assert strip_leading_cd_prefix(chained) == "cd /a && ls"


def test_default_strict_rule_reduces_end_to_end() -> None:
    content = "\n".join(
        ["On branch main", "Your branch is up to date with 'origin/main'."]
        + [f"\tmodified:   src/file_{index:03d}.py" for index in range(80)]
    )
    reduction = reduce_tool_result(
        tool_name="exec_command",
        content=content,
        is_error=False,
        tool_use_id="tu-strict",
        command="git status",
    )

    assert reduction is not None
    assert reduction.reducer == "git/status"
    assert "modified file: 80" in reduction.inline_text
    assert "On branch main" not in reduction.inline_text
    assert "... omitted 66 lines ..." in reduction.inline_text


def test_composite_shell_result_reduces_with_generic_fallback_end_to_end() -> None:
    content = _numbered_lines(500)
    reduction = reduce_tool_result(
        tool_name="exec_command",
        content=content,
        is_error=False,
        tool_use_id="tu-composite",
        command="git status && pytest -q",
    )

    assert reduction is not None
    assert reduction.reducer == "generic/fallback"
    assert "... omitted 100 lines ..." in reduction.inline_text


def test_failure_preserve_widens_smaller_failure_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(FAILURE_PRESERVE_ENV, "1")
    fallback = _rule("generic/fallback")
    assert _summarize_window(fallback, exit_code=1) == BASELINE_FALLBACK_SUCCESS_WINDOW
    assert _summarize_window(fallback, exit_code=0) == BASELINE_FALLBACK_SUCCESS_WINDOW
    npm_ls = _rule("package/npm-ls")
    assert _summarize_window(npm_ls, exit_code=1) == BASELINE_NPM_LS_FAILURE_WINDOW
    assert _summarize_window(npm_ls, exit_code=0) == BASELINE_NPM_LS_SUCCESS_WINDOW


def test_failure_preserve_invariant_across_all_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAILURE_PRESERVE_ENV, "1")
    for rule in load_rules():
        failure_head, failure_tail = _summarize_window(rule, exit_code=1)
        success_head, success_tail = _summarize_window(rule, exit_code=0)
        assert failure_head >= success_head, rule.id
        assert failure_tail >= success_tail, rule.id


def test_failure_preserve_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    fallback = _rule("generic/fallback")
    content = _numbered_lines(120)
    summary_off, _ = reduce_with_rule(fallback, content, exit_code=1)
    assert "... omitted 20 lines ..." in summary_off

    monkeypatch.setenv(FAILURE_PRESERVE_ENV, "1")
    summary_on, _ = reduce_with_rule(fallback, content, exit_code=1)
    assert "omitted" not in summary_on
    assert len(summary_on.splitlines()) == 120

    summary_success, _ = reduce_with_rule(fallback, content, exit_code=0)
    assert summary_success == content


def test_failure_preserve_does_not_change_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAILURE_PRESERVE_ENV, "1")
    assert _select("git status") == "git/status"
    assert _select("cd /tmp/x && git status") == "generic/fallback"


@pytest.mark.parametrize("value", TRUTHY_VALUES)
def test_truthy_env_values_enable_levers(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(STRICT_ENV, value)
    assert _select("git status") == "git/status"
    monkeypatch.setenv(CD_UNWRAP_ENV, value)
    assert _select("cd '/tmp/x' && npm ls") == "package/npm-ls"
    monkeypatch.setenv(FAILURE_PRESERVE_ENV, value)
    assert _summarize_window(_rule("generic/fallback"), exit_code=1) == (
        BASELINE_FALLBACK_SUCCESS_WINDOW
    )


@pytest.mark.parametrize("value", FALSY_VALUES)
def test_explicit_falsy_env_values_keep_levers_off(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv(STRICT_ENV, value)
    monkeypatch.setenv(CD_UNWRAP_ENV, value)
    monkeypatch.setenv(FAILURE_PRESERVE_ENV, value)
    assert _select("git status") == "filesystem/git-ls-files"
    assert _select("cd /tmp/x && git status") == "generic/fallback"
    assert _summarize_window(_rule("generic/fallback"), exit_code=1) == (
        BASELINE_FALLBACK_FAILURE_WINDOW
    )


@pytest.mark.parametrize("value", UNKNOWN_VALUES)
def test_unknown_strict_value_fails_safe_to_enabled(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv(STRICT_ENV, value)
    assert _select("git status") == "git/status"
    assert _select("npm test") == "tests/npm-test"


@pytest.mark.parametrize("value", UNKNOWN_VALUES)
def test_unknown_opt_in_values_keep_cd_and_failure_preserve_off(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv(CD_UNWRAP_ENV, value)
    monkeypatch.setenv(FAILURE_PRESERVE_ENV, value)
    assert _select("cd /tmp/x && git status") == "generic/fallback"
    assert _summarize_window(_rule("generic/fallback"), exit_code=1) == (
        BASELINE_FALLBACK_FAILURE_WINDOW
    )
