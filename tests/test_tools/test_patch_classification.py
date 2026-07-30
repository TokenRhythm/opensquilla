"""Unified-diff instrumentation classification.

Covers the classifier behind the endgame instrumentation policies: the git
freeze exemption (OPENSQUILLA_ENDGAME_GIT_FREEZE_INSTRUMENTATION_EXEMPT) and
the final-diff salvage veto (OPENSQUILLA_FINAL_DIFF_SALVAGE_VETO) both need to
tell diagnostic print/log additions apart from substantive changes.
Classification is conservative: anything not positively identified as
instrumentation counts as substantive.
"""

from __future__ import annotations

import pytest

from opensquilla.tools.patch_classification import (
    is_instrumentation_only_patch,
    iter_patch_line_changes,
)


def _patch(*body_lines: str) -> str:
    """Wrap hunk body lines in a realistic git unified-diff envelope."""

    return "\n".join(
        [
            "diff --git a/pkg.py b/pkg.py",
            "index 1111111..2222222 100644",
            "--- a/pkg.py",
            "+++ b/pkg.py",
            "@@ -1,1 +1,2 @@",
            *body_lines,
            "",
        ]
    )


def test_iter_patch_line_changes_skips_file_headers() -> None:
    added, removed = iter_patch_line_changes(
        _patch(" value = 1", '+print("debug")', "-old = 2")
    )

    # The +++/--- file headers must not surface as content changes.
    assert added == ['print("debug")']
    assert removed == ["old = 2"]


def test_iter_patch_line_changes_empty_patch() -> None:
    assert iter_patch_line_changes("") == ([], [])


def test_empty_patch_is_not_instrumentation_only() -> None:
    assert is_instrumentation_only_patch("") is False
    assert is_instrumentation_only_patch("   \n") is False


def test_any_removed_line_is_substantive() -> None:
    # Deleting a line changes existing behavior even when the deleted line
    # itself is a print — reverting such a diff would restore that behavior.
    assert (
        is_instrumentation_only_patch(_patch(' value = 1', '-print("old debug")'))
        is False
    )
    assert (
        is_instrumentation_only_patch(
            _patch("-value = 1", "+value = 2", '+print("debug")')
        )
        is False
    )


def test_blank_only_additions_are_substantive() -> None:
    # No added content line means nothing positively identified as
    # instrumentation; fail toward keeping protections active.
    assert is_instrumentation_only_patch(_patch("+", "+   ")) is False


def test_added_blank_lines_next_to_prints_are_fine() -> None:
    assert (
        is_instrumentation_only_patch(_patch("+", '+print("debug")', "+"))
        is True
    )


@pytest.mark.parametrize(
    "line",
    [
        'print(f"x={x}")',
        "pprint(state)",
        'sys.stderr.write("dbg\\n")',
        'sys.stdout.write(repr(x))',
        "traceback.print_exc()",
        "traceback.print_stack()",
        'logging.debug("hit")',
        'logger.info("value %s", value)',
        'log.warning("boom")',
        'logger.exception("failed")',
        "console.log('value', value);",
        "console.error(err);",
        'process.stdout.write("x");',
        'fmt.Println("x")',
        'fmt.Printf("%v\\n", x)',
        'fmt.Fprintf(os.Stderr, "%v", x)',
        'log.Printf("x=%v", x)',
        'puts "debug"',
        'println!("x = {}", x);',
        'eprintln!("boom");',
        "dbg!(&state);",
        'System.out.println("x");',
        "e.printStackTrace();",
        "std::cout << x << std::endl;",
        'cerr << "boom";',
        'printf("%d\\n", x);',
        'fprintf(stderr, "x=%d", x);',
        'puts("debug");',
        'perror("open");',
        "var_dump($state);",
        "print_r($arr);",
        'error_log("hit");',
        "$stderr.puts x.inspect",
        "debugger;",
    ],
)
def test_print_and_log_additions_across_ecosystems(line: str) -> None:
    assert is_instrumentation_only_patch(_patch(" value = 1", f"+    {line}")) is True


@pytest.mark.parametrize(
    "line",
    [
        "value = 2",
        "return None",
        "log.SetOutput(io.Discard)",  # Go log config, not log output
        "printer.render()",  # identifier merely starting with "print"
        "sprint(x)",
        "self.logger = logging.getLogger(__name__)",
        "if debug:",
        'raise ValueError("boom")',
    ],
)
def test_substantive_additions(line: str) -> None:
    assert is_instrumentation_only_patch(_patch(f"+{line}")) is False


def test_mixed_addition_is_substantive() -> None:
    assert (
        is_instrumentation_only_patch(_patch('+print("debug")', "+value = 2"))
        is False
    )


def test_multiline_print_call_is_substantive() -> None:
    # Only the first line of a multi-line call matches; the continuation
    # lines fail and classify the patch as substantive — the conservative
    # direction, since we cannot see where the call actually ends.
    assert (
        is_instrumentation_only_patch(_patch("+print(", "+    value,", "+)"))
        is False
    )
