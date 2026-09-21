from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from scripts.knowledge_research.navigation import Navigation
from scripts.knowledge_research.review import (
    report_review_hash,
    review_entries,
    review_locator,
    review_preparation,
)
from scripts.knowledge_research.state import KnowledgeResearchStore, ResearchStateError
from tests.test_scripts.test_knowledge_research_navigation import (
    invoke,
    result,
    search_payload,
    setup,
)


def test_deep_requires_scoped_call_and_current_complete_comparison(tmp_path: Path) -> None:
    source = search_payload("query", ["file-a"], "Original fact 5,700.")
    scoped = search_payload("precise", ["file-a"], "Original fact 5,700.", scoped=True)
    bridge, store, _, _ = setup(
        tmp_path, [result(source), result(scoped), result(_source_details(source))]
    )
    # Model a preexisting draft so the finalization gate still catches missing
    # scoped research independently of the new first-write preparation gate.
    begin, _ = invoke(bridge, "researchBegin", {"title": "Research", "mode": "standard"})
    rid = begin["researchId"]
    common = {"researchId": rid}
    search, _ = invoke(bridge, "search", {**common, "query": "query"})
    hit = search["results"][0]
    claim = {
        "claimKey": "fact",
        "section": "Finding",
        "text": "The target was raised to 5,700.",
        "evidenceRefs": [hit["evidenceRef"]],
    }
    invoke(bridge, "researchAddClaims", {**common, "batchKey": "one", "claims": [claim]})
    store.atomic_update(rid, lambda state: state.update(mode="deep"))
    pending = store.finalize(research_id=rid)
    assert pending["phase"] == "evaluation"
    assert pending["evaluation"]["status"] == "failed"
    assert pending["optimizationRequired"] is True
    assert "researchNavigate" in pending["nextStep"]
    assert {check["code"] for check in pending["checks"]} == {
        "SCOPED_SEARCH_REQUIRED",
        "SOURCE_COMPARISON_REQUIRED",
    }
    assert not store.output_root.exists()
    guarded, _ = invoke(bridge, "researchFinalize", common)
    assert guarded == pending
    assert "publicArtifactManifest" not in guarded
    invoke(
        bridge,
        "searchByIds",
        {**common, "query": "precise", "scopeRefs": [search["scopeRef"]]},
    )
    first, _ = invoke(bridge, "researchNavigate", {**common, "view": "review", "limit": 1})
    assert first["entries"][0]["kind"] == "claim"
    assert store.finalize(research_id=rid)["status"] == "needs_review"
    second, _ = invoke(
        bridge, "researchNavigate", {**common, "cursor": first["nextCursor"], "limit": 1}
    )
    assert second["entries"][0]["kind"] == "evidence"
    assert second["entries"][0]["content"] == source["results"][0]["content"]
    assert second["nextCursor"] is None
    finished = store.finalize(research_id=rid)
    assert finished["status"] == "finalized"
    assert finished["review"]["comparisonPrepared"] is True
    assert finished["review"]["semanticVerification"] == "not_performed_by_service"
    old_paths = [tmp_path / item["path"] for item in finished["publicArtifactManifest"]["files"]]
    old_bytes = [path.read_bytes() for path in old_paths]
    revised = {
        **claim,
        "text": "The target was raised to 5,700, according to the cited source.",
        "expectedClaimHash": first["entries"][0]["claimHash"],
    }
    invoke(bridge, "researchAddClaims", {**common, "batchKey": "two", "claims": [revised]})
    assert not review_preparation(store.snapshot(rid))["comparisonPrepared"]
    assert store.finalize(research_id=rid)["status"] == "needs_review"
    assert [path.read_bytes() for path in old_paths] == old_bytes


def test_long_comparison_pairs_each_claim_with_complete_sources(tmp_path: Path) -> None:
    content = '\u4e2d\u6587 "quoted" 12.8% ' * 900
    source = search_payload("query", ["file-a"], content)
    bridge, store, _, rid = setup(tmp_path, [result(source)])
    invoke(bridge, "search", {"researchId": rid, "query": "query"})
    evidence_id = source["results"][0]["evidenceId"]
    store.add_claims(
        research_id=rid,
        claims=[
            {
                "claimKey": str(i),
                "section": "Findings",
                "text": "Fact.",
                "evidenceIds": [evidence_id],
            }
            for i in range(2)
        ],
        batch_key="first",
    )
    state = store.snapshot(rid)
    nav = Navigation(state)
    review = review_entries(state, nav.reference)
    fragments = [entry for entry in review["entries"] if entry["kind"] == "evidence"]
    claims = [entry for entry in review["entries"] if entry["kind"] == "claim"]
    for claim in claims:
        bound = [entry for entry in fragments if entry["forClaimItem"] == claim["item"]]
        assert "".join(entry["content"] for entry in bound) == content
        assert review["entries"].index(bound[0]) == review["entries"].index(claim) + 1
    assert fragments[-1]["contentRange"]["end"] == len(content)
    assert all(len(entry["content"]) <= 2_000 for entry in fragments)
    assert sum(entry["kind"] == "claim" for entry in review["entries"]) == 2
    assert store.finalize(research_id=rid)["status"] == "finalized"


@pytest.mark.parametrize("field,value", [("mode", "unknown"), ("language", "fr")])
def test_invalid_research_options_create_no_state(tmp_path: Path, field: str, value: str) -> None:
    store = KnowledgeResearchStore(workspace=tmp_path)
    with pytest.raises(ResearchStateError):
        store.begin(title="Report", **{field: value})
    assert not store.private_root.exists()


def test_empty_scoped_search_is_observable_not_reading(tmp_path: Path) -> None:
    source = search_payload("query", ["file-a"])
    empty = search_payload("missing", [], scoped=True)
    bridge, store, _, rid = setup(tmp_path, [result(source), result(empty)])
    search, _ = invoke(bridge, "search", {"researchId": rid, "query": "query"})
    invoke(
        bridge,
        "searchByIds",
        {"researchId": rid, "query": "missing", "scopeRefs": [search["scopeRef"]]},
    )
    status = review_preparation(store.snapshot(rid))
    assert status["scopedSearchCallCount"] == 1
    assert status["comparisonPrepared"] is False


def test_table_caption_revision_and_source_truncation_are_preserved(tmp_path: Path) -> None:
    from scripts.knowledge_research.review import table_item_hash
    from tests.test_scripts.test_knowledge_research_claims import _seed, _seed_table, _store

    store = _store(tmp_path)
    rid = _seed(store)
    table_id = _seed_table(store, rid, truncated=True)
    arguments = {
        "research_id": rid,
        "section": "Tables",
        "table_id": table_id,
        "caption": "Original caption",
    }
    store.add_table(**arguments)
    state = store.snapshot(rid)
    before = next(item for item in state["report"]["items"] if item["kind"] == "table")
    digest = table_item_hash(before)
    with pytest.raises(ResearchStateError):
        store.add_table(**{**arguments, "caption": "Revised caption"})
    receipt = store.add_table(
        **{**arguments, "caption": "Revised caption", "expected_table_hash": digest}
    )
    assert receipt["item"] == before["itemId"]
    state = store.snapshot(rid)
    review = review_entries(state, Navigation(state).reference)
    table = next(entry for entry in review["entries"] if entry["kind"] == "table")
    assert table["inputCompleteness"] == "truncated"
    assert table["caption"] == "Revised caption"
    assert table["tableHash"] != digest
    assert table["quality"]["visualCheck"] == "not_performed"
    with pytest.raises(ResearchStateError):
        store.add_table(
            **{**arguments, "caption": "Another caption", "expected_table_hash": digest}
        )


def test_review_requires_interval_union_not_just_last_page(tmp_path: Path) -> None:
    bridge, store, _, rid = setup(tmp_path, [result(search_payload("q", ["file-a"]))])
    found, _ = invoke(bridge, "search", {"researchId": rid, "query": "q"})
    invoke(
        bridge,
        "researchAddClaims",
        {
            "researchId": rid,
            "batchKey": "facts",
            "claims": [
                {
                    "claimKey": str(i),
                    "section": "Findings",
                    "text": "A source-bound fact.",
                    "evidenceRefs": [found["results"][0]["evidenceRef"]],
                }
                for i in range(3)
            ],
        },
    )
    first, _ = invoke(bridge, "researchNavigate", {"researchId": rid, "view": "review", "limit": 1})
    nav = Navigation(store.snapshot(rid))
    last_cursor = nav.cursor(first["snapshotRef"], first["page"]["total"] - 1)
    last, _ = invoke(bridge, "researchNavigate", {"researchId": rid, "cursor": last_cursor})
    assert last["nextCursor"] is None
    assert not review_preparation(store.snapshot(rid))["comparisonPrepared"]
    cursor = first["nextCursor"]
    while cursor:
        reply, _ = invoke(
            bridge, "researchNavigate", {"researchId": rid, "cursor": cursor, "limit": 1}
        )
        cursor = reply["nextCursor"]
    assert review_preparation(store.snapshot(rid))["comparisonPrepared"]


def _source_details(source: Mapping[str, Any]) -> dict[str, Any]:
    record = source["results"][0]
    return {
        "contractVersion": "knowledge-vnext/2",
        "file": {
            "fileId": record["fileId"],
            "documentId": record["documentId"],
            "revision": record["revision"],
            "title": "HSBC | 2026-07-07 Korea outlook",
            "filename": "hsbc-korea.pdf",
            "sourcePath": "research/2026-07-07/hsbc-korea.pdf",
            "mediaType": "application/pdf",
        },
        "tables": [],
        "nextCursor": None,
        "tableExtraction": {"status": "ready", "tableCount": 0},
    }


def _submit_fact(store: KnowledgeResearchStore, rid: str, source: Mapping[str, Any]) -> None:
    store.add_claims(
        research_id=rid,
        claims=[
            {
                "claimKey": "finding",
                "section": "Findings",
                "text": "Verified finding.",
                "evidenceIds": [source["results"][0]["evidenceId"]],
            }
        ],
        batch_key="draft",
    )


def test_metadata_completion_precedes_review_without_an_extra_full_pass(tmp_path: Path) -> None:
    source = search_payload("q", ["file-a"], scoped=True)
    source["results"][0]["title"] = "[page 7]"
    details = _source_details(source)
    bridge, store, upstream, _ = setup(tmp_path, [result(source), result(details)])
    # Metadata/review must also complete for a legacy draft already on disk.
    rid = store.begin(title="Deep report", mode="standard")["researchId"]
    invoke(bridge, "searchByIds", {"researchId": rid, "query": "q", "fileIds": ["file-a"]})
    _submit_fact(store, rid, source)
    store.atomic_update(rid, lambda state: state.update(mode="deep"))
    first, _ = invoke(bridge, "researchNavigate", {"researchId": rid, "view": "review"})
    assert review_preparation(store.snapshot(rid))["comparisonPrepared"]
    assert not store.output_root.exists()
    assert len(upstream.calls) == 2
    entry = next(row for row in first["entries"] if row["kind"] == "evidence")
    assert entry["title"] == details["file"]["title"]
    assert entry["sourceFormat"] == "PDF"
    finished, _ = invoke(bridge, "researchFinalize", {"researchId": rid})
    assert finished["status"] == "finalized"
    second, _ = invoke(bridge, "researchNavigate", {"researchId": rid, "view": "review"})
    assert second["entries"] == []
    assert second["reportHash"] == first["reportHash"]
    replay, _ = invoke(bridge, "researchFinalize", {"researchId": rid})
    assert replay == finished
    assert len(upstream.calls) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "Other institution | 2027-01-01 Outlook"),
        ("filename", "another-issue.pdf"),
        ("sourcePath", "research/2027-01-01/another-issue.pdf"),
        ("institution", "Other Institution"),
        ("publicationDate", "2027-01-01"),
    ],
)
def test_only_cited_source_metadata_changes_invalidate_review(
    tmp_path: Path, field: str, value: str
) -> None:
    source = search_payload("q", ["file-a"])
    bridge, store, _, rid = setup(tmp_path, [result(source)])
    invoke(bridge, "search", {"researchId": rid, "query": "q"})
    _submit_fact(store, rid, source)
    invoke(bridge, "researchNavigate", {"researchId": rid, "view": "review"})
    before = store.snapshot(rid)
    changed = copy.deepcopy(before)
    changed["ledger"]["files"]["file-a"][field] = value
    assert not review_preparation(changed)["comparisonPrepared"]
    changed = copy.deepcopy(before)
    changed["ledger"]["files"]["not-cited"] = {field: value}
    changed["ledger"]["calls"].append(
        {"toolName": "getFileDetails", "arguments": {"fileId": "file-a"}}
    )
    changed["ledger"]["files"]["file-a"].update(
        metadataSource="getFileDetails", observedLocators=[{"pageStart": 99}], pageCount=100
    )
    changed["report"]["revision"] = 999
    changed["ledger"]["evidence"][source["results"][0]["evidenceId"]]["trace"] = "unrelated"
    assert report_review_hash(changed) == report_review_hash(before)
    assert review_preparation(changed)["comparisonPrepared"]


def test_known_search_metadata_is_hashed_and_authoritative_file_fields_win(tmp_path: Path) -> None:
    source = search_payload("q", ["file-a"])
    source["results"][0].update(institution="First Institution", publicationDate="2026-07-07")
    bridge, store, _, rid = setup(tmp_path, [result(source)])
    invoke(bridge, "search", {"researchId": rid, "query": "q"})
    _submit_fact(store, rid, source)
    first, _ = invoke(bridge, "researchNavigate", {"researchId": rid, "view": "review"})
    entry = next(row for row in first["entries"] if row["kind"] == "evidence")
    assert entry["institution"] == "First Institution"
    state = store.snapshot(rid)
    state["extensions"]["navigation"]["fileMetadata"]["file-a"]["institution"] = "New Institution"
    assert not review_preparation(state)["comparisonPrepared"]
    state["ledger"]["files"]["file-a"]["institution"] = "Authoritative Institution"
    page = review_entries(state, Navigation(state).reference)
    entry = next(row for row in page["entries"] if row["kind"] == "evidence")
    assert entry["institution"] == "Authoritative Institution"


@pytest.mark.parametrize(
    "locator",
    [
        {"page": {"start": 7, "end": 8}},
        {"sectionPath": ["Forecast assumptions", "ADVT definition"]},
        {"pageStart": 3, "pageEnd": 5, "pages": [3, 5]},
    ],
)
def test_review_preserves_legal_page_and_section_locators(
    tmp_path: Path, locator: dict[str, Any]
) -> None:
    source = search_payload("q", ["file-a"])
    source["results"][0]["locator"] = locator
    bridge, store, _, rid = setup(tmp_path, [result(source)])
    invoke(bridge, "search", {"researchId": rid, "query": "q"})
    _submit_fact(store, rid, source)
    page, _ = invoke(bridge, "researchNavigate", {"researchId": rid, "view": "review"})
    entry = next(row for row in page["entries"] if row["kind"] == "evidence")
    expected = {"pageStart": 7, "pageEnd": 8} if "page" in locator else locator
    assert expected.items() <= entry["locator"].items()


def test_unicode_metadata_fragments_are_lossless_and_required_for_coverage(tmp_path: Path) -> None:
    source = search_payload("q", ["file-a"])
    section = '\u4e2d\U00020000 "quoted" \\ 2027E\n' * 2_000
    source["results"][0]["locator"] = {"page": {"start": 7, "end": 8}, "sectionPath": [section]}
    bridge, store, _, rid = setup(tmp_path)
    store.record_knowledge_call(
        research_id=rid,
        tool_name="search",
        arguments={"query": "q"},
        result=result(source)["result"],
    )
    _submit_fact(store, rid, source)
    first, _ = invoke(bridge, "researchNavigate", {"researchId": rid, "view": "review", "limit": 1})
    nav = Navigation(store.snapshot(rid))
    last, _ = invoke(
        bridge,
        "researchNavigate",
        {
            "researchId": rid,
            "cursor": nav.cursor(first["snapshotRef"], first["page"]["total"] - 1),
        },
    )
    assert last["nextCursor"] is None
    assert not review_preparation(store.snapshot(rid))["comparisonPrepared"]
    entries = list(first["entries"])
    cursor = first["nextCursor"]
    while cursor:
        page, response = invoke(bridge, "researchNavigate", {"researchId": rid, "cursor": cursor})
        assert not response["result"]["isError"], page
        entries.extend(page["entries"])
        cursor = page["nextCursor"]
    metadata = [entry for entry in entries if entry["kind"] == "metadata"]
    restored = json.loads("".join(entry["content"] for entry in metadata))
    assert restored["locator"]["sectionPath"] == [section]
    assert (restored["locator"]["pageStart"], restored["locator"]["pageEnd"]) == (7, 8)
    assert all(len(entry["content"]) <= 2_000 for entry in entries)
    assert review_preparation(store.snapshot(rid))["comparisonPrepared"]
    assert (
        "".join(row["content"] for row in entries if row["kind"] == "evidence")
        == source["results"][0]["content"]
    )


def test_locator_callback_can_be_injected_without_navigation_import(tmp_path: Path) -> None:
    source = search_payload("q", ["file-a"])
    bridge, store, _, rid = setup(tmp_path, [result(source)])
    invoke(bridge, "search", {"researchId": rid, "query": "q"})
    _submit_fact(store, rid, source)
    state = store.snapshot(rid)
    calls = []

    def project(locator: Mapping[str, Any], title: str) -> dict[str, Any]:
        calls.append((locator, title))
        return review_locator(locator, title)

    page = review_entries(state, Navigation(state).reference, locator_projection=project)
    assert len(calls) == 1
    assert page["entries"][-1]["locator"]["pageStart"] == 1


def test_identical_passages_in_different_issues_remain_separate(tmp_path: Path) -> None:
    source = search_payload("q", ["issue-one", "issue-two"], "The same passage.")
    bridge, store, _, rid = setup(tmp_path, [result(source)])
    found, _ = invoke(bridge, "search", {"researchId": rid, "query": "q"})
    store.add_claims(
        research_id=rid,
        claims=[
            {
                "claimKey": "comparison",
                "section": "Findings",
                "text": "A comparison.",
                "evidenceIds": [row["evidenceId"] for row in source["results"]],
            }
        ],
        batch_key="draft",
    )
    page, _ = invoke(bridge, "researchNavigate", {"researchId": rid, "view": "review"})
    evidence = [entry for entry in page["entries"] if entry["kind"] == "evidence"]
    assert len(evidence) == 2
    assert {row["evidenceRef"] for row in evidence} == {
        row["evidenceRef"] for row in found["results"]
    }
