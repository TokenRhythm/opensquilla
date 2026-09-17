"""Offline contracts for the deterministic paper LaTeX sanitizer."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from opensquilla.subprocess_encoding import apply_utf8_child_env

ROOT = Path(__file__).resolve().parents[2]
SANITIZER = (
    ROOT
    / "src"
    / "opensquilla"
    / "skills"
    / "bundled"
    / "paper-latex-sanitizer"
    / "scripts"
    / "sanitize.py"
)
QUALITY_GATE = (
    ROOT
    / "src"
    / "opensquilla"
    / "skills"
    / "bundled"
    / "paper-quality-gate"
    / "scripts"
    / "audit.py"
)


def _run(
    script: Path,
    payload: dict[str, str],
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        cwd=cwd,
        env=apply_utf8_child_env(dict(os.environ)),
    )


def _gate_payload(manifest: str) -> dict[str, str]:
    return {
        "paper_contract": "EVIDENCE_STATUS: not_supplied",
        "length_gate": "LENGTH_GATE: pass\nBLOCKERS:\n- none",
        "citation_gate": "INTEGRITY: pass\nBLOCKERS:\n- none",
        "manuscript_package": manifest,
    }


def _evidence_free_manuscript() -> str:
    return r"""
\documentclass{article}
\begin{document}
\title{联邦学习——计划评估}
\begin{abstract}
尚无实验结果，所有评估均为计划，结果值待实验确定。
\end{abstract}
\section{Introduction}
该机制能够显著降低通信开销，同时将模型精度损失控制在可接受范围内，
预期不超过1\%。
\section{实验}
实验设置使用客户端总数\(K=100\)、50轮训练，并令隐私预算
\(\varepsilon\)分别取2、4、8。
假设H3——动态噪声方案可使模型精度下降控制在5\%以内；具体结果待实验确定。
\input{figure_placeholder_template}
\begin{figure}
\caption{假设H3：在\(\varepsilon=4\)时精度下降小于5\%；待实验验证。}
\end{figure}
\input{table_placeholder_template.tex}
\begin{table}
\caption{计划评估占位：结果值待实验确定。}
\end{table}
\input{user_appendix}
比较C1–C5—baseline配置。
\section{讨论}
我们预期最终测试精度可能提升2\%–4\%，具体结果待实验确定。
去除自适应量化后，通信开销可能增加40\%–50\%，结果值待实验确定。
预计收敛轮数将从100轮降至80轮，结果值待实验确定。
综合效率指标方面，本文方法较基线降低约60\%–70\%，结果值待实验确定。
\section{结论}
实验结果尚未提供，评估计划将在未来执行。
\end{document}
""".strip()


def test_sanitizer_atomically_repairs_persisted_manuscript_before_strict_gate(
    tmp_path: Path,
) -> None:
    manuscript_path = tmp_path / "paper.tex"
    references_path = tmp_path / "references.bib"
    manuscript_path.write_text(_evidence_free_manuscript(), encoding="utf-8")
    references_path.write_text("% no verified references", encoding="utf-8")
    manifest = (
        f"MANUSCRIPT_PATH: {manuscript_path}\n"
        f"REFERENCES_PATH: {references_path}\n"
    )

    result = _run(
        SANITIZER,
        {
            "paper_contract": "EVIDENCE_STATUS: not_supplied",
            "user_request": "写一篇计划评估论文，不提供实验数据或结果数值。",
            "manuscript_package": manifest,
        },
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SANITIZER: pass" in result.stdout
    assert f"MANUSCRIPT_PATH: {manuscript_path}" in result.stdout
    repaired = manuscript_path.read_text(encoding="utf-8")
    assert "K=100" in repaired
    assert "50轮训练" in repaired
    assert r"\varepsilon\)分别取2、4、8" in repaired
    assert "——" in repaired
    assert "C1--C5---baseline" in repaired
    assert "–" not in repaired
    assert "—baseline" not in repaired
    assert r"\input{figure_placeholder_template}" not in repaired
    assert r"\input{table_placeholder_template.tex}" not in repaired
    assert r"\input{user_appendix}" in repaired
    assert "REDUNDANT_PLACEHOLDER_INPUT_REPAIRS: 2" in result.stdout
    for invented in (
        r"1\%",
        r"5\%",
        r"2\%",
        r"4\%",
        r"40\%",
        r"50\%",
        r"60\%",
        r"70\%",
        "80轮",
    ):
        assert invented not in repaired
    assert repaired.count("待实验确定") >= 5

    gate = _run(QUALITY_GATE, _gate_payload(result.stdout))

    assert gate.returncode == 0, gate.stdout + gate.stderr
    assert "QUALITY_GATE: pass" in gate.stdout


def test_sanitizer_does_not_turn_strict_gate_into_a_semantic_bypass(tmp_path: Path) -> None:
    manuscript_path = tmp_path / "paper.tex"
    manuscript_path.write_text(
        r"""
\documentclass{article}
\begin{document}
\begin{abstract}
尚无实验结果，所有评估均为计划，结果值待实验确定。
\end{abstract}
\section{讨论}
结果表明本文方法优于全部基线。
\section{结论}
实验结果尚未提供，评估计划将在未来执行。
\end{document}
""".strip(),
        encoding="utf-8",
    )
    manifest = f"MANUSCRIPT_PATH: {manuscript_path}\n"

    sanitized = _run(
        SANITIZER,
        {
            "paper_contract": "EVIDENCE_STATUS: not_supplied",
            "user_request": "没有实验数据。",
            "manuscript_package": manifest,
        },
        cwd=tmp_path,
    )
    assert sanitized.returncode == 0, sanitized.stdout + sanitized.stderr
    assert "结果表明本文方法优于全部基线" in manuscript_path.read_text(encoding="utf-8")

    gate = _run(QUALITY_GATE, _gate_payload(sanitized.stdout))

    assert gate.returncode != 0
    assert "将实验结果表述为既成事实" in gate.stdout


def test_invented_target_or_hypothesis_magnitude_is_not_preserved() -> None:
    manuscript = r"""
\documentclass{article}
\begin{document}
\begin{abstract}No empirical results were supplied; evaluation is planned.\end{abstract}
\section{Experiments}
目标精度提升5\%；假设H3下降小于5\%。结果值待实验确定。
\section{Conclusion}
No empirical results were supplied and all outcomes remain TBD.
\end{document}
""".strip()

    result = _run(
        SANITIZER,
        {
            "paper_contract": "EVIDENCE_STATUS: not_supplied",
            "user_request": "No empirical data or result targets were supplied.",
            "manuscript_package": (
                f"MANUSCRIPT_TEX:\n{manuscript}\n"
                "REFERENCES_BIB:\n% no verified references\n"
            ),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert r"5\%" not in result.stdout
    assert result.stdout.count("待实验确定") >= 3


def test_explicit_user_decision_threshold_and_setup_values_are_preserved() -> None:
    manuscript = r"""
\documentclass{article}
\begin{document}
\begin{abstract}
No empirical results were supplied; the planned evaluation remains pending.
\end{abstract}
\section{Experiments}
The setup uses 100 clients for 50 rounds. The target accuracy is 80\% and is
defined as the preregistered decision threshold. All outcomes remain TBD.
\section{Conclusion}
Evaluation is planned and empirical results are not yet available.
\end{document}
""".strip()

    result = _run(
        SANITIZER,
        {
            "paper_contract": "EVIDENCE_STATUS: not_supplied",
            "user_request": "Use 80% accuracy as the preregistered decision threshold.",
            "manuscript_package": (
                f"MANUSCRIPT_TEX:\n{manuscript}\n"
                "REFERENCES_BIB:\n% no verified references\n"
            ),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "100 clients" in result.stdout
    assert "50 rounds" in result.stdout
    assert r"80\%" in result.stdout


def test_missing_generated_artifact_is_blocked() -> None:
    result = _run(
        SANITIZER,
        {
            "paper_contract": "PAPER_MODE: COMPACT_SKELETON\nEVIDENCE_STATUS: not_supplied",
            "manuscript_package": "",
        },
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "SANITIZER: block" in result.stdout


def test_nonredundant_generated_placeholder_input_is_not_removed(tmp_path: Path) -> None:
    manuscript_path = tmp_path / "paper.tex"
    manuscript_path.write_text(
        r"""\documentclass{article}
\begin{document}
\input{figure_placeholder_template}
\section{Conclusion}
No empirical results were supplied; evaluation is planned.
\end{document}
""",
        encoding="utf-8",
    )

    result = _run(
        SANITIZER,
        {
            "paper_contract": "EVIDENCE_STATUS: not_supplied",
            "manuscript_package": f"MANUSCRIPT_PATH: {manuscript_path}\n",
        },
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert r"\input{figure_placeholder_template}" in manuscript_path.read_text(
        encoding="utf-8"
    )
    assert "REDUNDANT_PLACEHOLDER_INPUT_REPAIRS: 0" in result.stdout


def test_generated_manifest_cannot_rewrite_a_file_outside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.tex"
    original = "outside content must remain unchanged"
    outside.write_text(original, encoding="utf-8")

    result = _run(
        SANITIZER,
        {
            "paper_contract": "EVIDENCE_STATUS: not_supplied",
            "manuscript_package": f"MANUSCRIPT_PATH: {outside}\n",
        },
        cwd=workspace,
    )

    assert result.returncode != 0
    assert "SANITIZER: block" in result.stdout
    assert "escapes the skill workspace" in result.stdout
    assert str(outside) not in result.stdout
    assert outside.read_text(encoding="utf-8") == original


def test_generated_manifest_cannot_forward_references_outside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manuscript = workspace / "paper.tex"
    manuscript.write_text(
        "\\documentclass{article}\\begin{document}safe\\end{document}",
        encoding="utf-8",
    )
    outside_references = tmp_path / "private.bib"
    outside_references.write_text(
        "@misc{private, title={Must not be forwarded}}",
        encoding="utf-8",
    )

    result = _run(
        SANITIZER,
        {
            "paper_contract": "EVIDENCE_STATUS: supplied",
            "manuscript_package": (
                f"MANUSCRIPT_PATH: {manuscript}\n"
                f"REFERENCES_PATH: {outside_references}\n"
            ),
        },
        cwd=workspace,
    )

    assert result.returncode != 0
    assert "references path escapes the skill workspace" in result.stdout
    assert str(outside_references) not in result.stdout
    assert "REFERENCES_PATH:" not in result.stdout


def test_current_run_manifest_cannot_rewrite_another_paper_run(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    run_a = workspace / "paper" / "run-a"
    run_b = workspace / "paper" / "run-b"
    run_a.mkdir(parents=True)
    run_b.mkdir(parents=True)
    own = run_a / "paper.tex"
    other = run_b / "paper.tex"
    own.write_text(_evidence_free_manuscript(), encoding="utf-8")
    other_original = "other run must remain unchanged"
    other.write_text(other_original, encoding="utf-8")

    rejected = _run(
        SANITIZER,
        {
            "paper_contract": "EVIDENCE_STATUS: not_supplied",
            "meta_run_id": "run-a",
            "manuscript_package": f"MANUSCRIPT_PATH: {other}\n",
        },
        cwd=workspace,
    )

    assert rejected.returncode == 2
    assert "does not belong to this meta-skill run" in rejected.stdout
    assert other.read_text(encoding="utf-8") == other_original

    accepted = _run(
        SANITIZER,
        {
            "paper_contract": "EVIDENCE_STATUS: not_supplied",
            "meta_run_id": "run-a",
            "manuscript_package": f"MANUSCRIPT_PATH: {own}\n",
        },
        cwd=workspace,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert f"MANUSCRIPT_PATH: {own}" in accepted.stdout


@pytest.mark.parametrize("link_level", ["paper-root", "run-directory"])
def test_sanitizer_rejects_symlinked_run_ancestors(
    tmp_path: Path,
    link_level: str,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    paper_root = workspace / "paper"
    try:
        if link_level == "paper-root":
            (outside / "run-a").mkdir()
            paper_root.symlink_to(outside, target_is_directory=True)
            target = outside / "run-a" / "paper.tex"
        else:
            paper_root.mkdir()
            (paper_root / "run-a").symlink_to(outside, target_is_directory=True)
            target = outside / "paper.tex"
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    original = "outside run content must remain unchanged"
    target.write_text(original, encoding="utf-8")

    result = _run(
        SANITIZER,
        {
            "paper_contract": "EVIDENCE_STATUS: not_supplied",
            "meta_run_id": "run-a",
            "manuscript_package": (
                f"MANUSCRIPT_PATH: {workspace / 'paper' / 'run-a' / 'paper.tex'}\n"
            ),
        },
        cwd=workspace,
    )

    assert result.returncode == 2
    assert "must not be a symlink" in result.stdout
    assert target.read_text(encoding="utf-8") == original


def test_sanitizer_rejects_symlinked_manuscript_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    run_dir = workspace / "paper" / "run-a"
    run_dir.mkdir(parents=True)
    outside = tmp_path / "outside.tex"
    original = "outside manuscript must remain unchanged"
    outside.write_text(original, encoding="utf-8")
    manuscript = run_dir / "paper.tex"
    try:
        manuscript.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    result = _run(
        SANITIZER,
        {
            "paper_contract": "EVIDENCE_STATUS: not_supplied",
            "meta_run_id": "run-a",
            "manuscript_package": f"MANUSCRIPT_PATH: {manuscript}\n",
        },
        cwd=workspace,
    )

    assert result.returncode == 2
    assert "manuscript path must not be a symlink" in result.stdout
    assert outside.read_text(encoding="utf-8") == original


def test_sanitizer_rejects_invalid_runtime_owned_run_id_even_for_inline_package() -> None:
    result = _run(
        SANITIZER,
        {
            "paper_contract": "EVIDENCE_STATUS: not_supplied",
            "meta_run_id": "../escape",
            "manuscript_package": (
                "MANUSCRIPT_TEX:\n"
                "\\documentclass{article}\\begin{document}safe\\end{document}\n"
                "REFERENCES_BIB:\n% none\n"
            ),
        },
    )

    assert result.returncode == 2
    assert "invalid runtime-owned meta_run_id" in result.stdout
