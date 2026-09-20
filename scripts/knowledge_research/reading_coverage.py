"""Bounded, read-only measurement of source text projected to a research task."""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from statistics import median
from typing import Any

if __package__:
    from .references import build_bibliography
else:  # pragma: no cover - direct bridge entrypoint
    from references import build_bibliography  # type: ignore[import-not-found,no-redef]

SCHEMA = "bibliography-reading-coverage/1"
MAX_DOCUMENT_CHUNKS = 20_000
QUERY_SECONDS = 10.0


class _UnavailableError(ValueError):
    pass


def _interval(start: Any, end: Any) -> tuple[int, int]:
    if type(start) is not int or type(end) is not int or not 0 <= start <= end:
        raise _UnavailableError("invalid_source_offsets")
    return start, end


def _union_size(ranges: list[tuple[int, int]]) -> int:
    right = total = 0
    for start, end in sorted(ranges):
        total += max(0, end - max(start, right))
        right = max(right, end)
    return total


def _projections(state: Mapping[str, Any]) -> tuple[dict[str, list[tuple[int, int]]], set[str]]:
    nav = state.get("extensions", {}).get("navigation")
    if not isinstance(nav, Mapping):
        raise _UnavailableError("projection_history_unavailable")
    evidence = state["ledger"]["evidence"]
    refs = {row["ref"]: key for key, row in nav.get("evidence", {}).items()}
    ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    invalid: set[str] = set()

    def add(key: str, start: Any, end: Any, content: Any = None) -> None:
        if key not in evidence:
            raise _UnavailableError("projection_source_missing")
        row = evidence[key]
        try:
            left, right = _interval(start, end)
            if right > len(row["content"]):
                raise _UnavailableError("invalid_projection_range")
            if content is not None and content != row["content"][left:right]:
                raise _UnavailableError("projection_content_mismatch")
            ranges[key].append((left, right))
        except _UnavailableError:
            invalid.add(row["fileId"])

    for projection in nav.get("projections", {}).values():
        if projection.get("projectionPrepared") is not True:
            continue
        snapshot = nav.get("snapshots", {}).get(projection.get("snapshotRef"))
        if not isinstance(snapshot, Mapping):
            raise _UnavailableError("projection_snapshot_missing")
        if snapshot["kind"] in {"search", "evidence"}:
            for span in projection.get("contentRanges", []):
                add(span["evidenceId"], span["start"], span["end"])
        elif snapshot["payload"].get("view") == "review":
            page = projection["range"]
            left, right = _interval(page["start"], page["end"])
            entries = snapshot["payload"]["entries"]
            if right > len(entries):
                raise _UnavailableError("invalid_review_projection")
            for entry in entries[left:right]:
                if entry.get("kind") != "evidence":
                    continue
                key = refs.get(entry.get("evidenceRef"))
                if key is None:
                    raise _UnavailableError("projection_source_missing")
                span = entry["contentRange"]
                add(key, span["start"], span["end"], entry["content"])
    return dict(ranges), invalid


class SQLiteReadingCoverage:
    def __init__(self, database: str | Path) -> None:
        self.database = Path(database).expanduser()

    def __call__(self, state: Mapping[str, Any]) -> dict[str, Any]:
        bibliography = build_bibliography(state)
        measured: dict[str, dict[str, Any]] = {}
        connection: sqlite3.Connection | None = None
        unavailable: str | None = None
        deadline = time.monotonic() + QUERY_SECONDS
        try:
            ranges, invalid = _projections(state)
            path = self.database.resolve(strict=True)
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
            connection.execute("PRAGMA query_only=ON")
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            connection.execute("BEGIN")
        except _UnavailableError as exc:
            unavailable = str(exc)
        except (KeyError, TypeError, ValueError):
            unavailable = "invalid_projection_history"
        except (OSError, sqlite3.Error):
            unavailable = "coverage_database_unavailable"
        try:
            for reference in bibliography["references"]:
                for member in reference["members"]:
                    file_id = member["fileId"]
                    if file_id in measured:
                        continue
                    result = {
                        "fileId": file_id,
                        "documentId": member["documentId"],
                        "revision": member["revision"],
                        "status": "unavailable",
                        "returnedSourceChars": None,
                        "indexedSourceChars": None,
                    }
                    try:
                        if unavailable:
                            raise _UnavailableError(unavailable)
                        if time.monotonic() >= deadline:
                            raise _UnavailableError("coverage_query_budget_exceeded")
                        if file_id in invalid:
                            raise _UnavailableError("invalid_source_projection")
                        assert connection is not None
                        result.update(self._measure(connection, state, member, ranges))
                    except _UnavailableError as exc:
                        result["reason"] = str(exc)
                    except sqlite3.Error:
                        result["reason"] = "coverage_query_unavailable"
                    except (KeyError, TypeError, ValueError):
                        result["reason"] = "invalid_source_metadata"
                    measured[file_id] = result
        finally:
            if connection is not None:
                connection.close()

        references = []
        for reference in bibliography["references"]:
            members = list(
                {m["fileId"]: measured[m["fileId"]] for m in reference["members"]}.values()
            )
            known = all(m["status"] == "available" for m in members)
            read = sum(m["returnedSourceChars"] for m in members) if known else None
            total = sum(m["indexedSourceChars"] for m in members) if known else None
            references.append(
                {
                    "number": reference["number"],
                    "status": "available" if known else "unavailable",
                    "percentage": round(100 * read / total, 2)
                    if total and read is not None
                    else None,
                    "returnedSourceChars": read,
                    "indexedSourceChars": total,
                    "members": members,
                }
            )
        available = [
            row
            for row in references
            if row["status"] == "available"
            and isinstance(row.get("percentage"), int | float)
            and not isinstance(row.get("percentage"), bool)
        ]
        percentages = [float(row["percentage"]) for row in available]
        returned = [row["returnedSourceChars"] for row in available]
        indexed = [row["indexedSourceChars"] for row in available]
        total_returned = sum(returned)
        total_indexed = sum(indexed)
        return {
            "schemaVersion": SCHEMA,
            "method": "union_projected_source_chars_over_union_indexed_child_chars",
            "meaning": (
                "Tool-projected text, not proof of model comprehension; table OCR excluded. "
                "Format variants retain separate coordinate spaces."
            ),
            "summary": {
                "bibliographyEntries": len(references),
                "availableReferences": len(available),
                "unavailableReferences": len(references) - len(available),
                "returnedSourceChars": total_returned,
                "indexedSourceChars": total_indexed,
                "overallPercentage": round(100 * total_returned / total_indexed, 2)
                if total_indexed
                else None,
                "medianPercentage": round(float(median(percentages)), 2) if percentages else None,
                "below5pctReferences": sum(value < 5 for value in percentages),
                "below10pctReferences": sum(value < 10 for value in percentages),
                "fullTextCoverageReferences": sum(value >= 100 for value in percentages),
            },
            "references": references,
        }

    @staticmethod
    def _measure(
        connection: sqlite3.Connection,
        state: Mapping[str, Any],
        member: Mapping[str, Any],
        projected: Mapping[str, list[tuple[int, int]]],
    ) -> dict[str, Any]:
        document = connection.execute(
            "SELECT source_file_id,content_sha256 FROM documents WHERE document_id=?",
            (member["documentId"],),
        ).fetchone()
        if document is None or document != (member["fileId"], member["revision"]):
            raise _UnavailableError("document_revision_mismatch")
        rows = connection.execute(
            "SELECT chunk_id,char_start,char_end,json_extract(metadata_json,'$.chunkRole'),"
            "json_extract(metadata_json,'$.chunkPolicyId'),json_extract(metadata_json,'$.indexVersion'),"
            "json_extract(metadata_json,'$.revision') "
            "FROM chunks INDEXED BY idx_chunks_document_ordinal WHERE document_id=? LIMIT ?",
            (member["documentId"], MAX_DOCUMENT_CHUNKS + 1),
        ).fetchall()
        if len(rows) > MAX_DOCUMENT_CHUNKS:
            raise _UnavailableError("document_chunk_budget_exceeded")
        chunks: dict[str, tuple[int, int]] = {}
        for key, start, end, role, policy, index, revision in rows:
            if role == "parent":
                continue
            if (
                role != "child"
                or policy != "hierarchical_token_v4"
                or index != "knowledge-index-v5"
                or revision != member["revision"]
            ):
                raise _UnavailableError("chunk_policy_mismatch")
            chunks[key] = _interval(start, end)
        total = _union_size(list(chunks.values()))
        if not total:
            raise _UnavailableError("indexed_text_unavailable")
        read_ranges: list[tuple[int, int]] = []
        for key, spans in projected.items():
            evidence = state["ledger"]["evidence"][key]
            if evidence["fileId"] != member["fileId"]:
                continue
            if evidence.get("verificationStatus") != "verified" or (
                evidence.get("revision") != member["revision"]
                or evidence.get("documentId") != member["documentId"]
            ):
                raise _UnavailableError("evidence_revision_mismatch")
            locator = evidence.get("locator", {})
            start, end = _interval(locator.get("charStart"), locator.get("charEnd"))
            if end - start != len(evidence["content"]) or chunks.get(evidence.get("chunkId")) != (
                start,
                end,
            ):
                raise _UnavailableError("evidence_offsets_mismatch")
            read_ranges.extend((start + left, start + right) for left, right in spans)
        return {
            "status": "available",
            "returnedSourceChars": _union_size(read_ranges),
            "indexedSourceChars": total,
        }
