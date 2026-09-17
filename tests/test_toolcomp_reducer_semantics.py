from __future__ import annotations

import pytest

from opensquilla.plugins.tokenjuice.reducer import reduce_with_rule
from opensquilla.plugins.tokenjuice.rules import load_rules
from opensquilla.plugins.tokenjuice.types import Rule


def _rule(rule_id: str) -> Rule:
    return next(rule for rule in load_rules() if rule.id == rule_id)


@pytest.mark.parametrize(
    ("raw_text", "expected"),
    [
        (
            "\n".join(
                [
                    "On branch feature",
                    "Your branch is ahead of 'origin/feature' by 3 commits.",
                    '  (use "git push" to publish your local commits)',
                    '    use "git restore <file>..." to discard changes',
                    "",
                    "nothing to commit, working tree clean",
                ]
            ),
            "Your branch is ahead of 'origin/feature' by 3 commits.",
        ),
        (
            "\n".join(
                [
                    "On branch feature",
                    (
                        "Your branch is behind 'origin/feature' by 2 commits, "
                        "and can be fast-forwarded."
                    ),
                    '  (use "git pull" to update your local branch)',
                    "",
                    "nothing to commit, working tree clean",
                ]
            ),
            ("Your branch is behind 'origin/feature' by 2 commits, and can be fast-forwarded."),
        ),
        (
            "\n".join(
                [
                    "On branch feature",
                    "Your branch and 'origin/feature' have diverged,",
                    "and have 2 and 4 different commits each, respectively.",
                    '  (use "git pull" to merge the remote branch into yours)',
                    "",
                    "nothing to commit, working tree clean",
                ]
            ),
            "\n".join(
                [
                    "Your branch and 'origin/feature' have diverged,",
                    "and have 2 and 4 different commits each, respectively.",
                ]
            ),
        ),
    ],
    ids=["ahead", "behind", "diverged"],
)
def test_git_status_preserves_branch_relation_counts(
    raw_text: str,
    expected: str,
) -> None:
    summary, facts = reduce_with_rule(_rule("git/status"), raw_text, exit_code=0)

    assert summary == expected
    assert facts == {
        "modified file": 0,
        "new file": 0,
        "deleted file": 0,
        "untracked file": 0,
    }
    assert len(summary) / len(raw_text) < 0.6


def test_npm_failure_keeps_bounded_v8_stack_without_polluting_counters() -> None:
    stack_frames = [
        (
            f"    at passingFailureFrame{index:02d} "
            f"(/workspace/src/case-{index:02d}.test.ts:{index + 10}:7)"
        )
        for index in range(1, 81)
    ]
    lines = [
        "FAIL src/regression.test.ts",
        "Error: expected true to be false",
        *stack_frames,
        "PASS src/health.test.ts",
    ]
    raw_text = "\n".join(lines)

    summary, facts = reduce_with_rule(_rule("tests/npm-test"), raw_text, exit_code=1)
    expected = "\n".join(
        [
            *lines[:16],
            "... omitted 51 lines ...",
            *lines[-16:],
        ]
    )

    assert summary == expected
    assert "    at passingFailureFrame01 " in summary
    assert "    at passingFailureFrame80 " in summary
    assert facts == {"failed": 1, "passed": 1}
    assert len(summary) / len(raw_text) < 0.5
