"""Offline contracts for deterministic paper citation integrity."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT
    / "src"
    / "opensquilla"
    / "skills"
    / "bundled"
    / "paper-citation-integrity-gate"
    / "scripts"
    / "audit.py"
)


def _payload(
    *,
    target: int = 15,
    total: int = 15,
    strong: int = 15,
    ok: int = 0,
    weak: int = 0,
    invalid: int = 0,
    unused: int = 0,
) -> dict[str, str]:
    return {
        "paper_contract": f"PAPER_MODE: FULL_MANUSCRIPT\nCITATION_TARGET: {target}",
        "paper_preferences": f"CITATION_TARGET: {target}",
        "citation_map": (
            "CITATION_MAP:\n| Cite Key | Cited Times |\n"
            f"SUMMARY: total_cite_keys={total}, strong={strong}, ok={ok}, "
            f"weak={weak}, invalid={invalid}, unused={unused}"
        ),
    }


def _run(payload: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def test_citation_gate_blocks_real_fourteen_of_fifteen_case() -> None:
    result = _run(_payload(total=14, strong=14, unused=33))

    assert result.returncode != 0
    assert "INTEGRITY: block" in result.stdout
    assert "TOTAL_CITE_KEYS: 14" in result.stdout
    assert "citation coverage insufficient: found 14/15 cited keys" in result.stdout
    assert "found 14/15 cited keys" in result.stderr


def test_citation_gate_passes_fifteen_of_fifteen_and_warns_unused() -> None:
    result = _run(_payload(total=15, strong=14, ok=1, unused=7))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "INTEGRITY: pass" in result.stdout
    assert "CITATION_TARGET: 15" in result.stdout
    assert "INVALID_COUNT: 0" in result.stdout
    assert "WEAK_PRIMARY_COUNT: 0" in result.stdout
    assert "bibliography contains 7 unused entries" in result.stdout


def test_citation_gate_blocks_invalid_or_weak_cited_keys() -> None:
    invalid = _run(_payload(total=15, strong=14, invalid=1))
    weak = _run(_payload(total=15, strong=14, weak=1))

    assert invalid.returncode != 0
    assert "citation_map contains 1 invalid cited keys" in invalid.stdout
    assert weak.returncode != 0
    assert "citation_map contains 1 weak cited keys" in weak.stdout


def test_citation_gate_rejects_missing_or_malformed_summary() -> None:
    missing = _payload()
    missing["citation_map"] = "CITATION_MAP:\n- no summary"
    malformed = _payload()
    malformed["citation_map"] = (
        "SUMMARY: total_cite_keys=fourteen, strong=14, ok=0, weak=0, invalid=0, unused=1"
    )

    missing_result = _run(missing)
    malformed_result = _run(malformed)

    assert missing_result.returncode != 0
    assert "exactly one machine-readable SUMMARY" in missing_result.stdout
    assert malformed_result.returncode != 0
    assert "total_cite_keys is not a non-negative integer" in malformed_result.stdout


def test_citation_gate_rejects_inconsistent_classification_total() -> None:
    result = _run(_payload(total=15, strong=13, ok=0))

    assert result.returncode != 0
    assert "classifications total 13, not 15 cited keys" in result.stdout
