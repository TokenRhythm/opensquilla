"""Historical diagnostics must finish on malformed and unusually large text."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from opensquilla.session.compaction_state import (
    _artifact_names,
    _command_references,
    _file_paths,
    build_coverage_obligations,
)


@pytest.mark.parametrize("case", [
    "unclosed_quote", "extensionless_word", "spaced_paths", "unmatched_fences",
    "artifact_prose", "many_commands",
])
def test_historical_coverage_scan_finishes_in_isolated_process(case: str) -> None:
    # A timeout in the same process cannot interrupt a regex holding the GIL.
    # Use a fresh interpreter so the old implementation fails without hanging
    # pytest, including on platforms whose multiprocessing default is spawn.
    script = textwrap.dedent("""
        import json
        import sys
        from opensquilla.session.compaction_state import build_coverage_obligations

        case = sys.argv[1]
        content = {
            "unclosed_quote": lambda: "'" + "\\\\" * 80,
            "extensionless_word": lambda: "a" * 150_000 + "/leaf",
            "spaced_paths": lambda: " ".join(f"src/cache_{index}" for index in range(10_000)),
            "unmatched_fences": lambda: " ".join("`" * size for size in range(1, 1025)),
            "artifact_prose": lambda: "Artifact: " + "word " * 30_000,
            "many_commands": lambda: "`npm test`\\n" * 15_000,
        }[case]()
        result = build_coverage_obligations([
            {"id": 1, "role": "assistant", "content": content},
            {"id": 2, "role": "user", "content": "Goal: retain the follow-up task."},
            {"id": 3, "role": "assistant", "content": "Open docs/retained.py."},
        ], session_id="synthetic-scan")
        assert any(item.kind == "user_goal" and item.value == "retain the follow-up task."
                   for item in result.obligations)
        assert len(result.manifest["entries"]) <= 64
        print(json.dumps({"case": case, "complete": result.manifest["complete"]}))
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, case], capture_output=True, text=True, timeout=15,
        check=True,
    )
    assert json.loads(result.stdout)["case"] == case


@pytest.mark.parametrize("path", [
    "docs/My  Report.md", r"C:\Program Files\My  App\report.txt", "/srv/My  Cache",
    "../cache/My\tReport.txt", "src/a+b.py",
])
@pytest.mark.parametrize("fence", ["'", '"', "`", "``"])
def test_scan_retains_literal_path_bytes(path: str, fence: str) -> None:
    assert _file_paths(f"Open {fence}{path}{fence} for details.") == [path]


def test_scan_keeps_all_adjacent_paths_without_revisiting_the_suffix() -> None:
    paths = [f"src/module_{index}.py" for index in range(500)]
    assert _file_paths("Open " + " ".join(paths) + " for details.") == paths


def test_scan_keeps_extensionless_paths_and_ignores_closing_markup() -> None:
    assert _file_paths("Open src/cache src/other for details. </previous-summary>") == [
        "src/cache", "src/other",
    ]


@pytest.mark.parametrize("text, expected", [
    (r"+C:\Program Files\report.txt", r"C:\Program Files\report.txt"),
    ("é+src/cache", "src/cache"),
    ("broken//srv/cache", "/srv/cache"),
])
def test_scan_preserves_path_boundaries_after_punctuation(text: str, expected: str) -> None:
    assert _file_paths(text) == [expected]


def test_unmatched_quote_does_not_hide_a_complete_other_quote() -> None:
    assert _file_paths("Unclosed ' prefix; open \"/srv/My  Cache\" now.") == ["/srv/My  Cache"]


def test_fence_index_preserves_command_boundaries_and_shell_backticks() -> None:
    assert _command_references("Use `npm test` then `python -V`.") == ["npm test", "python -V"]
    assert _command_references("Run ``bash -c 'echo `hostname`'`` now.") == [
        "bash -c 'echo `hostname`'",
    ]


def test_path_overflow_stays_incomplete_and_later_goal_survives() -> None:
    result = build_coverage_obligations([
        {"id": 1, "role": "assistant", "content": "a" * 1000 + "/leaf"},
        {"id": 2, "role": "user", "content": "Goal: continue after scanning."},
        {"id": 3, "role": "assistant", "content": "Open docs/retained.py."},
    ], session_id="synthetic-scan")
    assert result.manifest["complete"] is False
    assert "extraction_truncated" in result.manifest["incomplete_reasons"]
    assert any(item["value"] == "docs/retained.py" for item in result.manifest["entries"])
    assert any(item.kind == "user_goal" for item in result.obligations)


@pytest.mark.parametrize("text, expected", [
    ("abc.pdfé", []),
    ("b.pdf.txté", ["b.pdf"]),
    ("abc.pdf全局包 abc.pdf", ["abc.pdf"]),
    ("go test .pdf", []),
    ("go test ..pdf", ["go test ..pdf"]),
    ("final continuity report.pdf.", ["final continuity report.pdf"]),
])
def test_artifact_scan_preserves_original_word_boundaries(text: str, expected: list[str]) -> None:
    assert _artifact_names(text) == expected
