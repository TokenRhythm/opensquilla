"""Observable first-draft prerequisites, not research or semantic certification."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

if __package__:
    from .claims import ResearchStateError, sha256_json
    from .references import source_format
else:  # pragma: no cover - standalone bridge
    from claims import ResearchStateError, sha256_json  # type: ignore[import-not-found,no-redef]
    from references import source_format  # type: ignore[import-not-found,no-redef]


def scoped_search_count(state: Mapping[str, Any]) -> int:
    return sum(
        call.get("toolName") == "searchByIds" and call.get("verificationStatus") == "verified"
        for call in state["ledger"]["calls"]
    )


def _verified_evidence(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        record
        for record in state["ledger"]["evidence"].values()
        if isinstance(record, Mapping) and record.get("verificationStatus") == "verified"
    ]


def _discovered_file_ids(state: Mapping[str, Any]) -> list[str]:
    return list(dict.fromkeys(str(record["fileId"]) for record in _verified_evidence(state)))


def _discovery_query_count(state: Mapping[str, Any]) -> int:
    queries = {
        str(call.get("arguments", {}).get("query", "")).strip().casefold()
        for call in state["ledger"]["calls"]
        if call.get("toolName") == "search"
        and call.get("verificationStatus") == "verified"
        and str(call.get("arguments", {}).get("query", "")).strip()
    }
    return len(queries)


def _complete_read_file_ids(state: Mapping[str, Any]) -> set[str]:
    navigation = state.get("extensions", {}).get("navigation", {})
    completed: set[str] = set()
    for record in _verified_evidence(state):
        covered, _ = _read_gap(navigation, record)
        if covered == len(record.get("content", "")):
            completed.add(str(record["fileId"]))
    return completed


def _breadth_checks(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Require a small, adaptive minimum of independent research activity.

    The floor scales down for a genuinely narrow corpus. It is deliberately
    based on server-observed calls and projections rather than model claims.
    This prevents a long bibliography made from one shallow search from
    satisfying ``mode=deep`` while keeping one-file investigations usable.
    """

    discovered = _discovered_file_ids(state)
    corpus_size = len(discovered)
    if not corpus_size:
        return []
    checks: list[dict[str, Any]] = []
    required_queries = min(3, corpus_size)
    actual_queries = _discovery_query_count(state)
    if actual_queries < required_queries:
        checks.append(
            {
                "code": "DISCOVERY_BREADTH_REQUIRED",
                "tool": "search",
                "arguments": {"researchId": state["researchId"]},
                "required": required_queries,
                "observed": actual_queries,
                "message": (
                    "Deep research needs separate discovery questions before drafting. "
                    f"Use {required_queries - actual_queries} more focused search angle(s), "
                    "such as definitions, mechanisms, data, disagreement or outlook."
                ),
            }
        )

    required_scoped = min(2, corpus_size)
    actual_scoped = scoped_search_count(state)
    # The legacy single-scoped-search check below supplies the exact candidate
    # selection when no scoped search has happened yet. Once that first pass is
    # complete, this check asks for a second angle on a multi-file corpus.
    if actual_scoped and actual_scoped < required_scoped:
        checks.append(
            {
                "code": "SCOPED_READING_BREADTH_REQUIRED",
                "tool": "searchByIds",
                "arguments": {"researchId": state["researchId"]},
                "required": required_scoped,
                "observed": actual_scoped,
                "message": (
                    "Deep research needs more than one scoped pass over candidate files. "
                    f"Run {required_scoped - actual_scoped} more focused scoped pass(es) "
                    "to test context, qualifications and an alternative explanation."
                ),
            }
        )

    required_read_files = min(3, corpus_size)
    completed_files = _complete_read_file_ids(state)
    if required_read_files > 1 and len(completed_files) < required_read_files:
        refs_by_file: dict[str, str] = {}
        navigation = state.get("extensions", {}).get("navigation", {})
        for evidence_id, row in navigation.get("evidence", {}).items():
            file_id = state["ledger"]["evidence"].get(evidence_id, {}).get("fileId")
            evidence_ref = row.get("ref")
            if (
                isinstance(file_id, str)
                and isinstance(evidence_ref, str)
                and file_id not in completed_files
            ):
                refs_by_file.setdefault(file_id, evidence_ref)
        read_checks = 0
        for file_id in discovered:
            if file_id in completed_files:
                continue
            arguments: dict[str, Any] = {"researchId": state["researchId"]}
            evidence_ref = refs_by_file.get(file_id)
            if evidence_ref:
                arguments["evidenceRef"] = evidence_ref
            checks.append(
                {
                    "code": "SOURCE_READING_BREADTH_REQUIRED",
                    "tool": "researchReadEvidence",
                    "arguments": arguments,
                    "required": required_read_files,
                    "observed": len(completed_files),
                    "message": (
                        "Read complete relevant passages from this additional candidate source "
                        "before drafting. A discovered source that was not explicitly read "
                        "must not be treated as supporting or opposing evidence."
                    ),
                }
            )
            read_checks += 1
            if read_checks >= required_read_files - len(completed_files):
                break
    return checks


def research_depth_summary(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the server-observed depth audit used by the deep-write gate."""

    discovered = _discovered_file_ids(state)
    corpus_size = len(discovered)
    required_queries = min(3, corpus_size) if corpus_size else 0
    required_scoped = min(2, corpus_size) if corpus_size else 0
    required_read_files = min(3, corpus_size) if corpus_size else 0
    discovery_queries = _discovery_query_count(state)
    scoped = scoped_search_count(state)
    read_files = _complete_read_file_ids(state)
    return {
        "schemaVersion": "research-depth-audit/1",
        "status": (
            "ready"
            if discovery_queries >= required_queries
            and scoped >= required_scoped
            and len(read_files) >= required_read_files
            else "insufficient"
        ),
        "discoverySearches": discovery_queries,
        "scopedSearches": scoped,
        "discoveredSourceFiles": corpus_size,
        "explicitlyReadSourceFiles": len(read_files),
        "minimums": {
            "discoverySearches": required_queries,
            "scopedSearches": required_scoped,
            "explicitlyReadSourceFiles": required_read_files,
        },
    }


def scoped_search_check() -> dict[str, Any]:
    return {
        "code": "SCOPED_SEARCH_REQUIRED",
        "tool": "searchByIds",
        "message": (
            "Deep research has not searched within any candidate file. Use selection "
            "with returned scopes or files for context, assumptions or "
            "counterevidence. A verified empty-result search counts; grouping does not."
        ),
    }


def _read_gap(navigation: Mapping[str, Any], record: Mapping[str, Any]) -> tuple[int, str | None]:
    """Return the first unprojected offset using only explicit evidence-read snapshots."""
    intervals: list[tuple[int, int]] = []
    resume_snapshot = None
    for projection in navigation.get("projections", {}).values():
        if projection.get("projectionPrepared") is not True:
            continue
        snapshot_ref = projection.get("snapshotRef")
        snapshot = navigation.get("snapshots", {}).get(snapshot_ref, {})
        payload = snapshot.get("payload", {})
        if (
            snapshot.get("kind") != "evidence"
            or snapshot.get("orderedIds") != [record["evidenceId"]]
            or snapshot.get("sourceRevisions") != [record["revision"]]
            or payload.get("contentSha256") != record["contentSha256"]
        ):
            continue
        page = projection.get("range", {})
        left, right = page.get("start"), page.get("end")
        if type(left) is int and type(right) is int and 0 <= left < right <= len(record["content"]):
            intervals.append((left, right))
            resume_snapshot = str(snapshot_ref)
    covered = 0
    for left, right in sorted(intervals):
        if left > covered:
            break
        covered = max(covered, right)
    return covered, resume_snapshot


def require_first_write_preparation(
    state: Mapping[str, Any], claims: Sequence[Mapping[str, Any]]
) -> None:
    if state.get("mode") != "deep" or any(
        item.get("kind") == "claim" for item in state["report"]["items"]
    ):
        return

    navigation = state.get("extensions", {}).get("navigation", {})
    ledger = state["ledger"]
    evidence_ids = list(dict.fromkeys(key for item in claims for key in item["evidenceIds"]))
    file_ids = list(dict.fromkeys(ledger["evidence"][key]["fileId"] for key in evidence_ids))
    checks = _breadth_checks(state)
    if not scoped_search_count(state):
        check = scoped_search_check()
        file_refs = [
            navigation["files"][key]["ref"]
            for key in file_ids
            if key in navigation.get("files", {})
        ]
        if file_refs:
            check["suggestedSelection"] = {"selection": {"kind": "files", "refs": file_refs[:20]}}
        checks.append(check)

    for evidence_id in evidence_ids:
        record = ledger["evidence"][evidence_id]
        covered, snapshot_ref = _read_gap(navigation, record)
        if covered == len(record["content"]):
            continue
        arguments: dict[str, Any] = {"researchId": state["researchId"]}
        evidence_ref = navigation.get("evidence", {}).get(evidence_id, {}).get("ref")
        if evidence_ref:
            arguments["evidenceRef"] = evidence_ref
        if snapshot_ref:
            # Same signed offset format as Navigation.cursor; resume at the first gap.
            digest = sha256_json(
                [snapshot_ref, navigation["snapshots"][snapshot_ref]["hash"], covered]
            )
            arguments["cursor"] = f"{snapshot_ref}:{covered}:{digest[:24]}"
        checks.append(
            {
                "code": "EVIDENCE_READ_REQUIRED",
                "tool": "researchReadEvidence",
                "arguments": arguments,
                "message": (
                    "Read this first-batch cited excerpt and its continuations before writing. "
                    "Discovery and review projections do not replace explicit evidence reading. "
                    "Complete projection does not certify comprehension."
                ),
            }
        )

    explicit_files = {
        call["arguments"].get("fileId")
        for call in ledger["calls"]
        if call.get("toolName") == "getFileDetails"
        and call.get("verificationStatus") == "verified"
        and call.get("purpose") != "bibliography_metadata"
    }
    for file_id in file_ids:
        file = {
            **ledger["files"][file_id],
            **navigation.get("fileMetadata", {}).get(file_id, {}),
        }
        if source_format(file) != "PDF" and str(file.get("fileType", "")).lower() != "pdf":
            continue
        inventory = ledger["inventories"].get(file_id, {})
        if file_id in explicit_files and inventory.get("complete") is True:
            continue
        arguments = {"researchId": state["researchId"]}
        file_ref = navigation.get("files", {}).get(file_id, {}).get("ref")
        if file_ref:
            arguments["fileRef"] = file_ref
        checks.append(
            {
                "code": "PDF_TABLE_INSPECTION_REQUIRED",
                "tool": "getFileDetails",
                "arguments": arguments,
                "message": (
                    "Explicitly inspect this first-batch cited PDF's complete table inventory. "
                    "Follow returned pages and fetch relevant usable tables. Automatic "
                    "bibliography metadata does not count; no table quantity is required."
                ),
            }
        )
    if checks:
        raise ResearchStateError(
            "Complete the missing deep-research preparation, then retry this uncommitted batch.",
            details={"code": "RESEARCH_PREPARATION_REQUIRED", "committed": False, "checks": checks},
        )
