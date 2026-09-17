"""Offline contracts for the artifact-backed paper length gate."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT
    / "src"
    / "opensquilla"
    / "skills"
    / "bundled"
    / "paper-length-gate"
    / "scripts"
    / "audit.py"
)


def _manuscript(*, repeats: int = 100, citations: bool = True) -> str:
    cite = r" \cite{ref1,ref2}" if citations else ""
    paragraphs = "\n".join(
        "This synthetic section explains assumption dimension "
        f"{index}, method boundary {index}, evaluation design choice {index}, "
        "and reproducible limitations without claiming completed empirical results. "
        f"{cite}"
        for index in range(1, repeats + 1)
    )
    return "\n".join(
        [
            r"\documentclass{article}",
            r"\begin{document}",
            r"\begin{abstract}A synthetic readiness fixture.\end{abstract}",
            r"\section{Introduction}",
            paragraphs,
            r"\section{Related Work}",
            paragraphs.splitlines()[0],
            r"\section{Method}",
            paragraphs.splitlines()[1],
            r"\section{Experiments}",
            paragraphs.splitlines()[2],
            r"\section{Discussion}",
            paragraphs.splitlines()[3],
            r"\section{Conclusion}",
            paragraphs.splitlines()[4],
            r"\bibliography{references}",
            r"\end{document}",
        ]
    )


def _payload(path: str, *, pages: str = "8", mode: str = "FULL_MANUSCRIPT") -> dict[str, str]:
    return {
        "paper_contract": f"PAPER_MODE: {mode}\nTARGET_PAGES: {pages}",
        "manuscript_package": (
            f"MANUSCRIPT_PATH: {path}\n"
            "CONTEXT_POLICY: artifact-only; full manuscript omitted from prompt/output"
        ),
    }


def _run(payload: dict[str, str], workspace: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        cwd=workspace,
    )


def test_length_gate_reads_artifact_only_manifest_without_blocking(tmp_path: Path) -> None:
    paper = tmp_path / "paper" / "paper.tex"
    paper.parent.mkdir()
    paper.write_text(_manuscript(repeats=250), encoding="utf-8")

    result = _run(_payload(str(paper), pages="8"), tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "LENGTH_GATE: pass" in result.stdout
    assert "TARGET_PAGES: 8" in result.stdout
    assert "PAGE_COUNT_AUTHORITY: compile_pdf/pypdf" in result.stdout
    assert "REQUIRED_SECTIONS: 7/7" in result.stdout
    assert "DISTINCT_CITE_KEYS: 2" in result.stdout
    assert "CONTEXT_POLICY" not in result.stdout


def test_length_gate_rejects_workspace_escape_and_non_tex_path(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-paper.txt"
    outside.write_text(_manuscript(), encoding="utf-8")

    result = _run(_payload(str(outside)), tmp_path)

    assert result.returncode != 0
    assert "LENGTH_GATE: block" in result.stdout
    assert "escapes the active workspace" in result.stdout
    assert "must reference a .tex file" in result.stdout
    assert "escapes the active workspace" in result.stderr


def test_length_gate_rejects_missing_empty_and_non_tex_artifacts(tmp_path: Path) -> None:
    empty = tmp_path / "empty.tex"
    empty.write_text("", encoding="utf-8")
    not_tex = tmp_path / "paper.md"
    not_tex.write_text(_manuscript(), encoding="utf-8")

    missing_result = _run(_payload("paper/missing.tex"), tmp_path)
    empty_result = _run(_payload(str(empty)), tmp_path)
    non_tex_result = _run(_payload(str(not_tex)), tmp_path)

    assert missing_result.returncode != 0
    assert "cannot be resolved" in missing_result.stdout
    assert empty_result.returncode != 0
    assert "manuscript .tex file is empty" in empty_result.stdout
    assert non_tex_result.returncode != 0
    assert "MANUSCRIPT_PATH must reference a .tex file" in non_tex_result.stdout


def test_length_gate_blocks_missing_sections_small_body_and_citations(tmp_path: Path) -> None:
    paper = tmp_path / "short.tex"
    paper.write_text(
        r"\documentclass{article}\begin{document}\section{Introduction}Tiny.\end{document}",
        encoding="utf-8",
    )

    result = _run(_payload(str(paper), pages="10"), tmp_path)

    assert result.returncode != 0
    assert "required section missing: abstract" in result.stdout
    assert "required section missing: conclusion" in result.stdout
    assert "manuscript contains no LaTeX citation keys" in result.stdout
    assert "manuscript body is below target-correlated readiness floor" in result.stdout


def test_length_gate_rejects_missing_or_out_of_range_page_target(tmp_path: Path) -> None:
    paper = tmp_path / "paper.tex"
    paper.write_text(_manuscript(), encoding="utf-8")

    missing = _run(
        {
            "paper_contract": "PAPER_MODE: FULL_MANUSCRIPT",
            "manuscript_package": f"MANUSCRIPT_PATH: {paper}",
        },
        tmp_path,
    )
    out_of_range = _run(_payload(str(paper), pages="51"), tmp_path)

    assert missing.returncode != 0
    assert "TARGET_PAGES must be a machine-readable integer" in missing.stdout
    assert out_of_range.returncode != 0
    assert "TARGET_PAGES must be between 1 and 50" in out_of_range.stdout


def test_length_gate_rejects_modes_outside_the_public_contract(tmp_path: Path) -> None:
    paper = tmp_path / "paper.tex"
    paper.write_text(_manuscript(), encoding="utf-8")

    result = _run(_payload(str(paper), mode="LEGACY_MODE"), tmp_path)

    assert result.returncode != 0
    assert "PAPER_MODE must be FULL_MANUSCRIPT or COMPACT_SKELETON" in result.stdout


def test_length_gate_keeps_inline_compact_package_compatible(tmp_path: Path) -> None:
    manuscript = _manuscript(repeats=100)
    payload = {
        "paper_contract": "PAPER_MODE: COMPACT_SKELETON\nTARGET_PAGES: 4",
        "manuscript_package": f"MANUSCRIPT_TEX:\n{manuscript}\nREFERENCES_BIB:\n",
    }

    result = _run(payload, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "LENGTH_GATE: warn" in result.stdout
    assert "inline manuscript compatibility path used" in result.stdout
    assert "MANUSCRIPT_SOURCE: inline:MANUSCRIPT_TEX" in result.stdout


def test_length_gate_floor_scales_with_target_and_report_only_exposes_deficit(
    tmp_path: Path,
) -> None:
    paper = tmp_path / "paper.tex"
    # About 2,000 English content units: ready for the default four-page
    # contract, but deterministically too small for five pages.
    paper.write_text(_manuscript(repeats=80), encoding="utf-8")

    four_pages = _run(_payload(str(paper), pages="4", mode="COMPACT_SKELETON"), tmp_path)
    five_page_payload = _payload(str(paper), pages="5", mode="COMPACT_SKELETON")
    five_pages = _run(five_page_payload, tmp_path)
    report_only = _run({**five_page_payload, "report_only": True}, tmp_path)

    assert four_pages.returncode == 0, four_pages.stdout + four_pages.stderr
    assert "MINIMUM_CONTENT_UNITS: 2000" in four_pages.stdout
    assert five_pages.returncode != 0
    assert "MINIMUM_CONTENT_UNITS: 2500" in five_pages.stdout
    assert "below target-correlated readiness floor" in five_pages.stdout
    assert report_only.returncode == 0
    assert "LENGTH_GATE: block" in report_only.stdout
    assert report_only.stderr == ""


def test_length_gate_rejects_repeated_prose_as_page_padding(tmp_path: Path) -> None:
    paper = tmp_path / "paper.tex"
    manuscript = _manuscript(repeats=100)
    unique_line = next(
        line for line in manuscript.splitlines() if "assumption dimension" in line
    )
    padded = re.sub(
        r"This synthetic section explains assumption dimension[\s\S]*?"
        r"(?=\\section\{Related Work\})",
        lambda _match: (unique_line + "\n") * 130,
        manuscript,
    )
    paper.write_text(padded, encoding="utf-8")

    result = _run(_payload(str(paper), pages="4", mode="COMPACT_SKELETON"), tmp_path)

    assert result.returncode != 0
    assert "excessive repeated prose" in result.stdout


def test_length_gate_rejects_conditionally_hidden_content_and_structure(
    tmp_path: Path,
) -> None:
    paper = tmp_path / "paper.tex"
    hidden_units = " ".join(f"hiddenunit{index}" for index in range(2100))
    paper.write_text(
        "\n".join(
            (
                r"\documentclass{article}",
                r"\begin{document}",
                r"\iffalse",
                r"\begin{abstract}Hidden abstract.\end{abstract}",
                r"\section{Introduction}\section{Related Work}\section{Method}",
                r"\section{Experiments}\section{Discussion}\section{Conclusion}",
                hidden_units + r" \cite{ref1}",
                r"\fi",
                "No empirical results were provided. Planned evaluation.",
                r"\end{document}",
            )
        ),
        encoding="utf-8",
    )

    result = _run(_payload(str(paper), pages="4", mode="COMPACT_SKELETON"), tmp_path)

    assert result.returncode != 0
    assert "TeX conditionals that can hide counted prose" in result.stdout
    assert r"\iffalse" in result.stdout


@pytest.mark.parametrize(
    "visibility_control",
    (
        r"\color{white}",
        r"\textcolor{white}{hidden}",
        r"\color{white!100!black}",
        r"\textcolor{black!0}{hidden}",
        r"\color{red!1}",
        r"\textcolor{white!99!red}{hidden}",
        r"\definecolor{paperwhite}{RGB}{255,255,255}\color{paperwhite}",
        r"\color[cmy]{0,0,0}",
        r"\textcolor[hsb]{0,0,1}{hidden}",
        r"\textcolor[Hsb]{240,0,1}{hidden}",
        r"\textcolor[Gray]{15}{hidden}",
        r"\pagecolor{black}\color{black}",
        r"\colorbox{black}{hidden}",
        r"\rowcolor{black}\color{black}",
        r"\rowcolors{1}{black}{black}\color{black}",
        r"\transparent{0}",
        r"\texttransparent{0}{hidden}",
        r"\fontsize{0.1}{0.1}\selectfont",
        r"\fontsize{4}{4}\selectfont",
        r"\fontsize{4pt}{4pt}\selectfont",
        r"\scalebox{0.001}{hidden}",
        r"\scalebox{0.1}{hidden}",
        r"\resizebox{0.1pt}{!}{hidden}",
        r"\resizebox{4pt}{!}{hidden}",
        r"\resizebox{0.1\linewidth}{!}{hidden}",
        r"\resizebox{!}{0.1\textheight}{hidden}",
        r"\resizebox{1sp}{!}{hidden}",
        r"\resizebox{0.1\hsize}{!}{hidden}",
        r"\resizebox{\unknownwidth}{!}{hidden}",
        r"\raisebox{10000pt}[0pt][0pt]{hidden}",
        r"\raisebox{100pt}[0pt][0pt]{hidden}",
        r"\raisebox{\paperheight}[0pt][0pt]{hidden}",
        r"\raisebox{100\height}[0pt][0pt]{hidden}",
        r"\begin{picture}(0,0)\put(10000,0){hidden}\end{picture}",
        r"\typeout{hidden prose is not rendered}",
        r"\begin{lrbox}{\box0}hidden prose\end{lrbox}",
    ),
)
def test_length_gate_rejects_text_visibility_controls(
    tmp_path: Path,
    visibility_control: str,
) -> None:
    paper = tmp_path / "paper.tex"
    manuscript = _manuscript(repeats=100).replace(
        r"\begin{document}",
        "\\begin{document}\n" + visibility_control,
        1,
    )
    paper.write_text(manuscript, encoding="utf-8")

    result = _run(_payload(str(paper), pages="4", mode="COMPACT_SKELETON"), tmp_path)

    assert result.returncode != 0
    assert "commands that can make counted prose invisible" in result.stdout


@pytest.mark.parametrize(
    "formatting_control",
    (
        r"\definecolor{brandblue}{RGB}{10,80,160}\textcolor{brandblue}{visible}",
        r"\textcolor[cmy]{0.2,0,0}{visible}",
        r"\textcolor[hsb]{0,1,1}{visible}",
        r"\textcolor[Gray]{8}{visible}",
        r"\fontsize{10}{12}\selectfont",
        r"\fontsize{10pt}{12pt}\selectfont",
        r"\scalebox{0.9}{visible}",
        r"\scalebox{1}[1]{visible}",
        r"\resizebox{\linewidth}{!}{visible table}",
        r"\resizebox{\hsize}{!}{visible table}",
        r"\raisebox{1ex}{visible}",
        r"\raisebox{-.5\height}{visible}",
        r"\transparent{0.8}",
        r"\texttransparent{0.8}{visible}",
        r"\tiny compact table note\normalsize",
    ),
)
def test_length_gate_allows_ordinary_scholarly_formatting(
    tmp_path: Path,
    formatting_control: str,
) -> None:
    paper = tmp_path / "paper.tex"
    manuscript = _manuscript(repeats=100).replace(
        r"\begin{document}",
        "\\begin{document}\n" + formatting_control,
        1,
    )
    paper.write_text(manuscript, encoding="utf-8")

    result = _run(_payload(str(paper), pages="4", mode="COMPACT_SKELETON"), tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "LENGTH_GATE: pass" in result.stdout
