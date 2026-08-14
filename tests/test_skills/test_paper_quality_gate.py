"""Offline contract tests for the deterministic paper publication gate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from opensquilla.subprocess_encoding import apply_utf8_child_env

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT
    / "src"
    / "opensquilla"
    / "skills"
    / "bundled"
    / "paper-quality-gate"
    / "scripts"
    / "audit.py"
)


def _run(payload: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        env=apply_utf8_child_env(dict(os.environ)),
    )


def _payload(manuscript: str) -> dict[str, str]:
    return {
        "paper_contract": "EVIDENCE_STATUS: not_supplied",
        "length_gate": "LENGTH_GATE: pass\nBLOCKERS:\n- none",
        "citation_gate": "INTEGRITY: warn\nBLOCKERS:\n- none",
        "manuscript_package": f"MANUSCRIPT_TEX:\n{manuscript}\nREFERENCES_BIB:\n",
    }


def test_quality_gate_accepts_disclosed_planned_evaluation() -> None:
    manuscript = r"""
\documentclass{article}
\begin{document}
\begin{abstract}
No empirical results were supplied; the planned evaluation will test the hypothesis.
\end{abstract}
\section{Experiments}
No empirical results were supplied; this section specifies the planned evaluation.
The method will be evaluated against the registered baselines.
Values remain \textless TBD\textgreater.
\section{Conclusion}
Evaluation is planned and empirical results are not yet available.
\end{document}
"""

    result = _run(_payload(manuscript))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "QUALITY_GATE: pass" in result.stdout


def test_quality_gate_blocks_upstream_length_verdict() -> None:
    payload = _payload(
        r"\documentclass{article}\begin{document}No empirical results were supplied.\end{document}"
    )
    payload["length_gate"] = "LENGTH_GATE: block\nBLOCKERS:\n- conclusion missing"

    result = _run(payload)

    assert result.returncode != 0
    assert "length gate blocked compilation" in result.stdout


def test_quality_gate_blocks_fabricated_empirical_findings() -> None:
    manuscript = r"""
\documentclass{article}
\begin{document}
\begin{abstract}
No empirical results were supplied. Our study demonstrated a strong effect.
\end{abstract}
\section{Results}
The results show that the method outperformed the baseline by 18.4\% (p < 0.01; Cohen's d = 0.63).
\section{Conclusion}
This work provides the first experimental evidence for the approach.
\end{document}
"""

    result = _run(_payload(manuscript))

    assert result.returncode != 0
    assert "QUALITY_GATE: block" in result.stdout
    assert "reported significance statistic" in result.stdout
    assert "results presented as observed" in result.stdout


def test_quality_gate_blocks_concrete_chinese_predicted_result_numbers() -> None:
    manuscript = r"""
\documentclass{article}
\begin{document}
\begin{abstract}
尚无实验结果，所有评估均为计划，结果值待实验确定。
\end{abstract}
\section{讨论}
我们预期任务完成率将比最佳基线高出至少 12\%，端到端延迟降低约 25\%。
在严格约束下，该方法仍有望保持 80\% 以上的任务完成率。
\section{结论}
实验结果尚未提供，评估计划将在未来执行。
\end{document}
"""

    result = _run(_payload(manuscript))

    assert result.returncode != 0
    assert "QUALITY_GATE: block" in result.stdout
    assert "无证据的具体预测结果数字" in result.stdout
    assert "12\\%" in result.stdout


def test_quality_gate_blocks_own_method_forecast_outside_results_sections() -> None:
    manuscript = r"""
\documentclass{article}
\begin{document}
\begin{abstract}
尚无实验结果，所有评估均为计划，结果值待实验确定。
\end{abstract}
\section{Introduction}
该机制能够显著降低通信开销，同时将模型精度损失控制在可接受范围内，
预期不超过1\%。
\section{结论}
实验结果尚未提供，评估计划将在未来执行。
\end{document}
"""

    result = _run(_payload(manuscript))

    assert result.returncode != 0
    assert "own-method numeric forecast without evidence" in result.stdout
    assert r"1\%" in result.stdout


def test_quality_gate_blocks_categorical_own_method_numeric_change() -> None:
    manuscript = r"""
\documentclass{article}
\begin{document}
\begin{abstract}
尚无实验结果，所有评估均为计划，结果值待实验确定。
\end{abstract}
\section{讨论}
综合效率指标方面，本文方法较基线降低约60\%--70\%。
\section{结论}
实验结果尚未提供，评估计划将在未来执行。
\end{document}
"""

    result = _run(_payload(manuscript))

    assert result.returncode != 0
    assert "own-method numeric forecast without evidence" in result.stdout
    assert r"60\%" in result.stdout


def test_quality_gate_blocks_concrete_english_predicted_result_numbers() -> None:
    manuscript = r"""
\documentclass{article}
\begin{document}
\begin{abstract}
No empirical results were supplied; the planned evaluation remains pending.
\end{abstract}
\section{Discussion}
We expect accuracy to improve by 12\% and latency to drop by 25\%.
\section{Conclusion}
All result values remain TBD until the planned evaluation is complete.
\end{document}
"""

    result = _run(_payload(manuscript))

    assert result.returncode != 0
    assert "predicted numeric result without evidence" in result.stdout


def test_quality_gate_allows_numeric_setup_parameters_and_tbd_outcomes() -> None:
    manuscript = r"""
\documentclass{article}
\begin{document}
\begin{abstract}
No empirical results were supplied; the planned evaluation remains pending.
\end{abstract}
\section{Experiments}
The planned setup will use 30 agents, an arrival rate of 2.0 tasks/s,
a 25\% energy budget, 50 training epochs, and three registered baselines.
The planned analysis will use p < 0.05 as the significance threshold.
Completion-rate improvement is \textless TBD\textgreater and latency reduction
is待实验确定. No empirical results were supplied.
\section{Conclusion}
Evaluation is planned and empirical results are not yet available.
\end{document}
"""

    result = _run(_payload(manuscript))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "QUALITY_GATE: pass" in result.stdout


def test_quality_gate_blocks_categorical_chinese_result_captions_without_evidence() -> None:
    manuscript = r"""
\documentclass{article}
\begin{document}
\begin{abstract}
尚无实验结果，所有评估均为计划，结果值待实验确定。
\end{abstract}
\section{实验}
\begin{figure}
\caption{不同隐私预算下的通信成本与精度。所提方法以更低通信成本达到更高精度。}
\end{figure}
\begin{figure}
\caption{消融收敛曲线。消融变体收敛速度显著慢于完整方法，验证自适应组件的作用。}
\end{figure}
\begin{table}
\caption{性能汇总。所提方法保持最低通信成本与最高精度。}
\end{table}
所有数值结果均待实验确定。
\section{结论}
实验结果尚未提供，评估计划将在未来执行。
\end{document}
"""

    result = _run(_payload(manuscript))

    assert result.returncode != 0
    assert result.stdout.count("categorical observed result claim in evidence-free caption") == 3
    assert "所提方法以更低通信成本达到更高精度" in result.stdout
    assert "消融变体收敛速度显著慢于完整方法" in result.stdout
    assert "所提方法保持最低通信成本与最高精度" in result.stdout


def test_quality_gate_blocks_categorical_english_result_caption_without_evidence() -> None:
    manuscript = r"""
\documentclass{article}
\begin{document}
\begin{abstract}
No empirical results were supplied; the planned evaluation remains pending.
\end{abstract}
\section{Results}
\begin{figure}
\caption{Planned evaluation placeholder: the proposed method achieves lower
communication cost and higher accuracy than every baseline.}
\end{figure}
All values remain TBD until the planned evaluation is executed.
\section{Conclusion}
Evaluation is planned and empirical results are not yet available.
\end{document}
"""

    result = _run(_payload(manuscript))

    assert result.returncode != 0
    assert "categorical observed result claim in evidence-free caption" in result.stdout
    assert "proposed method achieves lower" in result.stdout


def test_quality_gate_allows_observed_caption_when_evidence_is_supplied() -> None:
    manuscript = r"""
\documentclass{article}
\begin{document}
\section{Results}
\begin{figure}
\caption{The proposed method achieves lower cost and higher accuracy than the baseline.}
\end{figure}
\end{document}
"""
    payload = _payload(manuscript)
    payload["paper_contract"] = "EVIDENCE_STATUS: supplied"

    result = _run(payload)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "QUALITY_GATE: pass" in result.stdout


def test_quality_gate_allows_explicitly_planned_chinese_caption_hypotheses() -> None:
    manuscript = r"""
\documentclass{article}
\begin{document}
\begin{abstract}
尚无实验结果，所有评估均为计划，结果值待实验确定。
\end{abstract}
\section{实验}
\begin{figure}
\caption{计划评估占位：100 个客户端、50 轮训练下的通信成本与精度。}
\end{figure}
\begin{table}
\caption{假设 H1：所提方法将以更低通信成本达到更高精度；该假设待实验验证。}
\end{table}
所有结果值均待实验确定。
\section{结论}
实验结果尚未提供，评估计划将在未来执行。
\end{document}
"""

    result = _run(_payload(manuscript))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "QUALITY_GATE: pass" in result.stdout


def test_quality_gate_allows_explicitly_planned_english_caption_hypotheses() -> None:
    manuscript = r"""
\documentclass{article}
\begin{document}
\begin{abstract}
No empirical results were supplied; the planned evaluation remains pending.
\end{abstract}
\section{Experiments}
\begin{figure}
\caption{Planned evaluation placeholder: cost and accuracy for 100 clients over
50 rounds.}
\end{figure}
\begin{table}
\caption{Hypothesis H1: the proposed method will achieve lower communication cost
and higher accuracy; all values remain TBD.}
\end{table}
\section{Conclusion}
Evaluation is planned and empirical results are not yet available.
\end{document}
"""

    result = _run(_payload(manuscript))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "QUALITY_GATE: pass" in result.stdout


def test_quality_gate_allows_conditional_claim_metric_threshold_and_chinese_dash() -> None:
    manuscript = r"""
\documentclass{article}
\begin{document}
\title{通信效率与隐私权衡——一种计划评估}
\begin{abstract}
尚无实验结果，所有评估均为计划，结果值待实验确定。
\end{abstract}
\section{讨论}
这些贡献若经实验证实，将为后续研究提供依据。
我们定义收敛至 50\% 准确率所需轮次为效率指标。
所提方法有望在较少轮次内达到 50\% 准确率；具体收敛轮次待实验确定。
消融变体可能无法稳定达到 50\% 准确率；该假设待实验验证。
CIFAR‑10 上的设置使用 100 个客户端和 50 轮训练。
\section{结论}
实验结果尚未提供，评估计划将在未来执行。
\end{document}
"""

    result = _run(_payload(manuscript))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "QUALITY_GATE: pass" in result.stdout


def test_quality_gate_blocks_literal_unicode_math_glyphs_and_dashes() -> None:
    manuscript = r"""
\documentclass{article}
\begin{document}
\begin{abstract}
No empirical results were supplied; the planned evaluation remains pending.
\end{abstract}
\section{Experiments}
The planned table varies ε with δ fixed and compares C1–C5—baseline.
No empirical results were supplied and all outcomes remain TBD.
\section{Conclusion}
Evaluation is planned and empirical results are not yet available.
\end{document}
"""

    result = _run(_payload(manuscript))

    assert result.returncode != 0
    assert "literal Unicode Greek math glyphs" in result.stdout
    assert "ε (U+03B5)" in result.stdout
    assert "δ (U+03B4)" in result.stdout
    assert "Unicode en/em dashes" in result.stdout
    assert "– (U+2013)" in result.stdout
    assert "— (U+2014)" in result.stdout


def test_quality_gate_accepts_latex_math_macros_and_range_punctuation() -> None:
    manuscript = r"""
\documentclass{article}
\begin{document}
\begin{abstract}
No empirical results were supplied; the planned evaluation remains pending.
\end{abstract}
\section{Experiments}
The planned table varies \(\varepsilon\) with \(\delta\) fixed and compares C1--C5.
No empirical results were supplied and all outcomes remain TBD.
\section{Conclusion}
Evaluation is planned and empirical results are not yet available.
\end{document}
"""

    result = _run(_payload(manuscript))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "QUALITY_GATE: pass" in result.stdout
