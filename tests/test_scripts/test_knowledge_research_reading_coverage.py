from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from scripts.knowledge_research import reading_coverage as module
from scripts.knowledge_research.reading_coverage import SQLiteReadingCoverage


def _state() -> dict[str, Any]:
    evidence = {
        key: {
            "fileId": "file-1",
            "documentId": "doc-1",
            "revision": "a" * 64,
            "chunkId": key,
            "content": "x" * (end - start),
            "verificationStatus": "verified",
            "locator": {"charStart": start, "charEnd": end},
        }
        for key, start, end in [("e1", 0, 60), ("e2", 40, 100)]
    }
    return {
        "ledger": {
            "files": {
                "file-1": {
                    "fileId": "file-1",
                    "documentId": "doc-1",
                    "revision": "a" * 64,
                    "title": "Fixture source",
                    "filename": "report.pdf",
                    "sourcePath": "source/report.pdf",
                }
            },
            "evidence": evidence,
            "tables": {},
        },
        "report": {"items": [{"kind": "claim", "evidenceIds": ["e1"]}]},
        "extensions": {
            "navigation": {
                "evidence": {"e1": {"ref": "E1"}, "e2": {"ref": "E2"}},
                "snapshots": {
                    "S1": {"kind": "search", "payload": {}},
                    "S2": {"kind": "table", "payload": {}},
                },
                "projections": {
                    "P1": {
                        "projectionPrepared": True,
                        "snapshotRef": "S1",
                        "contentRanges": [
                            {"evidenceId": "e1", "start": 0, "end": 60},
                            {"evidenceId": "e2", "start": 0, "end": 40},
                        ],
                    }
                },
            }
        },
    }


def _db(path: Path) -> Path:
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE documents(document_id TEXT PRIMARY KEY,"
            "source_file_id TEXT,content_sha256 TEXT);"
            "CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,document_id TEXT,ordinal INTEGER,"
            "char_start INTEGER,char_end INTEGER,metadata_json TEXT);"
            "CREATE INDEX idx_chunks_document_ordinal ON chunks(document_id,ordinal);"
        )
        db.execute("INSERT INTO documents VALUES(?,?,?)", ("doc-1", "file-1", "a" * 64))
        metadata = {
            "chunkRole": "child",
            "chunkPolicyId": "hierarchical_token_v4",
            "indexVersion": "knowledge-index-v5",
            "revision": "a" * 64,
        }
        db.executemany(
            "INSERT INTO chunks VALUES(?,?,?,?,?,?)",
            [
                (key, "doc-1", i, start, end, json.dumps(metadata))
                for i, (key, start, end) in enumerate(
                    [("e1", 0, 60), ("e2", 40, 100), ("e3", 100, 200)]
                )
            ],
        )
    return path


def _row(path: Path, state: dict[str, Any] | None = None) -> dict[str, Any]:
    result = SQLiteReadingCoverage(path)(state or _state())
    row: dict[str, Any] = result["references"][0]
    return row


def test_overlap_partial_and_repeated_projection_are_deduplicated(tmp_path: Path) -> None:
    path = _db(tmp_path / "knowledge.db")
    before = path.read_bytes()
    state = _state()
    state["extensions"]["navigation"]["projections"]["duplicate"] = copy.deepcopy(
        state["extensions"]["navigation"]["projections"]["P1"]
    )
    row = _row(path, state)
    assert row["status"] == "available"
    assert row["returnedSourceChars"] == 80
    assert row["indexedSourceChars"] == 200
    assert row["percentage"] == 40
    assert path.read_bytes() == before


def test_summary_reports_overall_median_and_low_coverage_counts(tmp_path: Path) -> None:
    result = SQLiteReadingCoverage(_db(tmp_path / "knowledge.db"))(_state())
    assert result["summary"] == {
        "bibliographyEntries": 1,
        "availableReferences": 1,
        "unavailableReferences": 0,
        "returnedSourceChars": 80,
        "indexedSourceChars": 200,
        "overallPercentage": 40.0,
        "medianPercentage": 40.0,
        "below5pctReferences": 0,
        "below10pctReferences": 0,
        "fullTextCoverageReferences": 0,
    }


def test_review_can_supply_missing_source_range_without_inflating_repeats(tmp_path: Path) -> None:
    path = _db(tmp_path / "knowledge.db")
    state = _state()
    nav = state["extensions"]["navigation"]
    nav["snapshots"]["R"] = {
        "kind": "directory",
        "payload": {
            "view": "review",
            "entries": [
                {"kind": "claim", "content": "Not source text"},
                {
                    "kind": "evidence",
                    "evidenceRef": "E2",
                    "content": "x" * 20,
                    "contentRange": {"start": 40, "end": 60},
                },
            ],
        },
    }
    nav["projections"]["R"] = {
        "projectionPrepared": True,
        "snapshotRef": "R",
        "range": {"start": 0, "end": 2},
    }
    assert _row(path, state)["percentage"] == 50
    nav["projections"]["R-repeat"] = copy.deepcopy(nav["projections"]["R"])
    assert _row(path, state)["percentage"] == 50


def test_table_metadata_and_unprojected_search_results_do_not_count(tmp_path: Path) -> None:
    path = _db(tmp_path / "knowledge.db")
    state = _state()
    nav = state["extensions"]["navigation"]
    nav["projections"] = {
        "T": {
            "projectionPrepared": True,
            "snapshotRef": "S2",
            "contentRanges": [{"evidenceId": "e1", "start": 0, "end": 60}],
        }
    }
    assert _row(path, state)["percentage"] == 0


@pytest.mark.parametrize(
    "change", ["document_revision", "chunk_revision", "chunk_policy", "offset", "projection"]
)
def test_stale_or_inconsistent_inputs_are_unavailable(tmp_path: Path, change: str) -> None:
    path = _db(tmp_path / "knowledge.db")
    state = _state()
    with sqlite3.connect(path) as db:
        if change == "document_revision":
            db.execute("UPDATE documents SET content_sha256='different'")
        elif change == "chunk_revision":
            db.execute("UPDATE chunks SET metadata_json=json_set(metadata_json,'$.revision','old')")
        elif change == "chunk_policy":
            db.execute(
                "UPDATE chunks SET metadata_json=json_set(metadata_json,'$.chunkPolicyId','old')"
            )
        elif change == "offset":
            db.execute("UPDATE chunks SET char_end=61 WHERE chunk_id='e1'")
        else:
            state["extensions"]["navigation"]["projections"]["P1"]["contentRanges"][0]["end"] = 1000
    row = _row(path, state)
    assert row["status"] == "unavailable" and row["percentage"] is None


def test_missing_database_does_not_create_it(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    row = _row(path)
    assert row["percentage"] is None and not path.exists()


def test_missing_index_fails_closed_without_table_scan(tmp_path: Path) -> None:
    path = _db(tmp_path / "knowledge.db")
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX idx_chunks_document_ordinal")
    assert _row(path)["percentage"] is None


def test_legacy_missing_projection_history_is_not_assumed_read(tmp_path: Path) -> None:
    path = _db(tmp_path / "knowledge.db")
    state = _state()
    state.pop("extensions")
    assert _row(path, state)["percentage"] is None


def test_query_limits_return_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _db(tmp_path / "knowledge.db")
    monkeypatch.setattr(module, "MAX_DOCUMENT_CHUNKS", 2)
    assert _row(path)["percentage"] is None
    monkeypatch.setattr(module, "MAX_DOCUMENT_CHUNKS", 20000)
    monkeypatch.setattr(module, "QUERY_SECONDS", 0)
    assert _row(path)["percentage"] is None


def test_parent_chunks_are_not_counted(tmp_path: Path) -> None:
    path = _db(tmp_path / "knowledge.db")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO chunks VALUES(?,?,?,?,?,?)",
            ("parent", "doc-1", 99, 0, 10000, '{"chunkRole":"parent"}'),
        )
    assert _row(path)["indexedSourceChars"] == 200


def test_format_variants_keep_separate_coordinates_and_both_denominators(tmp_path: Path) -> None:
    path = _db(tmp_path / "knowledge.db")
    state = _state()
    stem = "2026-09-03 A shared source report with a sufficiently descriptive title"
    first = state["ledger"]["files"]["file-1"]
    first.update(
        sourcePath=f"source/{stem}.pdf", filename=f"{stem}.pdf", verificationStatus="verified"
    )
    second = {
        **first,
        "fileId": "file-2",
        "documentId": "doc-2",
        "revision": "b" * 64,
        "sourcePath": f"source/{stem}.md",
        "filename": f"{stem}.md",
    }
    state["ledger"]["files"]["file-2"] = second
    state["ledger"]["evidence"]["e4"] = {
        **state["ledger"]["evidence"]["e1"],
        "fileId": "file-2",
        "documentId": "doc-2",
        "revision": "b" * 64,
        "chunkId": "e4",
        "content": "y" * 100,
        "locator": {"charStart": 0, "charEnd": 100},
    }
    state["report"]["items"][0]["evidenceIds"].append("e4")
    state["extensions"]["navigation"]["projections"]["P1"]["contentRanges"].append(
        {"evidenceId": "e4", "start": 0, "end": 10}
    )
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO documents VALUES(?,?,?)", ("doc-2", "file-2", "b" * 64))
        metadata = {
            "chunkRole": "child",
            "chunkPolicyId": "hierarchical_token_v4",
            "indexVersion": "knowledge-index-v5",
            "revision": "b" * 64,
        }
        db.execute(
            "INSERT INTO chunks VALUES(?,?,?,?,?,?)",
            ("e4", "doc-2", 0, 0, 100, json.dumps(metadata)),
        )
    result = SQLiteReadingCoverage(path)(state)
    assert len(result["references"]) == 1
    row = result["references"][0]
    assert row["returnedSourceChars"] == 90 and row["indexedSourceChars"] == 300
    assert row["percentage"] == 30


def test_locked_database_returns_without_waiting_for_writer(tmp_path: Path) -> None:
    path = _db(tmp_path / "knowledge.db")
    with sqlite3.connect(path) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        row = _row(path)
        assert row["status"] == "unavailable" and row["percentage"] is None
