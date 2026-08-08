from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from opensquilla.subprocess_encoding import apply_utf8_child_env

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT
    / "src"
    / "opensquilla"
    / "skills"
    / "bundled"
    / "paper-refbib-stub"
    / "scripts"
    / "json_to_bib.py"
)


def _run_refbib(payload: dict[str, object], out: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out)],
        input=json.dumps(payload),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        env=apply_utf8_child_env(dict(os.environ)),
    )


def test_refbib_preserves_metadata_omits_unknown_year_and_deduplicates_doi(
    tmp_path: Path,
) -> None:
    payload = {
        "query": "resource-aware routing",
        "results": [
            {
                "title": "Resource-aware task routing",
                "url": "https://doi.org/10.5555/Example.One",
                "snippet": "Journal of Edge Systems",
                "doi": "10.5555/Example.One",
                "year": 2022,
                "authors": ["Ada Lovelace", "Lin Chen", "Edge Systems Consortium"],
                "corporate_authors": ["Edge Systems Consortium"],
            },
            {
                "title": "Duplicate publisher landing page",
                "url": "https://publisher.example/doi/10.5555/example.one",
                "doi": "https://doi.org/10.5555/EXAMPLE.ONE",
                "year": 2023,
                "authors": ["Should Not Appear"],
            },
            {
                "title": "Metadata without a known year",
                "url": "https://example.org/no-year",
                "snippet": "Year is unavailable.",
                "authors": [{"given": "Grace", "family": "Hopper"}],
            },
        ],
    }
    out = tmp_path / "references.bib"
    completed = _run_refbib(payload, out)

    bib = out.read_text(encoding="utf-8")
    assert completed.stdout == bib
    assert bib.count("@misc{") == 2
    assert "@misc{ref1," in bib
    assert "@misc{ref2," in bib
    assert "doi = {10.5555/example.one}" in bib
    assert bib.lower().count("10.5555/example.one") == 2  # URL plus DOI field in one entry.
    assert (
        "author = {Ada Lovelace and Lin Chen and {{Edge Systems Consortium}}}" in bib
    )
    assert "author = {Grace Hopper}" in bib
    assert "year = {2022}" in bib
    assert "year = {2023}" not in bib
    assert "year = {2026}" not in bib
    assert "Should Not Appear" not in bib


def test_refbib_uses_explicit_doi_when_url_is_missing_and_ignores_bad_year(
    tmp_path: Path,
) -> None:
    payload = {
        "results": [
            {
                "title": "DOI-only record",
                "doi": "doi:10.9999/Only.DOI",
                "year": "unknown",
                "authors": [],
            }
        ]
    }
    out = tmp_path / "references.bib"
    _run_refbib(payload, out)

    bib = out.read_text(encoding="utf-8")
    assert "howpublished = {\\url{https://doi.org/10.9999/only.doi}}" in bib
    assert "doi = {10.9999/only.doi}" in bib
    assert "year =" not in bib


def test_refbib_preserves_valid_locator_shapes_and_omits_empty_howpublished(
    tmp_path: Path,
) -> None:
    payload = {
        "results": [
            {
                "title": "HTTPS record",
                "url": "https://papers.example.test/item",
            },
            {
                "title": "DOI-only record",
                "doi": "10.7777/example.doi",
            },
            {
                "title": "arXiv record",
                "url": "https://arxiv.org/abs/2401.12345v2",
            },
            {
                "title": "Record without a locator",
                "url": "",
            },
        ]
    }
    out = tmp_path / "references.bib"

    _run_refbib(payload, out)

    bib = out.read_text(encoding="utf-8")
    assert "howpublished = {\\url{https://papers.example.test/item}}" in bib
    assert "howpublished = {\\url{https://doi.org/10.7777/example.doi}}" in bib
    assert "doi = {10.7777/example.doi}" in bib
    assert "howpublished = {\\url{https://arxiv.org/abs/2401.12345v2}}" in bib
    assert "eprint = {2401.12345}" in bib
    empty_entry = bib.split("@misc{ref4,", 1)[1]
    assert "title = {Record without a locator}" in empty_entry
    assert "howpublished" not in empty_entry


def test_refbib_sanitizes_embedded_entries_unbalanced_braces_and_controls(
    tmp_path: Path,
) -> None:
    payload = {
        "results": [
            {
                "title": "Unicode 标题 {broken} @Article{nested, title={`T`}\x00",
                "url": "https://example.org/论文/{unsafe}`?q=a_b&x=1",
                "snippet": (
                    "Copied metadata @InProceedings\\{victim, title=\\{Unclosed "
                    "`fragment` with } stray brace\x1b"
                ),
                "authors": [
                    {"literal": "研究机构 {Lab} @Book{evil"},
                    {"given": "Ada`", "family": "Lovelace\\{Test"},
                ],
                "corporate_authors": ["研究机构 {Lab} @Book{evil"],
                "year": 2024,
            }
        ]
    }
    out = tmp_path / "references.bib"

    completed = _run_refbib(payload, out)

    bib = out.read_text(encoding="utf-8")
    assert completed.stdout == bib
    assert "Unicode 标题" in bib
    assert "研究机构" in bib
    assert "@Article" not in bib
    assert "@InProceedings" not in bib
    assert "@Book" not in bib
    assert "`" not in bib
    assert "\x00" not in bib
    assert "\x1b" not in bib
    assert "https://example.org/论文/%7Bunsafe%7D%60?q=a_b&x=1" in bib
    assert bib.count("{") == bib.count("}")


def test_refbib_malicious_search_metadata_parses_with_real_bibtex(tmp_path: Path) -> None:
    bibtex = shutil.which("bibtex")
    if bibtex is None:
        pytest.skip("real bibtex is not installed")

    payload = {
        "results": [
            {
                "title": "安全 Unicode 标题 @Article{unterminated",
                "url": "https://example.org/paper/{unsafe}`",
                "snippet": "@InProceedings\\{x, title=\\{copied fragment with { braces",
                "authors": [{"given": "Lin", "family": "Chen{broken"}],
                "year": 2025,
            }
        ]
    }
    references = tmp_path / "references.bib"
    _run_refbib(payload, references)
    (tmp_path / "paper.aux").write_text(
        "\\relax\n\\citation{ref1}\n\\bibstyle{plain}\n\\bibdata{references}\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [bibtex, "paper"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (tmp_path / "paper.bbl").is_file()
