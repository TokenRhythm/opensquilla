from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.knowledge_research.bridge import KnowledgeResearchBridge, _arguments
from scripts.knowledge_research.reading_coverage import SQLiteReadingCoverage
from scripts.knowledge_research.state import KnowledgeResearchStore
from tests.test_scripts.test_knowledge_research_metadata_preparation import details
from tests.test_scripts.test_knowledge_research_navigation import (
    Upstream,
    invoke,
    result,
    search_payload,
)
from tests.test_scripts.test_knowledge_research_reading_coverage import _db

EVIDENCE = "ev4_11111111111111111111111111111111"


def _seed(store: KnowledgeResearchStore, *, mode: str = "standard") -> str:
    # A deep fixture represents an already stored draft from before the writing
    # gate; these tests exercise finalization/coverage ordering, not first writes.
    rid = store.begin(title="Research", mode="standard", language="zh-CN")["researchId"]
    store.record_knowledge_call(
        research_id=rid,
        tool_name="search",
        arguments={"query": "earnings"},
        result={
            "structuredContent": {
                "contractVersion": "knowledge-vnext/2",
                "chunkPolicyId": "hierarchical_token_v4",
                "indexVersion": "knowledge-index-v5",
                "effectiveProfile": "hybrid_rrf_bge_m3_fts5",
                "retrievalProfile": "hybrid_rrf_bge_m3_fts5",
                "selectionStrategy": "hierarchical_interleave",
                "scopeEnforced": True,
                "warnings": [],
                "count": 1,
                "lexicalCandidateCount": 1,
                "vectorCandidateCount": 1,
                "results": [
                    {
                        "evidenceId": EVIDENCE,
                        "fileId": "fixture-file-001",
                        "documentId": "fixture-document-001",
                        "chunkId": "fixture-chunk-001",
                        "revision": "a" * 64,
                        "content": "Conditional earnings forecast.",
                        "title": "Source research",
                        "locator": {"pageStart": 1, "pageEnd": 1},
                    }
                ],
            },
            "isError": False,
        },
    )
    store.add_claims(
        research_id=rid,
        batch_key="draft",
        claims=[
            {
                "section": "Outlook",
                "text": "The forecast is conditional.",
                "evidenceIds": [EVIDENCE],
                "claimKey": "outlook",
            }
        ],
    )
    if mode != "standard":
        store.atomic_update(rid, lambda state: state.update(mode=mode))
    return str(rid)


def _reading(percentage: Any = 8.93, status: str = "available") -> dict[str, Any]:
    return {
        "schemaVersion": "bibliography-reading-coverage/1",
        "method": "returned_indexed_source_text_union",
        "references": [
            {
                "number": 1,
                "status": status,
                "percentage": percentage,
                "returnedSourceChars": 893,
                "indexedSourceChars": 10000,
                "members": [{"fileId": "fixture-file-001"}],
            }
        ],
    }


def _outputs(store: KnowledgeResearchStore, receipt: dict[str, Any]) -> dict[str, Path]:
    return {
        item["name"]: store.workspace / item["path"]
        for item in receipt["publicArtifactManifest"]["files"]
    }


def test_finalize_automatically_renders_coverage_and_records_machine_details(
    tmp_path: Path,
) -> None:
    rendered: list[str] = []

    def pdf(html: str, _base: Path) -> bytes:
        rendered.append(html)
        return b"%PDF-fixture"

    store = KnowledgeResearchStore(
        workspace=tmp_path,
        pdf_renderer=pdf,
        reading_coverage_resolver=lambda _state: _reading(),
    )
    rid = _seed(store)
    before = store.snapshot(rid)
    receipt = store.finalize(research_id=rid)
    outputs = _outputs(store, receipt)
    html = outputs["report.html"].read_text()
    assert html == rendered[0]
    assert html.count("\u8986\u76d6\u7387\uff1a8.93%") == 1
    assert "893" not in html and "fixture-file-001" not in html
    assert "readingCoverage" not in receipt
    provenance = json.loads(outputs["provenance.json"].read_text())
    assert provenance["readingCoverage"] == _reading()
    assert provenance["report"] == before["report"]
    assert store.snapshot(rid)["ledger"] == before["ledger"]


def test_coverage_overview_and_legacy_extension_are_published(tmp_path: Path) -> None:
    reading = _reading()
    reading["summary"] = {
        "bibliographyEntries": 1,
        "availableReferences": 1,
        "unavailableReferences": 0,
        "returnedSourceChars": 893,
        "indexedSourceChars": 10_000,
        "overallPercentage": 8.93,
        "medianPercentage": 8.93,
        "below5pctReferences": 0,
        "below10pctReferences": 1,
        "fullTextCoverageReferences": 0,
    }
    store = KnowledgeResearchStore(
        workspace=tmp_path,
        pdf_renderer=lambda *_: b"%PDF-fixture",
        reading_coverage_resolver=lambda _: copy.deepcopy(reading),
    )
    outputs = _outputs(store, store.finalize(research_id=_seed(store)))
    html = outputs["report.html"].read_text()
    provenance = json.loads(outputs["provenance.json"].read_text())
    assert "\u8d44\u6599\u9605\u8bfb\u8986\u76d6\u6982\u89c8" in html
    assert "\u603b\u4f53\u8986\u76d6\u7387 8.93%" in html
    assert provenance["extensions"]["bibliographyReadingCoverage"] == reading


@pytest.mark.parametrize(
    "value,status",
    [
        (None, "unavailable"),
        (-1, "available"),
        (101, "available"),
        (True, "available"),
        ("8.93<script>", "available"),
    ],
)
def test_unknown_or_invalid_coverage_does_not_fabricate_percent(
    tmp_path: Path, value: Any, status: str
) -> None:
    store = KnowledgeResearchStore(
        workspace=tmp_path,
        pdf_renderer=lambda *_: b"%PDF-fixture",
        reading_coverage_resolver=lambda _: _reading(value, status),
    )
    outputs = _outputs(store, store.finalize(research_id=_seed(store)))
    html = outputs["report.html"].read_text()
    assert "\u8986\u76d6\u7387\uff1a\u672a\u7edf\u8ba1" in html
    assert "0.00%" not in html and "<script>" not in html


def test_coverage_change_invalidates_cached_artifact_but_keeps_prior_bytes(tmp_path: Path) -> None:
    value = _reading()
    store = KnowledgeResearchStore(
        workspace=tmp_path,
        pdf_renderer=lambda *_: b"%PDF-fixture",
        reading_coverage_resolver=lambda _: copy.deepcopy(value),
    )
    rid = _seed(store)
    first = store.finalize(research_id=rid)
    old_bytes = {path: path.read_bytes() for path in _outputs(store, first).values()}
    assert store.finalize(research_id=rid) == first
    value["references"][0]["percentage"] = 20
    value["references"][0]["returnedSourceChars"] = 2000
    second = store.finalize(research_id=rid)
    assert second != first
    assert "\u8986\u76d6\u7387\uff1a20.00%" in _outputs(store, second)["report.html"].read_text()
    assert all(path.read_bytes() == payload for path, payload in old_bytes.items())


def test_disabled_coverage_keeps_existing_contract(tmp_path: Path) -> None:
    store = KnowledgeResearchStore(workspace=tmp_path, pdf_renderer=lambda *_: b"%PDF-fixture")
    outputs = _outputs(store, store.finalize(research_id=_seed(store)))
    assert "\u8986\u76d6\u7387\uff1a" not in outputs["report.html"].read_text()
    assert "readingCoverage" not in json.loads(outputs["provenance.json"].read_text())


def test_deep_review_gate_precedes_coverage_lookup(tmp_path: Path) -> None:
    store = KnowledgeResearchStore(
        workspace=tmp_path,
        pdf_renderer=lambda *_: pytest.fail("must not render"),
        reading_coverage_resolver=lambda _: pytest.fail("must not query DB before review"),
    )
    reply = store.finalize(research_id=_seed(store, mode="deep"))
    assert reply["status"] == "needs_review"


def test_coverage_db_is_operator_configuration_not_an_agent_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_KNOWLEDGE_COVERAGE_DB", "/fixture/knowledge.db")
    common = ["--workspace", "/fixture/workspace", "--media-root", "/fixture/media"]
    assert _arguments([*common, "--", "upstream"]).coverage_db == "/fixture/knowledge.db"
    assert (
        _arguments(
            [*common, "--coverage-db", "/explicit/knowledge.db", "--", "upstream"]
        ).coverage_db
        == "/explicit/knowledge.db"
    )


def test_fresh_mcp_research_gets_coverage_without_model_percentage_argument(tmp_path: Path) -> None:
    database = _db(tmp_path / "knowledge.db")
    source = search_payload("q", ["file-1"], "x" * 60)
    source["results"][0].update(documentId="doc-1", chunkId="e1")
    source["results"][0]["locator"].update(charStart=0, charEnd=60)
    metadata = details("file-1")
    metadata["file"]["documentId"] = "doc-1"
    upstream = Upstream([result(source), result(metadata)])
    rendered = []

    def pdf(document: str, _: Path) -> bytes:
        rendered.append(document)
        return b"%PDF-fixture"

    store = KnowledgeResearchStore(
        workspace=tmp_path / "workspace",
        pdf_renderer=pdf,
        reading_coverage_resolver=SQLiteReadingCoverage(database),
    )
    bridge = KnowledgeResearchBridge(upstream, store)
    begun, _ = invoke(bridge, "researchBegin", {"title": "Research", "language": "zh-CN"})
    rid = begun["researchId"]
    found, _ = invoke(bridge, "search", {"researchId": rid, "query": "q"})
    accepted, _ = invoke(
        bridge,
        "researchAddClaims",
        {
            "researchId": rid,
            "batchKey": "draft",
            "claims": [
                {
                    "section": "Outlook",
                    "text": "A supported observation.",
                    "claimKey": "outlook",
                    "evidenceRefs": [found["results"][0]["evidenceRef"]],
                }
            ],
        },
    )
    assert accepted["status"] == "accepted"
    final, _ = invoke(bridge, "researchFinalize", {"researchId": rid})
    assert final["status"] == "finalized"
    assert "\u8986\u76d6\u7387\uff1a30.00%" in rendered[0]
    assert len(upstream.calls) == 2
