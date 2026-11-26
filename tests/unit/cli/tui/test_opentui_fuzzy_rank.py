from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from opensquilla.cli.tui.opentui.completion import fuzzy_filter, fuzzy_rank


def _bun_executable() -> str | None:
    """Resolve a bun binary subprocess can actually execute.

    On Windows npm installs bun as a ``.ps1``/``.cmd`` shim that ``subprocess``
    cannot launch directly; prefer the real ``bun.exe`` shipped beside the shim.
    """
    bun = shutil.which("bun")
    if bun is None:
        return None
    if os.name == "nt":
        candidate = Path(bun).parent / "node_modules" / "bun" / "bin" / "bun.exe"
        if candidate.is_file():
            return str(candidate)
        exe = shutil.which("bun.exe")
        if exe:
            return exe
    return bun

_COMPOSER_MJS = (
    Path(__file__).resolve().parents[4]
    / "src/opensquilla/cli/tui/opentui/package/src/composer.mjs"
)

# Shared query/candidate fixtures fed to BOTH the Python scorer and the JS
# host's fuzzyScore (via filterCatalog). The host ranks its local snapshot
# first and the Python response then replaces the menu, so any ordering drift
# between the two scorers shows up as a visible reorder flicker.
_PARITY_FIXTURES: list[tuple[str, list[str]]] = [
    ("x", ["gate.txt", "late.txt", "index/tests"]),
    ("m", ["make.go", "map/beta.txt", "src/domain.py"]),
    ("cmp", ["compress", "compact", "compare", "model"]),
    ("re", ["compact", "resume", "reset", "render"]),
    ("ma", ["src/domain.py", "src/cli/main.py", "docs/manual.md", "src/tui/messages.py"]),
    ("mod", ["models.py", "/model", "/mode-off"]),
    ("co", ["/compact", "/cost", "Cost", "config.py"]),
    ("main", ["src/main.py", "main.py", "domain/main_window.py", "remains.txt"]),
    ("", ["b.txt", "a.txt"]),
    ("zz", ["alpha", "beta"]),
]


def test_fuzzy_filter_empty_query_preserves_input_order() -> None:
    items = ["resume", "compact", "reset"]

    assert fuzzy_filter("", items) == items


def test_fuzzy_filter_is_case_insensitive() -> None:
    assert fuzzy_filter("CMp", ["Reset", "COMPACT", "models"]) == ["COMPACT"]


def test_fuzzy_filter_ranks_compact_before_other_cmp_matches() -> None:
    ranked = fuzzy_filter("cmp", ["compress", "compact", "compare", "model"])

    assert ranked[0] == "compact"
    assert "compact" in ranked
    assert "model" not in ranked


def test_fuzzy_filter_matches_reset_and_resume_for_re() -> None:
    ranked = fuzzy_filter("re", ["compact", "resume", "reset", "render"])

    assert ranked[:2] == ["reset", "resume"]
    assert "compact" not in ranked


def test_fuzzy_filter_prefers_segment_start_and_early_matches() -> None:
    ranked = fuzzy_filter(
        "ma",
        [
            "src/domain.py",
            "src/cli/main.py",
            "docs/manual.md",
            "src/tui/messages.py",
        ],
    )

    assert ranked[:2] == ["src/cli/main.py", "docs/manual.md"]


def test_fuzzy_filter_gives_slash_commands_a_command_segment_bonus() -> None:
    # Mirrors the JS scorer's +90 command-segment bonus: a query naming the
    # command (without the slash) ranks the command above plain-file matches.
    ranked = fuzzy_filter("mod", ["models.py", "/model"])

    assert ranked[0] == "/model"


def test_fuzzy_rank_returns_candidate_indexes_and_scores() -> None:
    ranked = fuzzy_rank("re", ["compact", "reset", "resume"])

    assert [index for index, _score in ranked] == [1, 2]
    assert ranked[0][1] >= ranked[1][1]


@pytest.mark.skipif(_bun_executable() is None, reason="bun runtime is not on PATH")
def test_fuzzy_rank_order_matches_js_filter_catalog(tmp_path: Path) -> None:
    script = tmp_path / "parity.mjs"
    script.write_text(
        f'import {{ filterCatalog }} from {json.dumps(_COMPOSER_MJS.as_uri())};\n'
        "const fixtures = JSON.parse(process.argv[2]);\n"
        "const out = fixtures.map(([query, candidates]) =>\n"
        "  filterCatalog(candidates.map((label) => ({ label })), query)\n"
        "    .map((item) => item.label));\n"
        "console.log(JSON.stringify(out));\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [_bun_executable(), str(script), json.dumps(_PARITY_FIXTURES)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    js_orders = json.loads(result.stdout)

    for (query, candidates), js_order in zip(_PARITY_FIXTURES, js_orders, strict=True):
        python_order = fuzzy_filter(query, candidates)
        assert python_order == js_order, (
            f"scorer drift for query {query!r}: python={python_order} js={js_order}"
        )
