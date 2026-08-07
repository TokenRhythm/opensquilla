"""Offline contracts for the deterministic paper delivery summary."""

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
    / "paper-delivery-summary"
    / "scripts"
    / "render.py"
)
SYNTHETIC_PDF_PATH = (ROOT / ".test-output" / "paper.pdf").resolve()


def _payload(
    *,
    language: str = "en",
    instruction: str | None = None,
    pages: str = "12",
    target: str = "10",
    summary: str | None = None,
) -> dict[str, str]:
    if instruction is None:
        instruction = (
            "Output language rule: write final user-facing prose in Simplified Chinese."
            if language == "zh"
            else "Output language rule: write final user-facing prose in English only."
        )
    if summary is None:
        summary = (
            "SUMMARY: total_cite_keys=10, strong=8, ok=2, weak=0, "
            "invalid=0, unused=3"
        )
    return {
        "paper_contract": (
            f"PAPER_MODE: FULL_MANUSCRIPT\nLANGUAGE: {language}\nTARGET_PAGES: {target}"
        ),
        "language_instruction": instruction,
        "compile_pdf": (
            f"PDF_PATH: {SYNTHETIC_PDF_PATH}\n"
            f"PDF_PAGES: {pages}\n"
            f"PDF_TARGET_PAGES: {target}\n"
            "PDF_BYTES: 123456"
        ),
        "citation_map": f"CITATION_MAP:\n\n{summary}",
    }


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


def test_delivery_summary_renders_exact_english_machine_counts() -> None:
    result = _run(_payload())

    assert result.returncode == 0, result.stdout + result.stderr
    assert "📄 Paper compiled" in result.stdout
    assert "Pages: 12 (target: at least 10)" in result.stdout
    assert "cited keys 10" in result.stdout
    assert "strong 8" in result.stdout
    assert "acceptable 2" in result.stdout
    assert "weak 0" in result.stdout
    assert "invalid 0" in result.stdout
    assert "unused entries 3" in result.stdout
    assert "3 bibliography entries are not cited" in result.stdout
    assert "论文已生成" not in result.stdout


def test_delivery_summary_renders_exact_chinese_machine_counts() -> None:
    result = _run(_payload(language="zh"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "📄 论文已生成" in result.stdout
    assert "页数: 12（目标至少 10 页）" in result.stdout
    assert "正文引用键 10" in result.stdout
    assert "强来源 8" in result.stdout
    assert "一般来源 2" in result.stdout
    assert "弱来源 0" in result.stdout
    assert "无效 0" in result.stdout
    assert "未使用条目 3" in result.stdout
    assert "参考文献库中有 3 条未在正文引用" in result.stdout
    assert "Paper compiled" not in result.stdout


def test_delivery_summary_has_no_warning_when_all_counts_are_clean() -> None:
    result = _run(
        _payload(
            summary=(
                "SUMMARY: total_cite_keys=10, strong=10, ok=0, weak=0, "
                "invalid=0, unused=0"
            )
        )
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Warnings: none" in result.stdout


def test_delivery_summary_rejects_missing_or_malformed_citation_stats() -> None:
    missing = _run(
        _payload(
            summary=(
                "SUMMARY: total_cite_keys=10, strong=8, ok=2, weak=0, invalid=0"
            )
        )
    )
    malformed = _run(
        _payload(
            summary=(
                "SUMMARY: total_cite_keys=10, strong=many, ok=2, weak=0, "
                "invalid=0, unused=3"
            )
        )
    )
    inconsistent = _run(
        _payload(
            summary=(
                "SUMMARY: total_cite_keys=10, strong=7, ok=2, weak=0, "
                "invalid=0, unused=3"
            )
        )
    )

    assert missing.returncode != 0
    assert "citation_map SUMMARY is missing unused" in missing.stdout
    assert malformed.returncode != 0
    assert "citation_map SUMMARY contains a malformed field" in malformed.stdout
    assert inconsistent.returncode != 0
    assert "classifications do not equal total_cite_keys" in inconsistent.stdout


def test_delivery_summary_rejects_missing_compile_stats() -> None:
    missing_pages_payload = _payload()
    missing_pages_payload["compile_pdf"] = f"PDF_PATH: {SYNTHETIC_PDF_PATH}"
    missing_pages = _run(missing_pages_payload)

    assert missing_pages.returncode != 0
    assert "exactly one PDF_PAGES marker" in missing_pages.stdout


def test_confirmed_chinese_contract_wins_over_stale_english_instruction() -> None:
    result = _run(
        _payload(
            language="zh",
            instruction="Output language rule: write final user-facing prose in English only.",
        )
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "📄 论文已生成" in result.stdout
    assert "页数: 12（目标至少 10 页）" in result.stdout
    assert "Paper compiled" not in result.stdout


def test_language_instruction_is_fallback_when_contract_language_is_missing() -> None:
    payload = _payload(
        instruction="Output language rule: write final user-facing prose in English only."
    )
    payload["paper_contract"] = "PAPER_MODE: FULL_MANUSCRIPT\nTARGET_PAGES: 10"

    result = _run(payload)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "📄 Paper compiled" in result.stdout
