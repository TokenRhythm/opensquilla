"""Observable first-draft prerequisites, not research or semantic certification."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

if __package__:
    from .claims import ResearchStateError, sha256_json
    from .references import build_bibliography, source_format
else:  # pragma: no cover - standalone bridge
    from claims import ResearchStateError, sha256_json  # type: ignore[import-not-found,no-redef]
    from references import (  # type: ignore[import-not-found,no-redef]
        build_bibliography,
        source_format,
    )


MAX_BIBLIOGRAPHY_SOURCES = 30
MAX_EXPLICIT_READ_SOURCES = 24
MAX_REPORT_TABLES = 5


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


def bibliography_breadth_target(state: Mapping[str, Any]) -> int:
    """Return the adaptive number of distinct references expected for deep work."""

    if state.get("mode") != "deep":
        return 0
    candidate_count = len(_discovered_file_ids(state))
    # Small investigations should retain the existing evidence/review flow;
    # bibliography breadth is a safeguard for genuinely broad corpora.
    if candidate_count < 10:
        return 0
    return min(MAX_BIBLIOGRAPHY_SOURCES, candidate_count)


def explicit_read_breadth_target(state: Mapping[str, Any]) -> int:
    """Require a meaningful core reading set without forcing every candidate open."""

    target = bibliography_breadth_target(state)
    # Narrow single-file investigations already have the ordinary cited-evidence
    # gate. The additional core-reading breadth rule is for genuinely broad work.
    if target < 10:
        return 0
    # A broad report should be built from a real core reading set.  Keep the
    # floor at twelve for a 30-source bibliography and scale toward 80% for
    # smaller corpora, while preserving a bounded upper limit.
    return min(MAX_EXPLICIT_READ_SOURCES, max(12, (target * 4 + 4) // 5))


def report_table_breadth_target(state: Mapping[str, Any]) -> int:
    """Return the adaptive number of source-grounded data exhibits expected."""

    if state.get("mode") != "deep":
        return 0
    candidate_count = len(_discovered_file_ids(state))
    if candidate_count < 10:
        return 0
    # Three exhibits make a broad report comparable and concrete; very large
    # corpora earn up to five so the report can cover index data, mechanisms,
    # scenarios, risks and company/segment evidence without padding.
    return min(MAX_REPORT_TABLES, max(3, (candidate_count + 9) // 10))


def _report_table_stats(state: Mapping[str, Any]) -> tuple[int, int]:
    items = state.get("report", {}).get("items", [])
    tables = state.get("ledger", {}).get("tables", {})
    source_ids: set[str] = set()
    count = 0
    for item in items:
        if not isinstance(item, Mapping) or item.get("kind") != "table":
            continue
        count += 1
        table = tables.get(item.get("tableId"), {})
        if isinstance(table, Mapping) and table.get("fileId"):
            source_ids.add(str(table["fileId"]))
    return count, len(source_ids)


def bibliography_breadth_summary(state: Mapping[str, Any]) -> dict[str, Any]:
    bibliography = build_bibliography(state)
    candidate_count = len(_discovered_file_ids(state))
    target = bibliography_breadth_target(state)
    read_files = _complete_read_file_ids(state)
    read_target = explicit_read_breadth_target(state)
    table_count, table_source_count = _report_table_stats(state)
    table_target = report_table_breadth_target(state)
    return {
        "schemaVersion": "research-breadth-audit/2",
        "status": (
            "ready"
            if bibliography["sourceCount"] >= target
            and len(read_files) >= read_target
            and table_count >= table_target
            and table_source_count >= table_target
            else "insufficient"
        ),
        "candidateSourceFiles": candidate_count,
        "bibliographyEntries": bibliography["sourceCount"],
        "citedSourceFiles": bibliography["sourceFileCount"],
        "explicitlyReadSourceFiles": len(read_files),
        "reportTableExhibits": table_count,
        "reportTableSourceFiles": table_source_count,
        "minimums": {
            "bibliographyEntries": target,
            "explicitlyReadSourceFiles": read_target,
            "reportTableExhibits": table_target,
            "reportTableSourceFiles": table_target,
        },
    }


def bibliography_breadth_check(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """Block finalization when a broad corpus was reduced to a tiny citation set."""

    return next(
        (
            check
            for check in report_breadth_checks(state)
            if check["code"] == "BIBLIOGRAPHY_BREADTH_REQUIRED"
        ),
        None,
    )


def report_breadth_checks(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return finalization checks for bibliography, reading and exhibit breadth."""

    if state.get("mode") != "deep" or not state.get("report", {}).get("items"):
        return []
    summary = bibliography_breadth_summary(state)
    missing_refs = max(
        0,
        summary["minimums"]["bibliographyEntries"] - summary["bibliographyEntries"],
    )
    missing_reads = max(
        0,
        summary["minimums"]["explicitlyReadSourceFiles"] - summary["explicitlyReadSourceFiles"],
    )
    missing_tables = max(
        0,
        summary["minimums"]["reportTableExhibits"] - summary["reportTableExhibits"],
    )
    missing_table_sources = max(
        0,
        summary["minimums"]["reportTableSourceFiles"] - summary["reportTableSourceFiles"],
    )

    checks: list[dict[str, Any]] = []
    if missing_refs or missing_reads:
        navigation = state.get("extensions", {}).get("navigation", {})
        cited: set[str] = set()
        for item in state["report"]["items"]:
            if item.get("kind") == "claim":
                cited.update(
                    str(state["ledger"]["evidence"][evidence_id]["fileId"])
                    for evidence_id in item.get("evidenceIds", [])
                )
            elif item.get("kind") == "table":
                table = state["ledger"]["tables"].get(item.get("tableId"), {})
                if table.get("fileId"):
                    cited.add(str(table["fileId"]))
        candidate_refs = [
            str(row["ref"])
            for file_id, row in navigation.get("files", {}).items()
            if file_id not in cited and isinstance(row.get("ref"), str)
        ]
        completed_files = _complete_read_file_ids(state)
        unread_cited_refs = [
            str(row["ref"])
            for file_id, row in navigation.get("files", {}).items()
            if file_id in cited
            and file_id not in completed_files
            and isinstance(row.get("ref"), str)
        ]
        reasons: list[str] = []
        if missing_refs:
            reasons.append(
                f"cite at least {missing_refs} more independent source file(s) in supported claims"
            )
        if missing_reads:
            reasons.append(
                f"complete explicit reading for {missing_reads} more core source file(s)"
            )
        check: dict[str, Any] = {
            "code": "BIBLIOGRAPHY_BREADTH_REQUIRED",
            "tool": "searchByIds",
            "arguments": {
                "researchId": state["researchId"],
                "query": (
                    "Extract the main finding, assumptions, key numbers, qualifications, "
                    "and contrary evidence relevant to the report question."
                ),
            },
            "required": {
                "bibliographyEntries": summary["minimums"]["bibliographyEntries"],
                "explicitlyReadSourceFiles": summary["minimums"]["explicitlyReadSourceFiles"],
            },
            "observed": {
                "bibliographyEntries": summary["bibliographyEntries"],
                "explicitlyReadSourceFiles": summary["explicitlyReadSourceFiles"],
            },
            "message": (
                "The discovered corpus is broad but the draft cites too few independent sources. "
                + "; ".join(reasons)
                + ". Use returned evidenceRefs in substantive claims; do not add a bare or "
                "unread bibliography list."
            ),
        }
        selection_refs = candidate_refs if missing_refs else unread_cited_refs
        if selection_refs:
            check["suggestedSelection"] = {
                "selection": {"kind": "files", "refs": selection_refs[:20]}
            }
        checks.append(check)

    if missing_tables or missing_table_sources:
        navigation = state.get("extensions", {}).get("navigation", {})
        refs = [
            str(row["ref"])
            for row in navigation.get("files", {}).values()
            if isinstance(row.get("ref"), str)
        ]
        checks.append(
            {
                "code": "REPORT_TABLE_BREADTH_REQUIRED",
                "tool": "searchByIds",
                "arguments": {
                    "researchId": state["researchId"],
                    "query": (
                        "Find quantitative comparisons, index or company data, earnings, "
                        "valuation, scenarios, mechanisms and risks that can become "
                        "source-grounded report tables."
                    ),
                },
                "required": {
                    "reportTableExhibits": summary["minimums"]["reportTableExhibits"],
                    "reportTableSourceFiles": summary["minimums"]["reportTableSourceFiles"],
                },
                "observed": {
                    "reportTableExhibits": summary["reportTableExhibits"],
                    "reportTableSourceFiles": summary["reportTableSourceFiles"],
                },
                "message": (
                    "The report needs more original-source data exhibits before it can be "
                    f"finalized: add {max(missing_tables, missing_table_sources)} more table(s) "
                    "from distinct source files where possible. Inspect complete inventories, "
                    "fetch the relevant tables and original crops, and add them with "
                    "mcp_researchAddTable. Every table must include units, period, estimate "
                    "status and a precise source; never fabricate or pad a table."
                ),
                "suggestedSelection": {"selection": {"kind": "files", "refs": refs[:20]}},
            }
        )
    return checks


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
    breadth = bibliography_breadth_summary(state)
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
        "bibliographyBreadth": breadth,
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
