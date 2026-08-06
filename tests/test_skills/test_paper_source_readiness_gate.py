"""Offline contracts for the early paper source-readiness gate."""

from __future__ import annotations

import json
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
    / "paper-source-readiness-gate"
    / "scripts"
    / "audit.py"
)


def _payload(*, target: int, usable: int, status: str = "sufficient") -> dict[str, str]:
    keys = [f"ref{index}" for index in range(1, usable + 1)]
    source_pack = "\n".join(
        [
            f"SOURCE_STATUS: {status}",
            f"CITATION_TARGET: {target}",
            f"USABLE_REFERENCE_COUNT: {usable}",
            "USABLE_KEYS:",
            *(f"- {key}" for key in keys),
            "EXCLUDED_KEYS:",
            "- none",
            "SOURCE_PACK:",
            "PRIMARY_REFERENCES:",
            *(f"- {key} | Synthetic paper {key} | relevant claim" for key in keys),
            "COVERAGE_GAPS:",
            "- none",
        ],
    )
    bibliography = "\n".join(
        (
            f"@article{{{key},\n"
            f"  title = {{Synthetic paper {key}}},\n"
            f"  url = {{https://example.test/{key}}}\n"
            "}"
        )
        for key in keys
    )
    return {
        "paper_contract": (
            "PAPER_MODE: FULL_MANUSCRIPT\n"
            f"CITATION_TARGET: {target}\n"
            "EVIDENCE_STATUS: not_supplied"
        ),
        "paper_preferences": f"CITATION_TARGET: {target}",
        "source_pack": source_pack,
        "bibliography": bibliography,
    }


def _run(payload: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def test_source_readiness_gate_passes_unique_verified_target() -> None:
    result = _run(_payload(target=15, usable=15))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SOURCE_READINESS: pass" in result.stdout
    assert "FOUND_REFERENCES: 15/15" in result.stdout
    assert "- none" in result.stdout


@pytest.mark.parametrize(
    "locator",
    [
        "url = {https://papers.example.test/ref1}",
        r"howpublished = {\url{https://papers.example.test/ref1}}",
        "doi = {10.5555/example.one}",
        "eprint = {2401.12345v2}",
        "eprint = {cs.LG/0312001}",
    ],
)
def test_source_readiness_gate_accepts_strict_offline_locators(locator: str) -> None:
    payload = _payload(target=1, usable=1)
    payload["bibliography"] = (
        "@article{ref1,\n"
        "  title = {Synthetic paper},\n"
        f"  {locator}\n"
        "}"
    )

    result = _run(payload)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "FOUND_REFERENCES: 1/1" in result.stdout


@pytest.mark.parametrize(
    "locator",
    [
        "url={https://papers.example.test/ref1}",
        "doi={10.5555/example.one}",
        "eprint={2401.12345v2}",
    ],
)
def test_source_readiness_gate_accepts_single_line_bibtex_locators(locator: str) -> None:
    payload = _payload(target=1, usable=1)
    payload["bibliography"] = f"@article{{ref1,title={{Synthetic paper}},{locator}}}"

    result = _run(payload)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "FOUND_REFERENCES: 1/1" in result.stdout


@pytest.mark.parametrize(
    "locator",
    [
        "url = {}",
        r"howpublished = {\url{}}",
        "url = {http://papers.example.test/ref1}",
        "url = {https:///missing-host}",
        "doi = {}",
        "doi = {not-a-doi}",
        "eprint = {}",
        "eprint = {not-an-arxiv-id}",
    ],
)
def test_source_readiness_gate_rejects_empty_or_malformed_locators(locator: str) -> None:
    payload = _payload(target=1, usable=1)
    payload["bibliography"] = (
        "@article{ref1,\n"
        "  title = {Synthetic paper},\n"
        f"  {locator}\n"
        "}"
    )

    result = _run(payload)

    assert result.returncode != 0
    assert "FOUND_REFERENCES: 0/1" in result.stdout
    assert "usable bibliography entries lack URL/DOI/arXiv locator: ref1" in result.stdout


@pytest.mark.parametrize(
    "locator",
    [
        "url={}",
        "url={https:///missing-host}",
        "doi={not-a-doi}",
        "eprint={not-an-arxiv-id}",
    ],
)
def test_source_readiness_gate_rejects_malformed_single_line_locators(locator: str) -> None:
    payload = _payload(target=1, usable=1)
    payload["bibliography"] = f"@article{{ref1,title={{Synthetic paper}},{locator}}}"

    result = _run(payload)

    assert result.returncode != 0
    assert "FOUND_REFERENCES: 0/1" in result.stdout
    assert "usable bibliography entries lack URL/DOI/arXiv locator: ref1" in result.stdout


def test_source_readiness_gate_ignores_locator_like_text_inside_title() -> None:
    payload = _payload(target=1, usable=1)
    payload["bibliography"] = (
        "@article{ref1,title={Discussion of, url={https://example.test/false}}}"
    )

    result = _run(payload)

    assert result.returncode != 0
    assert "FOUND_REFERENCES: 0/1" in result.stdout


def test_source_readiness_gate_blocks_with_concrete_three_of_fifteen_count() -> None:
    result = _run(_payload(target=15, usable=3, status="insufficient"))

    assert result.returncode != 0
    assert "SOURCE_READINESS: block" in result.stdout
    assert "source coverage insufficient: found 3/15 usable references" in result.stdout
    # skill_exec reports stderr on non-zero exits, so the actionable count is
    # mirrored there instead of becoming an empty error in the UI.
    assert "found 3/15 usable references" in result.stderr


def test_source_readiness_gate_parses_bold_markdown_fields_without_relaxing_gate() -> None:
    payload = _payload(target=10, usable=6, status="insufficient")
    for field in (
        "SOURCE_STATUS",
        "CITATION_TARGET",
        "USABLE_REFERENCE_COUNT",
        "USABLE_KEYS",
        "EXCLUDED_KEYS",
        "SOURCE_PACK",
        "PRIMARY_REFERENCES",
        "COVERAGE_GAPS",
    ):
        payload["source_pack"] = payload["source_pack"].replace(
            f"{field}:",
            f"**{field}:**",
        )

    result = _run(payload)

    assert result.returncode != 0
    assert "SOURCE_STATUS is insufficient, not sufficient" in result.stdout
    assert "source coverage insufficient: found 6/10 usable references" in result.stdout
    assert "source pack omitted" not in result.stdout
    assert "USABLE_KEYS is empty or missing" not in result.stdout
    assert "PRIMARY_REFERENCES is empty or missing" not in result.stdout


def test_source_readiness_gate_deduplicates_markdown_wrapped_usable_keys() -> None:
    payload = _payload(target=2, usable=2)
    payload["source_pack"] = payload["source_pack"].replace(
        "USABLE_KEYS:\n- ref1\n- ref2",
        "**USABLE_KEYS:**\n- ref1\n- ref1",
    ).replace(
        "PRIMARY_REFERENCES:\n- ref1 | Synthetic paper ref1 | relevant claim\n"
        "- ref2 | Synthetic paper ref2 | relevant claim",
        "**PRIMARY_REFERENCES:**\n- ref1 | Synthetic paper ref1 | relevant claim\n"
        "- ref1 | Synthetic paper ref1 | duplicate claim",
    )

    result = _run(payload)

    assert result.returncode != 0
    assert "declared usable count 2 does not match 1 verified primary keys" in result.stdout
    assert "source coverage insufficient: found 1/2 usable references" in result.stdout


def test_source_readiness_gate_rejects_duplicate_markdown_scalar_fields() -> None:
    payload = _payload(target=4, usable=4)
    payload["source_pack"] = payload["source_pack"].replace(
        "USABLE_REFERENCE_COUNT: 4",
        "USABLE_REFERENCE_COUNT: 4\n**USABLE_REFERENCE_COUNT**: 999",
    )

    result = _run(payload)

    assert result.returncode != 0
    assert "source pack contains duplicate USABLE_REFERENCE_COUNT fields" in result.stdout


def test_source_readiness_gate_does_not_parse_embedded_markdown_as_a_field() -> None:
    payload = _payload(target=2, usable=2)
    payload["source_pack"] = payload["source_pack"].replace(
        "USABLE_KEYS:",
        "Narrative text containing **USABLE_KEYS:**",
    )

    result = _run(payload)

    assert result.returncode != 0
    assert "USABLE_KEYS is empty or missing" in result.stdout
    assert "source coverage insufficient: found 0/2 usable references" in result.stdout


def test_source_readiness_gate_does_not_trust_declared_count_without_primary_keys() -> None:
    payload = _payload(target=4, usable=4)
    payload["source_pack"] = payload["source_pack"].replace(
        "- ref4 | Synthetic paper ref4 | relevant claim",
        "- excluded4 | unrelated result | excluded",
    )

    result = _run(payload)

    assert result.returncode != 0
    assert "declared usable count 4 does not match 3 verified primary keys" in result.stdout
    assert "usable keys absent from PRIMARY_REFERENCES: ref4" in result.stdout
