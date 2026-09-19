from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

from opensquilla.tools.source_edit_contract import (
    SourceEditContractError,
    apply_line_edits,
    build_diff_summary,
    build_line_receipt,
    source_revision_for_path,
    workspace_file_reference,
)


def test_source_revision_changes_when_file_content_changes(tmp_path: Path) -> None:
    path = tmp_path / "src.py"
    path.write_text("alpha\n", encoding="utf-8")
    first = source_revision_for_path(path)

    path.write_text("beta\n", encoding="utf-8")
    second = source_revision_for_path(path)

    assert first.startswith("file_")
    assert second.startswith("file_")
    assert first != second


def test_build_line_receipt_returns_plain_lines_without_read_file_prefixes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "src.py"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")

    receipt = build_line_receipt(path, start_line=2, end_line=3, display_path="src.py")

    assert receipt["status"] == "success"
    assert receipt["path"] == "src.py"
    assert receipt["range"] == [2, 3]
    assert receipt["lines"] == [
        {"line": 2, "text": "two"},
        {"line": 3, "text": "three"},
    ]
    assert receipt["revision"].startswith("file_")
    assert receipt["reference"] == {
        "version": 1,
        "kind": "workspace_file",
        "id": "src.py",
        "label": "src.py:2-3",
        "scope": {},
        "locator": {"relativePath": "src.py", "startLine": 2, "endLine": 3},
        "state": {"available": True, "revision": receipt["revision"]},
        "capabilities": {"open": True, "copy": True, "reveal": False},
    }


def test_apply_line_edits_replaces_inclusive_ranges_atomically() -> None:
    original = "a\nb\nc\nd\n"

    updated = apply_line_edits(
        original,
        [{"start_line": 2, "end_line": 3, "replacement": "B\nC\n"}],
    )

    assert updated == "a\nB\nC\nd\n"


@pytest.mark.parametrize("display_path", [
    " source.py", "source.py ", "\tsource.py", "source.py\n", r"src\source.py",
])
def test_line_receipt_does_not_retarget_unsupported_path_characters(
    tmp_path: Path, display_path: str,
) -> None:
    path = tmp_path / "source.py"
    path.write_text("one\n", encoding="utf-8")

    receipt = build_line_receipt(path, start_line=1, end_line=1, display_path=display_path)

    assert receipt["path"] == display_path
    assert receipt["lines"] == [{"line": 1, "text": "one"}]
    assert "reference" not in receipt


def test_windows_path_converted_by_caller_retains_exact_reference_identity() -> None:
    workspace = PureWindowsPath(r"C:\project")
    path = workspace / "src" / "new file.py"
    relative_path = path.relative_to(workspace).as_posix()

    reference = workspace_file_reference(
        relative_path, revision="file_1234567890abcdef", start_line=1, end_line=1,
    )

    assert reference is not None
    assert reference["id"] == "src/new file.py"
    assert reference["locator"]["relativePath"] == "src/new file.py"


def test_apply_line_edits_rejects_overlapping_ranges() -> None:
    with pytest.raises(SourceEditContractError, match="overlap"):
        apply_line_edits(
            "a\nb\nc\n",
            [
                {"start_line": 1, "end_line": 2, "replacement": "x\n"},
                {"start_line": 2, "end_line": 3, "replacement": "y\n"},
            ],
        )


def test_build_diff_summary_uses_readable_unified_diff_headers() -> None:
    summary = build_diff_summary("a\nb\n", "a\nB\n", path="src/app.py")

    assert summary.startswith("--- a/src/app.py\n+++ b/src/app.py\n@@")
    assert "\n-b\n+B\n" in summary
