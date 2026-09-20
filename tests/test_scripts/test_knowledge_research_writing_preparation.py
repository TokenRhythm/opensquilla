from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.knowledge_research.navigation import Navigation
from tests.test_scripts.test_knowledge_research_navigation import (
    REVISION,
    invoke,
    result,
    search_payload,
    setup,
)


def _source(files: list[str], content: str = "Source facts and qualifications.") -> dict[str, Any]:
    payload = search_payload("discovery", files, content)
    for row in payload["results"]:
        row["filename"] = row["fileId"]
    return payload


def _details(file_id: str, *, next_cursor: str | None = None) -> dict[str, Any]:
    return {
        "contractVersion": "knowledge-vnext/2",
        "file": {
            "fileId": file_id,
            "documentId": "document-" + file_id,
            "revision": REVISION,
            "filename": file_id,
            "title": "Source research",
        },
        "tables": [],
        "nextCursor": next_cursor,
    }


def _claims(found: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "claimKey": f"finding-{index}",
            "section": "Findings",
            "text": "The source's finding remains conditional.",
            "evidenceRefs": [row["evidenceRef"]],
        }
        for index, row in enumerate(found["results"])
    ]


def _begin(bridge: Any, mode: str = "deep") -> str:
    started, _ = invoke(bridge, "researchBegin", {"title": "Research", "mode": mode})
    return str(started["researchId"])


def _write(bridge: Any, rid: str, claims: list[dict[str, Any]], *, single: bool = False) -> Any:
    common = {"researchId": rid, "batchKey": "first-draft"}
    name = "researchAddClaim" if single else "researchAddClaims"
    return invoke(bridge, name, {**common, **claims[0]} if single else {**common, "claims": claims})


def _checks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    assert payload["details"]["code"] == "RESEARCH_PREPARATION_REQUIRED"
    assert payload["details"]["committed"] is False
    return list(payload["details"]["checks"])


def _read_all(bridge: Any, rid: str, evidence_ref: str) -> None:
    arguments = {"researchId": rid, "evidenceRef": evidence_ref}
    page, _ = invoke(bridge, "researchReadEvidence", arguments)
    while page.get("nextCursor"):
        page, _ = invoke(
            bridge, "researchReadEvidence", {**arguments, "cursor": page["nextCursor"]}
        )


@pytest.mark.parametrize("single", [False, True])
def test_first_deep_write_blocks_atomically_and_recovers_with_same_batch_key(
    tmp_path: Path, single: bool
) -> None:
    source = _source(["analysis.pdf"])
    empty = search_payload("counterevidence", [], scoped=True)
    bridge, store, _, _ = setup(
        tmp_path, [result(source), result(empty), result(_details("analysis.pdf"))]
    )
    rid = _begin(bridge)
    found, _ = invoke(bridge, "search", {"researchId": rid, "query": "discovery"})
    before = store.snapshot(rid)
    state_path = store.private_root / rid / "state.json"
    original_bytes = state_path.read_bytes()
    rejected, envelope = _write(bridge, rid, _claims(found), single=single)
    assert envelope["result"]["isError"] is True
    checks = _checks(rejected)
    assert {check["code"] for check in checks} == {
        "SCOPED_SEARCH_REQUIRED",
        "EVIDENCE_READ_REQUIRED",
        "PDF_TABLE_INSPECTION_REQUIRED",
    }
    assert checks[0]["suggestedSelection"] == {
        "selection": {"kind": "files", "refs": [found["results"][0]["fileRef"]]}
    }
    assert store.snapshot(rid) == before
    assert state_path.read_bytes() == original_bytes
    assert not store.output_root.exists()

    scoped, _ = invoke(
        bridge,
        "searchByIds",
        {"researchId": rid, "query": "counterevidence", "scopeRefs": [found["scopeRef"]]},
    )
    assert scoped["results"] == []
    for check in checks[1:]:
        response, envelope = invoke(bridge, check["tool"], check["arguments"])
        assert envelope["result"]["isError"] is False, response
    accepted, _ = _write(bridge, rid, _claims(found), single=single)
    assert accepted["insertedCount"] == 1
    after = store.snapshot(rid)
    replay, _ = _write(bridge, rid, _claims(found), single=single)
    assert replay == accepted
    assert store.snapshot(rid) == after


def test_read_gate_requires_contiguous_explicit_read_and_supplies_working_resume_cursor(
    tmp_path: Path,
) -> None:
    source = _source(["analysis.md"], "Long source passage with qualifications. " * 2_000)
    bridge, store, _, _ = setup(
        tmp_path, [result(source), result(search_payload("context", [], scoped=True))]
    )
    rid = _begin(bridge)
    found, _ = invoke(bridge, "search", {"researchId": rid, "query": "discovery"})
    invoke(
        bridge,
        "searchByIds",
        {"researchId": rid, "query": "context", "scopeRefs": [found["scopeRef"]]},
    )
    evidence_ref = found["results"][0]["evidenceRef"]
    first, _ = invoke(
        bridge, "researchReadEvidence", {"researchId": rid, "evidenceRef": evidence_ref}
    )
    assert first["nextCursor"]
    nav = Navigation(store.snapshot(rid))
    last_cursor = nav.cursor(first["snapshotRef"], len(source["results"][0]["content"]) - 10)
    last, _ = invoke(
        bridge,
        "researchReadEvidence",
        {"researchId": rid, "evidenceRef": evidence_ref, "cursor": last_cursor},
    )
    assert last["nextCursor"] is None
    rejected, _ = _write(bridge, rid, _claims(found))
    (check,) = _checks(rejected)
    assert check["code"] == "EVIDENCE_READ_REQUIRED"
    assert check["arguments"]["cursor"] == first["nextCursor"]
    assert check["arguments"]["evidenceRef"] == evidence_ref
    resumed, envelope = invoke(bridge, check["tool"], check["arguments"])
    assert envelope["result"]["isError"] is False
    while resumed.get("nextCursor"):
        resumed, _ = invoke(
            bridge,
            "researchReadEvidence",
            {"researchId": rid, "evidenceRef": evidence_ref, "cursor": resumed["nextCursor"]},
        )
    accepted, _ = _write(bridge, rid, _claims(found))
    assert accepted["status"] == "accepted"


def test_bibliography_metadata_and_failed_explicit_details_cannot_satisfy_pdf_gate(
    tmp_path: Path,
) -> None:
    source = _source(["analysis.pdf"])
    bridge, store, upstream, _ = setup(
        tmp_path, [result(source), result(search_payload("context", [], scoped=True))]
    )
    rid = _begin(bridge)
    found, _ = invoke(bridge, "search", {"researchId": rid, "query": "discovery"})
    invoke(
        bridge,
        "searchByIds",
        {"researchId": rid, "query": "context", "scopeRefs": [found["scopeRef"]]},
    )
    _read_all(bridge, rid, found["results"][0]["evidenceRef"])
    store.record_knowledge_call(
        research_id=rid,
        tool_name="getFileDetails",
        arguments={"fileId": "analysis.pdf"},
        result=result(_details("analysis.pdf"))["result"],
        metadata_only=True,
    )
    # Metadata may be verified and complete upstream; its purpose still is not inspection.
    upstream.responses.append({"result": {"isError": True, "content": []}})
    invoke(bridge, "getFileDetails", {"researchId": rid, "fileRef": found["results"][0]["fileRef"]})
    rejected, _ = _write(bridge, rid, _claims(found))
    (check,) = _checks(rejected)
    assert check["code"] == "PDF_TABLE_INSPECTION_REQUIRED"
    upstream.responses.append(result(_details("analysis.pdf")))
    invoke(bridge, check["tool"], check["arguments"])
    assert _write(bridge, rid, _claims(found))[0]["status"] == "accepted"


def test_mixed_first_batch_requires_each_cited_excerpt_but_no_uncited_files(tmp_path: Path) -> None:
    source = _source(["first.md", "second.md", "uncited.pdf"])
    second_discovery = search_payload("mechanism", [])
    third_discovery = search_payload("risk", [])
    bridge, store, _, _ = setup(
        tmp_path,
        [
            result(source),
            result(search_payload("context", [], scoped=True)),
            result(second_discovery),
            result(third_discovery),
            result(search_payload("counter", [], scoped=True)),
        ],
    )
    rid = _begin(bridge)
    found, _ = invoke(bridge, "search", {"researchId": rid, "query": "discovery"})
    invoke(
        bridge,
        "searchByIds",
        {"researchId": rid, "query": "context", "scopeRefs": [found["scopeRef"]]},
    )
    invoke(bridge, "search", {"researchId": rid, "query": "mechanism"})
    invoke(bridge, "search", {"researchId": rid, "query": "risk"})
    invoke(
        bridge,
        "searchByIds",
        {"researchId": rid, "query": "counter", "scopeRefs": [found["scopeRef"]]},
    )
    for row in found["results"]:
        _read_all(bridge, rid, row["evidenceRef"])
    assert _write(bridge, rid, _claims(found)[:2])[0]["insertedCount"] == 2


def test_grouping_and_failed_scoped_calls_are_not_search_preparation(tmp_path: Path) -> None:
    source = _source([f"file-{index}.md" for index in range(21)])
    bridge, store, upstream, _ = setup(
        tmp_path,
        [
            result(source),
            result(search_payload("mechanism", [])),
            result(search_payload("risk", [])),
            result(search_payload("context", [], scoped=True)),
            {"result": {"isError": True, "content": []}},
        ],
    )
    rid = _begin(bridge)
    found, _ = invoke(bridge, "search", {"researchId": rid, "query": "discovery"})
    invoke(bridge, "search", {"researchId": rid, "query": "mechanism"})
    invoke(bridge, "search", {"researchId": rid, "query": "risk"})
    grouped, _ = invoke(
        bridge,
        "searchByIds",
        {"researchId": rid, "query": "context", "scopeRefs": [found["scopeRef"]]},
    )
    assert "groups" in grouped
    _read_all(bridge, rid, found["results"][0]["evidenceRef"])
    invoke(
        bridge,
        "searchByIds",
        {"researchId": rid, "query": "context", "fileRefs": [found["results"][0]["fileRef"]]},
    )
    rejected, _ = _write(bridge, rid, _claims(found)[:1])
    checks = _checks(rejected)
    assert "SCOPED_READING_BREADTH_REQUIRED" in {check["code"] for check in checks}
    assert "SOURCE_READING_BREADTH_REQUIRED" in {check["code"] for check in checks}
    assert store.snapshot(rid)["report"]["items"] == []


def test_standard_and_existing_drafts_keep_their_compatibility(tmp_path: Path) -> None:
    bridge, store, _, rid = setup(tmp_path, [result(_source(["analysis.pdf"]))])
    found, _ = invoke(bridge, "search", {"researchId": rid, "query": "discovery"})
    claims = _claims(found)
    accepted, _ = _write(bridge, rid, claims)
    assert accepted["status"] == "accepted"
    store.atomic_update(rid, lambda state: state.update(mode="deep"))
    new = {**claims[0], "claimKey": "legacy-followup"}
    appended, _ = invoke(
        bridge, "researchAddClaims", {"researchId": rid, "claims": [new], "batchKey": "append"}
    )
    assert appended["insertedCount"] == 1
