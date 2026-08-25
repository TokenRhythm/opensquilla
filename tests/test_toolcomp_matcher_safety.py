from __future__ import annotations

import pytest

from opensquilla.plugins.tokenjuice.matcher import command_argv, rule_matches, select_rule
from opensquilla.plugins.tokenjuice.plugin import reduce_tool_result
from opensquilla.plugins.tokenjuice.rules import load_rules
from opensquilla.plugins.tokenjuice.types import Rule

STRICT_ENV = "OPENSQUILLA_TOOLCOMP_MATCHER_STRICT"


@pytest.fixture(autouse=True)
def _clear_strict_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(STRICT_ENV, raising=False)


def _select(command: str, *, content: str = "output") -> str | None:
    rule = select_rule(
        load_rules(),
        tool_name="exec_command",
        command=command,
        argv=command_argv(command, None),
        content=content,
        exit_code=0,
    )
    return rule.id if rule else None


def _rule(rule_id: str) -> Rule:
    return next(rule for rule in load_rules() if rule.id == rule_id)


def _matches(rule_id: str, command: str, *, content: str = "output") -> bool:
    return rule_matches(
        _rule(rule_id),
        tool_name="exec_command",
        command=command,
        argv=command_argv(command, None),
        content=content,
        exit_code=0,
    )


@pytest.mark.parametrize(
    "command",
    [
        "sh -c 'git status'",
        "bash -lc 'git status'",
        "zsh -xec 'git status'",
        "csh -c 'git status'",
        "tcsh -c 'git status'",
        "yash -c 'git status'",
        "dash -c 'git status'",
        "fish -c 'git status'",
        "nu -c 'git status'",
        "sh.exe -ec 'git status'",
        "/usr/bin/bash -xc 'git status'",
        "/usr/bin/env bash -lc 'git status'",
        "/usr/bin/env -S 'bash -lc \"git status && pytest -q\"'",
        "/usr/bin/env --split-string='bash -lc \"git status && pytest -q\"'",
        r'/usr/bin/env -S "bash\_-lc\_\"git status && pytest -q\""',
        "/usr/bin/env -S '${TASK_SHELL} -lc \"git status && pytest -q\"'",
        "env PROFILE=test pwsh -Command 'git status'",
        '$SHELL -lc "git status && pytest -q"',
        '${TASK_SHELL} -lc "git status && pytest -q"',
        'FOO=1 BAR="two words" bash -lc "git status && pytest -q"',
        '%COMSPEC% /d /c"git status && pytest -q"',
        r'C:\Windows\System32\cmd.exe /c "git status && pytest -q"',
        (
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe "
            '-Command "git status; pytest -q"'
        ),
        'eval "git status && pytest -q"',
        'iex "git status; pytest -q"',
        'call "git status && pytest -q"',
        "cmd /c 'git status'",
        "cmd.exe /C 'git status'",
        "cmd.exe /k 'git status'",
        "C:/Windows/System32/cmd.exe /d /s /c 'git status'",
        'cmd.exe /c"git status && pytest -q"',
        'cmd.exe /k"git status && pytest -q"',
        'cmd.exe /d/c"git status && pytest -q"',
        "powershell -Command 'git status'",
        'powershell -Command:"git status; pytest -q"',
        "powershell.exe -EncodedCommand Z2l0IHN0YXR1cw==",
        "powershell.exe -enc Z2l0IHN0YXR1cw==",
        "pwsh -c 'git status'",
        "pwsh -CommandWithArgs 'git status'",
        "pwsh.exe -NoProfile -Command 'git status'",
    ],
)
def test_shell_dispatch_wrappers_always_use_generic_fallback(command: str) -> None:
    assert _select(command) == "generic/fallback"


@pytest.mark.parametrize(
    "command",
    [
        "bash -lc 'git status'",
        "zsh.exe -xec 'git status && pytest -q'",
        "cmd.exe /c 'git status'",
        "pwsh -Command 'git status'",
        'FOO=1 bash -lc "git status && pytest -q"',
    ],
)
def test_shell_dispatch_safety_is_not_disabled_by_legacy_matcher(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    monkeypatch.setenv(STRICT_ENV, "0")
    assert _select(command) == "generic/fallback"


def test_explicit_wrapper_argv_cannot_bypass_command_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(STRICT_ENV, "0")
    rule = select_rule(
        load_rules(),
        tool_name="exec_command",
        command="pytest -q",
        argv=["bash", "-lc", "git status && pytest -q"],
        content="output",
        exit_code=1,
    )
    assert rule is not None
    assert rule.id == "generic/fallback"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("pytest -q", "tests/pytest"),
        ("pytest -k eval", "tests/pytest"),
        ("pytest -k bash -c pyproject.toml", "tests/pytest"),
        ("/workspace/.venv/bin/pytest -q", "tests/pytest"),
        (r'"C:\workspace\.venv\Scripts\pytest.exe" -q', "tests/pytest"),
        ("C:/workspace/.venv/Scripts/pytest.exe -q", "tests/pytest"),
        ("PYTHONPATH=src pytest -q", "tests/pytest"),
        ("python -m pytest -q", "tests/pytest"),
        ("PYTHONPATH=src python -m pytest -q", "tests/pytest"),
        ("uv run pytest -q", "tests/pytest"),
        ("npx jest --runInBand", "tests/jest"),
        ("pnpm exec vitest run", "tests/vitest"),
        ("vite build", "build/vite"),
        ("npx vite build", "build/vite"),
        ("prettier src --check", "lint/prettier-check"),
        ("npx prettier src --check", "lint/prettier-check"),
    ],
)
def test_command_basename_rules_follow_the_actual_invoked_command(
    command: str,
    expected: str,
) -> None:
    assert _select(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "./run-pytest-and-git.sh",
        "sh ./run-pytest-and-git.sh",
        "python run-pytest-and-git.py",
    ],
)
def test_tool_names_inside_script_paths_do_not_select_test_reducers(command: str) -> None:
    assert _select(command) != "tests/pytest"


@pytest.mark.parametrize(
    "command",
    [
        r"C:\tools\notpytest.exe --verbose",
        r"C:\tools\myjest.exe --verbose",
        r"C:\tools\notvitest.exe --verbose",
    ],
)
def test_ambiguous_unquoted_windows_paths_do_not_suffix_match_test_tools(
    command: str,
) -> None:
    assert _select(command) == "generic/fallback"


def test_ambiguous_windows_path_keeps_critical_non_test_diagnostic() -> None:
    content_lines = [f"ordinary diagnostic {index:03d}" for index in range(500)]
    # The generic failure window keeps this line; the much narrower pytest
    # failure window would drop it if the executable suffix were misclassified.
    content_lines.insert(30, "CRITICAL LICENSE FAILURE CODE 77")

    reduction = reduce_tool_result(
        tool_name="exec_command",
        content="\n".join(content_lines),
        is_error=True,
        tool_use_id="tu-ambiguous-windows-path",
        command=r"C:\tools\notpytest.exe --verbose",
    )

    assert reduction is not None
    assert reduction.reducer == "generic/fallback"
    assert "CRITICAL LICENSE FAILURE CODE 77" in reduction.inline_text


def test_command_basename_criterion_is_strict_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not _matches("tests/pytest", "echo pytest")
    monkeypatch.setenv(STRICT_ENV, "0")
    assert _matches("tests/pytest", "echo pytest")
    assert _select("python -m pytest -q") == "task/python"


@pytest.mark.parametrize(
    "command",
    [
        "vite --config build.config.ts",
        "npx vite --config build.config.ts",
        "prettier --config ./--check.json src",
        "npx prettier --config ./--check.json src",
    ],
)
def test_mode_words_inside_config_paths_do_not_select_specialized_rules(
    command: str,
) -> None:
    assert _select(command) == "generic/fallback"


def test_wrapper_words_in_pytest_arguments_do_not_hide_middle_failure() -> None:
    content = "\n".join(
        [
            *[f"setup trace {index:02d}" for index in range(60)],
            "FAILED tests/test_eval.py::test_regression - AssertionError",
            *[f"teardown trace {index:02d}" for index in range(60)],
        ]
    )

    for command in (
        "pytest -k eval",
        "pytest -k bash -c pyproject.toml",
        "PYTHONPATH=src pytest -q",
    ):
        reduction = reduce_tool_result(
            tool_name="exec_command",
            content=content,
            is_error=True,
            tool_use_id="tu-wrapper-word-argument",
            command=command,
        )
        assert reduction is not None
        assert reduction.reducer == "tests/pytest"
        assert "FAILED tests/test_eval.py::test_regression" in reduction.inline_text


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git branch", "git/branch"),
        ("git diff --name-only HEAD~1", "git/diff-name-only"),
        ("git diff --stat HEAD~1", "git/diff-stat"),
        ("git diff -- src/module.py", "git/diff"),
        ("git log --oneline", "git/log-oneline"),
        ("git remote -v", "git/remote-v"),
        ("git show HEAD", "git/show"),
        ("git show status", "git/show"),
        ("git grep status", "search/git-grep"),
        ("git -C repo status", "git/status"),
        ("GIT_OPTIONAL_LOCKS=0 git status", "git/status"),
    ],
)
def test_git_rules_require_the_real_git_verb(command: str, expected: str) -> None:
    content = "diff --git a/src/module.py b/src/module.py" if expected == "git/diff" else "output"
    assert _select(command, content=content) == expected


@pytest.mark.parametrize(
    ("rule_id", "positive", "wrong_verb"),
    [
        ("git/branch", "git branch", "git rev-parse branch"),
        ("git/diff-name-only", "git diff --name-only", "git config --name-only diff"),
        ("git/diff-stat", "git diff --stat", "git config --stat diff"),
        ("git/diff", "git diff -- path", "git config git diff value"),
        ("git/log-oneline", "git log --oneline", "git config log --oneline"),
        ("git/remote-v", "git remote -v", "git config remote -v"),
        ("git/show", "git show HEAD", "git rev-parse show"),
        ("search/git-grep", "git grep needle", "git rev-parse grep"),
    ],
)
def test_each_git_rule_checks_its_declared_subcommand(
    rule_id: str,
    positive: str,
    wrong_verb: str,
) -> None:
    content = "diff --git a/src/module.py b/src/module.py" if rule_id == "git/diff" else "output"
    assert _matches(rule_id, positive, content=content)
    assert not _matches(rule_id, wrong_verb, content=content)


@pytest.mark.parametrize(
    ("rule_id", "positive", "wrong_shape"),
    [
        (
            "git/diff-name-only",
            "git -C repo diff --name-only",
            "git -C repo diff -- --name-only",
        ),
        ("git/diff-stat", "git -C repo diff --stat", "git -C repo diff -- --stat"),
        ("git/log-oneline", "git log --oneline", "git log -- --oneline"),
        ("git/remote-v", "git remote -v", "git remote get-url -v"),
        ("git/stash-list", "git stash list", "git stash push list"),
        ("git/worktree-list", "git worktree list", "git worktree add list"),
    ],
)
def test_git_option_and_action_rules_require_their_semantic_position(
    rule_id: str,
    positive: str,
    wrong_shape: str,
) -> None:
    assert _matches(rule_id, positive)
    assert not _matches(rule_id, wrong_shape)
    assert _select(wrong_shape) == "generic/fallback"


def test_wrong_git_option_position_keeps_non_git_failure_evidence() -> None:
    content_lines = [f"ordinary diagnostic {index:03d}" for index in range(500)]
    content_lines.insert(30, "CRITICAL LICENSE FAILURE CODE 77")

    reduction = reduce_tool_result(
        tool_name="exec_command",
        content="\n".join(content_lines),
        is_error=True,
        tool_use_id="tu-wrong-git-option-position",
        command="git log -- --oneline",
    )

    assert reduction is not None
    assert reduction.reducer == "generic/fallback"
    assert "CRITICAL LICENSE FAILURE CODE 77" in reduction.inline_text


@pytest.mark.parametrize(
    ("command", "expected_reducer", "first_evidence", "last_evidence"),
    [
        (
            "git diff --stat",
            "git/diff-stat",
            "src/file_000.py | 1 +",
            "80 files changed, 80 insertions(+)",
        ),
        (
            "git diff --name-only",
            "git/diff-name-only",
            "src/file_000.py",
            "src/file_079.py",
        ),
    ],
)
def test_specific_git_diff_reducers_preserve_bounded_file_evidence(
    command: str,
    expected_reducer: str,
    first_evidence: str,
    last_evidence: str,
) -> None:
    if command.endswith("--stat"):
        lines = [f"src/file_{index:03d}.py | 1 +" for index in range(80)]
        lines.append("80 files changed, 80 insertions(+)")
    else:
        lines = [f"src/file_{index:03d}.py" for index in range(80)]

    reduction = reduce_tool_result(
        tool_name="exec_command",
        content="\n".join(lines),
        is_error=False,
        tool_use_id="tu-specific-git-diff",
        command=command,
    )

    assert reduction is not None
    assert reduction.reducer == expected_reducer
    assert first_evidence in reduction.inline_text
    assert last_evidence in reduction.inline_text


def test_specific_git_diff_priority_is_strict_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(STRICT_ENV, "0")
    assert _select("git diff --stat") == "filesystem/git-ls-files"


@pytest.mark.parametrize(
    ("command", "line_template"),
    [
        ("git diff --numstat", "1\t0\tsrc/file_{index:03d}.py"),
        ("git diff --name-status", "M\tsrc/file_{index:03d}.py"),
        ("git diff --raw", ":100644 100644 abcdef0 1234567 M\tsrc/file_{index:03d}.py"),
    ],
)
def test_non_patch_git_diff_modes_do_not_use_patch_reducer(
    command: str,
    line_template: str,
) -> None:
    content = "\n".join(line_template.format(index=index) for index in range(80))

    assert _select(command, content=content) == "generic/fallback"
    assert (
        reduce_tool_result(
            tool_name="exec_command",
            content=content,
            is_error=False,
            tool_use_id="tu-non-patch-git-diff",
            command=command,
        )
        is None
    )


def test_full_patch_git_diff_still_uses_patch_reducer() -> None:
    content = "\n".join(
        [
            "diff --git a/src/module.py b/src/module.py",
            "index 1234567..abcdef0 100644",
            "--- a/src/module.py",
            "+++ b/src/module.py",
            "@@ -1,3 +1,3 @@",
            *[f"+added line {index:03d}" for index in range(80)],
        ]
    )

    reduction = reduce_tool_result(
        tool_name="exec_command",
        content=content,
        is_error=False,
        tool_use_id="tu-full-patch-git-diff",
        command="git diff -- src/module.py",
    )

    assert reduction is not None
    assert reduction.reducer == "git/diff"
    assert "diff --git a/src/module.py b/src/module.py" in reduction.inline_text


@pytest.mark.parametrize(
    "command",
    [
        "git rev-parse status",
        "git rev-parse branch",
        "git rev-parse show",
        "git rev-parse grep",
        "git config diff",
        "git config remote -v",
        "git config log --oneline",
    ],
)
def test_git_verb_names_in_arguments_do_not_select_git_rules(command: str) -> None:
    assert _select(command) == "generic/fallback"


@pytest.mark.parametrize(
    "command",
    [
        "git -h status",
        "git --help status",
        "git --version status",
        "git --html-path status",
        "git --exec-path status",
        "git --man-path status",
        "git --info-path status",
        "git --list-cmds=main status",
        "git -v status",
        "git -- status",
    ],
)
def test_git_terminal_global_options_do_not_fabricate_a_subcommand(command: str) -> None:
    assert _select(command) != "git/status"


@pytest.mark.parametrize(
    "command",
    ["npm ls", "npm list", "npm ls react", "NODE_ENV=test npm ls"],
)
def test_npm_ls_rule_accepts_only_ls_and_list_prefixes(command: str) -> None:
    assert _select(command) == "package/npm-ls"


@pytest.mark.parametrize(
    "command",
    [
        "npm run inspect -- ls",
        "npm exec helper -- list",
        "npm query ls",
    ],
)
def test_npm_ls_tokens_later_in_argv_do_not_select_npm_ls_rule(command: str) -> None:
    assert _select(command) == "generic/fallback"


def test_npm_ls_prefix_check_is_strict_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(STRICT_ENV, "0")
    assert _select("npm run inspect -- ls") == "package/npm-ls"


@pytest.mark.parametrize(
    "command",
    [
        "npm test",
        "npm test -- --runInBand",
        "npm run test",
        "npm run test -- --runInBand",
        "npm run-script test",
        "CI=1 npm test",
    ],
)
def test_npm_test_rule_accepts_only_test_script_prefixes(command: str) -> None:
    assert _select(command) == "tests/npm-test"


@pytest.mark.parametrize(
    "command",
    [
        "npm run helper -- test",
        "npm exec helper -- test",
        "npm install test",
        "npm run testing",
    ],
)
def test_npm_test_tokens_later_in_argv_do_not_select_test_rule(command: str) -> None:
    assert _select(command) != "tests/npm-test"


def test_npm_test_prefix_check_is_strict_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(STRICT_ENV, "0")
    assert _matches("tests/npm-test", "npm run foo -- test")
    assert _select("npm run foo -- test") == "package/npm-ls"


@pytest.mark.parametrize(
    "command",
    [
        "FOO=1 docker run alpine build",
        "FOO=1 kubectl exec pod -- get svc",
        "FOO=1 cargo run -- test",
        "FOO=1 pnpm exec helper -- test",
    ],
)
def test_assignments_do_not_expand_unhardened_legacy_rule_matching(command: str) -> None:
    assert _select(command) == "generic/fallback"


def test_assignment_does_not_expand_legacy_rule_and_drop_diagnostic() -> None:
    content_lines = [f"ordinary diagnostic {index:03d}" for index in range(500)]
    content_lines.insert(30, "CRITICAL LICENSE FAILURE CODE 77")

    reduction = reduce_tool_result(
        tool_name="exec_command",
        content="\n".join(content_lines),
        is_error=True,
        tool_use_id="tu-assignment-legacy-rule",
        command="FOO=1 docker run alpine build",
    )

    assert reduction is not None
    assert reduction.reducer == "generic/fallback"
    assert "CRITICAL LICENSE FAILURE CODE 77" in reduction.inline_text


def test_nested_shell_mixed_output_keeps_both_head_and_tail_evidence() -> None:
    git_lines = [
        "On branch feature",
        "Your branch is ahead of 'origin/main' by 2 commits.",
        *[f"\tmodified:   src/file_{index:03d}.py" for index in range(140)],
    ]
    pytest_lines = [
        "FAILED tests/test_example.py::test_regression - AssertionError",
        "1 failed in 0.10s",
    ]
    content = "\n".join([*git_lines, *pytest_lines])

    reduction = reduce_tool_result(
        tool_name="exec_command",
        content=content,
        is_error=True,
        tool_use_id="tu-nested-shell",
        command='bash -lc "git status && pytest -q"',
    )

    assert reduction is not None
    assert reduction.reducer == "generic/fallback"
    assert "Your branch is ahead of 'origin/main' by 2 commits." in reduction.inline_text
    assert "\tmodified:   src/file_000.py" in reduction.inline_text
    assert "\tmodified:   src/file_139.py" in reduction.inline_text
    assert "FAILED tests/test_example.py::test_regression" in reduction.inline_text


@pytest.mark.parametrize(
    "command",
    [
        'cmd.exe /d/c"git status && pytest -q"',
        "/usr/bin/env -S 'bash -lc \"git status && pytest -q\"'",
        "/usr/bin/env --split-string='bash -lc \"git status && pytest -q\"'",
        r'/usr/bin/env -S "bash\_-lc\_\"git status && pytest -q\""',
        "/usr/bin/env -S '${TASK_SHELL} -lc \"git status && pytest -q\"'",
        'powershell -Command:"git status; pytest -q"',
        '$SHELL -lc "git status && pytest -q"',
        'FOO=1 bash -lc "git status && pytest -q"',
        '%COMSPEC% /d /c"git status && pytest -q"',
        r'C:\Windows\System32\cmd.exe /c "git status && pytest -q"',
        'eval "git status && pytest -q"',
        "./run-pytest-and-git.sh",
        "sh ./run-pytest-and-git.sh",
    ],
)
def test_hidden_dispatch_declines_lossy_reduction_for_short_mixed_output(
    command: str,
) -> None:
    content = "\n".join(
        [
            *[f"trace before {index:02d}" for index in range(40)],
            "Your branch is ahead of 'origin/main' by 2 commits.",
            *[f"trace after {index:02d}" for index in range(40)],
            "FAILED tests/test_example.py::test_regression - AssertionError",
            "1 failed in 0.10s",
        ]
    )

    assert _select(command) == "generic/fallback"
    assert (
        reduce_tool_result(
            tool_name="exec_command",
            content=content,
            is_error=True,
            tool_use_id="tu-hidden-dispatch",
            command=command,
        )
        is None
    )
