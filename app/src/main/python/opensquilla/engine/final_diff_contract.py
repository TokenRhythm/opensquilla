"""Final-diff contract diagnostics for coding-agent runs."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_TEST_PATH_PATTERNS = (
    re.compile(r"(^|/)(test|tests|__tests__)(/|$)", re.I),
    re.compile(r"(^|/)[^/]+\.(spec|test)\.[^/]+$", re.I),
    re.compile(r"(^|/)test_[^/]+\.[^/]+$", re.I),
    re.compile(r"(^|/)[^/]+_test\.[^/]+$", re.I),
    re.compile(r"(^|/)[^/]*test[^/]*\.(py|js|ts|tsx|rb|php|sh|txt|java|go|zsh)$", re.I),
)
_SCRATCH_PATH_PATTERNS = (
    re.compile(r"(^|/)(tmp|temp|scratch)(/|$)", re.I),
    re.compile(r"(^|/)\.?(pytest_cache|mypy_cache|ruff_cache|phpunit\.cache)(/|$)", re.I),
    re.compile(
        r"^[^/]*(debug|repro|reproduce|scratch|verify|inspect|investigate)[^/]*"
        r"\.(py|js|mjs|cjs|ts|rb|php|sh|txt|md|json|yaml|yml|patch|diff|zsh)$",
        re.I,
    ),
    re.compile(r"^[^/]*(debug|repro|reproduce|scratch)[^/]*/", re.I),
    re.compile(r"(^|/)[^/]*\.(patch|diff)$", re.I),
)
_DIAGNOSTIC_SOURCE_LIKE_PATTERNS = (
    re.compile(r"^\.?(php_cs|php-cs|php-cs-fixer)[^/]*\.(php|dist|json|ya?ml)$", re.I),
    re.compile(
        r"^[^/]*(check|verify|inspect|investigate|trace|analy[sz]e|analysis)[^/]*"
        r"\.(py|js|ts|rb|php|sh|txt|md|json|yaml|yml|zsh)$",
        re.I,
    ),
    re.compile(
        r"(^|/)[^/]*(?:[_-](?:test|repro|reproduce|debug|scratch)|"
        r"(?:test|repro|reproduce|debug|scratch)[_-])[^/]*/",
        re.I,
    ),
    re.compile(r"^(?:[^/]+/){7,}[^/]+\.txt$", re.I),
)
_DOC_PATH_PATTERNS = (
    re.compile(r"(^|/)(docs?|documentation|manual)(/|$)", re.I),
    re.compile(r"(^|/)readme(\.[^/]*)?$", re.I),
)
_GENERATED_PATH_PATTERNS = (
    re.compile(r"(^|/)(dist|build|target|generated|gen)(/|$)", re.I),
    re.compile(r"(^|/)[^/]*(generated|prebuilt|bundle|min)\.[^/]+$", re.I),
)


@dataclass(frozen=True)
class FinalDiffContractObservation:
    """Structured summary of whether the current final diff looks actionable."""

    diff_paths: list[str]
    source_paths: list[str]
    scratch_paths: list[str]
    test_like_paths: list[str]
    docs_paths: list[str]
    generated_paths: list[str]
    diagnostic_source_like_paths: list[str]
    actionable_source_paths: list[str]
    candidate_source_paths: list[str]
    candidate_source_missing_paths: list[str]
    candidate_actionable_source_missing_paths: list[str]
    read_source_paths: list[str]
    read_source_missing_paths: list[str]
    read_actionable_source_missing_paths: list[str]
    mutation_overlap_paths: list[str]
    changed_source_receipt_paths: list[str]
    lost_source_mutation_paths: list[str]
    source_diff_candidates: list[dict[str, Any]]
    recoverable_candidate_ids: list[str]
    triggers: list[str]

    @property
    def suspicious(self) -> bool:
        return bool(self.triggers)

    @property
    def primary_reason(self) -> str:
        return self.triggers[0] if self.triggers else "final_diff_contract_ok"

    def to_event_details(self) -> dict[str, Any]:
        return {
            "diff_paths": self.diff_paths,
            "source_paths": self.source_paths,
            "scratch_paths": self.scratch_paths,
            "test_like_paths": self.test_like_paths,
            "docs_paths": self.docs_paths,
            "generated_paths": self.generated_paths,
            "diagnostic_source_like_paths": self.diagnostic_source_like_paths,
            "actionable_source_paths": self.actionable_source_paths,
            "candidate_source_paths": self.candidate_source_paths,
            "candidate_source_missing_paths": self.candidate_source_missing_paths,
            "candidate_actionable_source_missing_paths": (
                self.candidate_actionable_source_missing_paths
            ),
            "read_source_paths": self.read_source_paths,
            "read_source_missing_paths": self.read_source_missing_paths,
            "read_actionable_source_missing_paths": (
                self.read_actionable_source_missing_paths
            ),
            "mutation_overlap_paths": self.mutation_overlap_paths,
            "changed_source_receipt_paths": self.changed_source_receipt_paths,
            "lost_source_mutation_paths": self.lost_source_mutation_paths,
            "source_diff_candidate_count": len(self.source_diff_candidates),
            "recoverable_candidate_ids": self.recoverable_candidate_ids,
            "recoverable_candidate_count": len(self.recoverable_candidate_ids),
            "triggers": self.triggers,
            "source_file_count": len(self.source_paths),
            "scratch_file_count": len(self.scratch_paths),
            "test_like_file_count": len(self.test_like_paths),
            "diagnostic_source_like_count": len(self.diagnostic_source_like_paths),
            "actionable_source_count": len(self.actionable_source_paths),
            "diagnostic_source_like_only": bool(
                self.source_paths and not self.actionable_source_paths
            ),
            "candidate_source_count": len(self.candidate_source_paths),
            "candidate_actionable_source_missing_count": len(
                self.candidate_actionable_source_missing_paths
            ),
            "read_source_count": len(self.read_source_paths),
            "read_source_missing_count": len(self.read_source_missing_paths),
            "read_actionable_source_missing_count": len(
                self.read_actionable_source_missing_paths
            ),
            "mutation_overlap_count": len(self.mutation_overlap_paths),
            "changed_source_receipt_count": len(self.changed_source_receipt_paths),
            "lost_source_mutation_count": len(self.lost_source_mutation_paths),
            "suspicious": self.suspicious,
            "primary_reason": self.primary_reason,
        }


def classify_final_diff_path(relative_path: str) -> str:
    """Classify a changed path for final-patch diagnostics.

    The classifier is intentionally conservative for nested source trees: root-level
    debug/repro/check artifacts are scratch, while paths under source directories
    remain source unless they match standard test/generated/doc locations.
    """

    normalized = _normalize_path(relative_path)
    if not normalized or normalized == "/dev/null":
        return "unknown"
    if any(pattern.search(normalized) for pattern in _TEST_PATH_PATTERNS):
        return "test-like"
    if any(pattern.search(normalized) for pattern in _SCRATCH_PATH_PATTERNS):
        return "scratch"
    if any(pattern.search(normalized) for pattern in _DOC_PATH_PATTERNS):
        return "docs"
    if any(pattern.search(normalized) for pattern in _GENERATED_PATH_PATTERNS):
        return "generated"
    return "source"


def build_final_diff_contract_observation(
    *,
    diff_paths: Sequence[str],
    read_records: Sequence[Mapping[str, Any]] = (),
    write_records: Sequence[Mapping[str, Any]] = (),
    mutation_records: Sequence[Mapping[str, Any]] = (),
    mutation_receipts: Sequence[Mapping[str, Any]] = (),
    source_diff_candidates: Sequence[Mapping[str, Any]] = (),
    known_scratch_paths: Sequence[str] = (),
) -> FinalDiffContractObservation:
    normalized_diff_paths = _unique_paths(diff_paths)
    known_scratch_set = set(_unique_paths(known_scratch_paths))
    by_kind: dict[str, list[str]] = {
        "source": [],
        "scratch": [],
        "test-like": [],
        "docs": [],
        "generated": [],
    }
    for path in normalized_diff_paths:
        kind = "scratch" if path in known_scratch_set else classify_final_diff_path(path)
        if kind in by_kind:
            by_kind[kind].append(path)

    touched_paths = [
        path
        for path in _paths_from_records(
            [*write_records, *mutation_records],
            excluded_classifications=frozenset({"scratch"}),
        )
        if path not in known_scratch_set
    ]
    changed_source_receipts = [
        path
        for path in _changed_source_paths_from_receipts(mutation_receipts)
        if path not in known_scratch_set
    ]
    touched_source_paths = _source_paths_from_records(
        [*write_records, *mutation_records],
        known_scratch_paths=known_scratch_set,
    )
    read_source_paths = _source_paths_from_records(
        read_records,
        known_scratch_paths=known_scratch_set,
    )
    candidate_source_paths = (
        touched_source_paths or changed_source_receipts or read_source_paths[-10:]
    )
    source_set = set(by_kind["source"])
    diagnostic_source_like_paths = [
        path for path in by_kind["source"] if _looks_diagnostic_source_like_path(path)
    ]
    diagnostic_source_like_set = set(diagnostic_source_like_paths)
    actionable_source_paths = [
        path for path in by_kind["source"] if path not in diagnostic_source_like_set
    ]
    candidate_missing = [path for path in candidate_source_paths if path not in source_set]
    read_source_missing = [path for path in read_source_paths if path not in source_set]
    actionable_source_set = set(actionable_source_paths)
    candidate_actionable_missing = [
        path for path in candidate_source_paths if path not in actionable_source_set
    ]
    read_actionable_missing = [
        path for path in read_source_paths if path not in actionable_source_set
    ]

    mutation_paths = set(_paths_from_records(mutation_records))
    mutation_overlap = [path for path in normalized_diff_paths if path in mutation_paths]
    lost_source_mutations = [
        path for path in changed_source_receipts if path not in source_set
    ]
    normalized_candidates = [dict(candidate) for candidate in source_diff_candidates]
    recoverable_candidate_ids = _recoverable_candidate_ids(
        normalized_candidates,
        lost_source_mutations,
    )

    triggers: list[str] = []
    if lost_source_mutations:
        triggers.append("source_mutation_lost_before_final")
    if not normalized_diff_paths and touched_paths:
        triggers.append("workspace_writes_without_final_diff")
    if normalized_diff_paths and not by_kind["source"]:
        triggers.append("final_diff_without_source")
    if (
        normalized_diff_paths
        and touched_source_paths
        and candidate_missing
        and not (set(touched_source_paths) & source_set)
    ):
        triggers.append("candidate_source_drift")
    if by_kind["scratch"]:
        triggers.append("scratch_artifact_in_final_diff")
    if diagnostic_source_like_paths:
        triggers.append("diagnostic_source_like_in_final_diff")
    if _test_like_pollution_is_suspicious(
        test_like_count=len(by_kind["test-like"]),
        source_count=len(by_kind["source"]),
    ):
        triggers.append("test_like_heavy_final_diff")
    if by_kind["generated"]:
        triggers.append("generated_artifact_in_final_diff")

    return FinalDiffContractObservation(
        diff_paths=normalized_diff_paths,
        source_paths=by_kind["source"],
        scratch_paths=by_kind["scratch"],
        test_like_paths=by_kind["test-like"],
        docs_paths=by_kind["docs"],
        generated_paths=by_kind["generated"],
        diagnostic_source_like_paths=diagnostic_source_like_paths,
        actionable_source_paths=actionable_source_paths,
        candidate_source_paths=candidate_source_paths,
        candidate_source_missing_paths=candidate_missing,
        candidate_actionable_source_missing_paths=candidate_actionable_missing,
        read_source_paths=read_source_paths,
        read_source_missing_paths=read_source_missing,
        read_actionable_source_missing_paths=read_actionable_missing,
        mutation_overlap_paths=mutation_overlap,
        changed_source_receipt_paths=changed_source_receipts,
        lost_source_mutation_paths=lost_source_mutations,
        source_diff_candidates=normalized_candidates,
        recoverable_candidate_ids=recoverable_candidate_ids,
        triggers=_unique_strings(triggers),
    )


def final_diff_contract_recovery_message(
    observation: FinalDiffContractObservation,
) -> str:
    reason = observation.primary_reason.replace("_", " ")
    diff_text = _render_path_list(observation.diff_paths)
    source_text = _render_path_list(observation.source_paths)
    candidate_text = _render_path_list(observation.candidate_source_missing_paths)
    lost_source_text = _render_path_list(observation.lost_source_mutation_paths)
    pollution_paths = [
        *observation.scratch_paths,
        *observation.test_like_paths,
        *observation.diagnostic_source_like_paths,
    ]
    pollution_text = _render_path_list(pollution_paths)
    source_sentence = f" Current source diff paths: {source_text}." if source_text else ""
    candidate_sentence = (
        f" Source candidate(s) seen earlier but absent from the current diff: {candidate_text}."
        if candidate_text
        else ""
    )
    lost_source_sentence = (
        " Successful source edit receipt(s) exist, but these source path(s) are "
        f"absent from the current diff: {lost_source_text}."
        if lost_source_text
        else ""
    )
    recoverable_candidate_sentence = (
        " Recoverable source diff candidate(s): "
        f"{_render_path_list(observation.recoverable_candidate_ids)}. Inspect `git diff`; "
        "keep or recreate the source patch if it is still correct, or explicitly "
        "explain why the previous source edit should be discarded."
        if observation.recoverable_candidate_ids
        else ""
    )
    scratch_sentence = (
        " Suspicious scratch/debug/repro/test-like/diagnostic/source-like paths "
        f"in the current diff: {pollution_text}."
        if pollution_paths
        else ""
    )
    return (
        "[Runtime final-diff check]\n"
        f"The model is about to finish, but the current repository diff looks suspicious: "
        f"{reason}. Current diff paths: {diff_text}.{source_sentence}"
        f"{candidate_sentence}{lost_source_sentence}{recoverable_candidate_sentence}"
        f"{scratch_sentence} Before finalizing, inspect `git diff`, "
        "keep the smallest necessary source patch, and remove temporary scratch/debug/"
        "repro files. Keep test or documentation changes only if they are intentional "
        "and travel with the source fix."
    )


def _paths_from_records(
    records: Sequence[Mapping[str, Any]],
    *,
    excluded_classifications: frozenset[str] = frozenset(),
) -> list[str]:
    paths: list[str] = []
    for record in records:
        if record.get("classification") in excluded_classifications:
            continue
        raw = record.get("relative_path")
        if isinstance(raw, str) and raw:
            paths.append(raw)
            continue
        raw_paths = record.get("paths")
        if isinstance(raw_paths, Iterable) and not isinstance(raw_paths, (str, bytes)):
            for item in raw_paths:
                if isinstance(item, Mapping):
                    if item.get("classification") in excluded_classifications:
                        continue
                    nested = item.get("relative_path")
                    if isinstance(nested, str) and nested:
                        paths.append(nested)
    return _unique_paths(paths)


def _source_paths_from_records(
    records: Sequence[Mapping[str, Any]],
    *,
    known_scratch_paths: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    return [
        path
        for path in _paths_from_records(
            records,
            excluded_classifications=frozenset({"scratch"}),
        )
        if path not in known_scratch_paths and classify_final_diff_path(path) == "source"
    ]


def _changed_source_paths_from_receipts(
    receipts: Sequence[Mapping[str, Any]],
) -> list[str]:
    paths: list[str] = []
    for receipt in receipts:
        if receipt.get("changed") is not True:
            continue
        if receipt.get("classification") != "source":
            continue
        relative_path = receipt.get("relative_path")
        if isinstance(relative_path, str) and relative_path:
            paths.append(relative_path)
    return _unique_paths(paths)


def _recoverable_candidate_ids(
    candidates: Sequence[Mapping[str, Any]],
    lost_source_paths: Sequence[str],
) -> list[str]:
    lost_set = set(_unique_paths(lost_source_paths))
    if not lost_set:
        return []
    result: list[str] = []
    for candidate in candidates:
        if candidate.get("lost") is not True or candidate.get("restored") is True:
            continue
        candidate_paths = _unique_paths(
            path
            for path in candidate.get("paths", [])
            if isinstance(path, str)
        )
        if not lost_set.intersection(candidate_paths):
            continue
        candidate_id = candidate.get("candidate_id")
        if isinstance(candidate_id, str):
            result.append(candidate_id)
    return _unique_strings(result)


def _test_like_pollution_is_suspicious(*, test_like_count: int, source_count: int) -> bool:
    if test_like_count <= 0:
        return False
    if source_count <= 0:
        return True
    return test_like_count >= 3 and test_like_count > source_count * 2


def _looks_diagnostic_source_like_path(path: str) -> bool:
    return any(pattern.search(path) for pattern in _DIAGNOSTIC_SOURCE_LIKE_PATTERNS)


def _render_path_list(paths: Sequence[str], *, limit: int = 8) -> str:
    if not paths:
        return "<none>"
    rendered = ", ".join(paths[:limit])
    if len(paths) > limit:
        rendered += ", ..."
    return rendered


def _unique_paths(paths: Iterable[str]) -> list[str]:
    return _unique_strings(_normalize_path(path) for path in paths)


def _unique_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _normalize_path(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    if text == "/dev/null":
        return text
    if text.startswith("a/") or text.startswith("b/"):
        text = text[2:]
    normalized = Path(text).as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")
