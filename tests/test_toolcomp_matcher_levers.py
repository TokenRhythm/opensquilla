from __future__ import annotations

import pytest

from opensquilla.plugins.tokenjuice.matcher import (
    command_argv,
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
FALSY_VALUES = ["", " ", "0", "false", "off", "no", "2", "banana"]

# Rule selections captured with every lever unset; default-off runs must
# reproduce them exactly.
BASELINE_SELECTIONS = {
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

# generic/fallback windows captured with every lever unset: failure keeps
# 50/50 lines while success keeps 200/200.
BASELINE_FALLBACK_FAILURE_WINDOW = (50, 50)
BASELINE_FALLBACK_SUCCESS_WINDOW = (200, 200)
BASELINE_NPM_LS_FAILURE_WINDOW = (18, 18)
BASELINE_NPM_LS_SUCCESS_WINDOW = (12, 10)


@pytest.fixture(autouse=True)
def _clear_lever_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ALL_LEVER_ENVS:
        monkeypatch.delenv(name, raising=False)


def _select(command: str, *, exit_code: int = 0) -> str | None:
    rule = select_rule(
        load_rules(),
        tool_name="Bash",
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


def test_default_off_selections_match_baseline() -> None:
    for command, expected in BASELINE_SELECTIONS.items():
        assert _select(command) == expected, command


def test_default_off_failure_reduction_matches_baseline_golden() -> None:
    content = _numbered_lines(120)
    expected = (
        "exit 1\n"
        + "\n".join(f"trace line {index:03d}" for index in range(1, 51))
        + "\n... omitted 20 lines ...\n"
        + "\n".join(f"trace line {index:03d}" for index in range(71, 121))
    )
    reduction = reduce_tool_result(
        tool_name="Bash",
        content=content,
        is_error=True,
        tool_use_id="tu-1",
        command="mystery-tool --verbose",
    )
    assert reduction is not None
    assert reduction.reducer == "generic/fallback"
    assert reduction.inline_text == expected


def test_default_off_windows_match_baseline() -> None:
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


def test_strict_separates_worktree_and_stash_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    assert _select("git worktree list") == "git/worktree-list"
    assert _select("git -C /a worktree list") == "git/worktree-list"
    assert _select("git stash list") == "git/stash-list"


def test_strict_does_not_unwrap_cd_prefixes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    assert _select("cd /tmp/x && git status") == "generic/fallback"


def test_strict_does_not_change_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    assert _summarize_window(_rule("generic/fallback"), exit_code=1) == (
        BASELINE_FALLBACK_FAILURE_WINDOW
    )


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
        assert _select(wrapped) == _select(bare) == BASELINE_SELECTIONS[bare], wrapped


def test_cd_unwrap_leaves_unsafe_prefixes_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CD_UNWRAP_ENV, "1")
    for command in [
        "cd /tmp/x > /dev/null && git status",
        "cd /tmp/x | tee log && git status",
        "cd /a; git status",
        "cd && git status",
    ]:
        assert _select(command) == "generic/fallback", command


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


def test_cd_unwrap_does_not_enable_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CD_UNWRAP_ENV, "1")
    assert _select("cd /tmp/x && git status") == "filesystem/git-ls-files"
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
    assert strip_leading_cd_prefix("cd /a \n && ls") == "cd /a \n && ls"
    chained = "cd /a && " * 9 + "ls"
    assert strip_leading_cd_prefix(chained) == "cd /a && ls"


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
    assert _select("git status") == "filesystem/git-ls-files"
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
def test_falsy_env_values_keep_levers_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(STRICT_ENV, value)
    monkeypatch.setenv(CD_UNWRAP_ENV, value)
    monkeypatch.setenv(FAILURE_PRESERVE_ENV, value)
    assert _select("git status") == "filesystem/git-ls-files"
    assert _select("cd /tmp/x && git status") == "generic/fallback"
    assert _summarize_window(_rule("generic/fallback"), exit_code=1) == (
        BASELINE_FALLBACK_FAILURE_WINDOW
    )
