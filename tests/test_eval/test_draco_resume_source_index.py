from __future__ import annotations

import asyncio
import copy
import gc
import io
import json
import os
import re
import tracemalloc
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from opensquilla.eval import draco_resume_source_index as resume_source_index
from opensquilla.eval import draco_selection_plan_evidence as plan_evidence
from opensquilla.eval.draco_artifact_integrity import (
    seal_result_row,
    trace_row_from_result,
    verify_result_row_evidence,
)
from opensquilla.eval.draco_artifact_io import (
    DRACO_DURABLE_RESULT_ROW_FIELD,
    DRACO_RUN_MANIFEST_SCHEMA_V2,
    DurableDracoArtifactWriter,
    durable_artifact_capability_contract,
    verify_durable_draco_artifacts,
)
from opensquilla.eval.draco_resume_source_index import (
    DracoResumeSourceError,
    ResumeGroupTaskStates,
    ResumeRowLocator,
    ResumeSourceIndex,
)
from opensquilla.eval.draco_selection_plan_evidence import (
    SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD,
    SELECTION_PLAN_EVIDENCE_ROW_FIELD,
    SELECTION_PLAN_PACK_ARTIFACT_FIELD,
    LazySelectionPlanMapping,
    SelectionPlanEvidenceError,
    SelectionPlanPackAppender,
    SelectionPlanPackReader,
    compact_selection_plan_evidence_row,
    lazy_selection_plan_row_view,
    selection_plan_evidence_capability_contract,
    selection_plan_evidence_manifest_binding,
    selection_plan_reference_signal,
)


def _sealed_row(task_id: str, *, padding: str = "") -> dict[str, object]:
    return seal_result_row(
        {
            "group": "B1",
            "task_id": task_id,
            "final_text": "accepted",
            "padding": padding,
        }
    )


def _write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("wb") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")).encode())
            handle.write(b"\n")


def _write_compact_source_bundle(
    directory: Path,
    rows: list[dict[str, object]],
    *,
    stamp: str = "20260811-120000",
) -> tuple[Path, Path, list[dict[str, object]]]:
    results_path = directory / f"draco_ensemble_{stamp}.jsonl"
    trace_path = directory / f"draco_run_{stamp}.trace.jsonl"
    checkpoint_path = directory / f"draco_run_{stamp}.checkpoint.json"
    manifest_path = directory / f"draco_run_{stamp}.manifest.json"
    pack_path = directory / f"draco_run_{stamp}.selection-plan.pack.jsonl"
    compact_rows: list[dict[str, object]] = []
    durable_capability = durable_artifact_capability_contract()
    with SelectionPlanPackAppender(pack_path) as appender:
        for row in rows:
            compact = compact_selection_plan_evidence_row(row, appender=appender)
            compact[DRACO_DURABLE_RESULT_ROW_FIELD] = durable_capability
            compact_rows.append(seal_result_row(compact))
    with DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    ) as writer:
        for row in compact_rows:
            assert writer.append(row, trace_row_from_result(row))
    verification = verify_durable_draco_artifacts(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )
    compact_row_count = sum(
        selection_plan_reference_signal(row) for row in compact_rows
    )
    with SelectionPlanPackReader(pack_path, owner_only=True) as reader:
        binding = selection_plan_evidence_manifest_binding(
            pack_index=reader.index,
            durable_artifact_verification=verification,
            compact_row_count=compact_row_count,
        )
    groups = list(dict.fromkeys(str(row["group"]) for row in rows))
    artifacts = {
        "results_jsonl": str(results_path),
        "trace_jsonl": str(trace_path),
        "checkpoint_json": str(checkpoint_path),
        "manifest_json": str(manifest_path),
        SELECTION_PLAN_PACK_ARTIFACT_FIELD: str(pack_path),
    }
    manifest = {
        "schema": DRACO_RUN_MANIFEST_SCHEMA_V2,
        "stamp": stamp,
        "status": "complete",
        "groups": groups,
        "durable_artifact_capability": durable_capability,
        "run_compatibility": {
            "contracts": {
                group: {"durable_artifact_capability": durable_capability}
                for group in groups
            }
        },
        "artifacts": artifacts,
        SELECTION_PLAN_EVIDENCE_ROW_FIELD: (
            selection_plan_evidence_capability_contract()
        ),
        SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD: binding,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return results_path, manifest_path, compact_rows


def _scan_source(
    index: ResumeSourceIndex,
    path: Path,
    *,
    source_index: int,
) -> list[ResumeRowLocator]:
    locators: list[ResumeRowLocator] = []
    for indexed in index.iter_source(path, source_index=source_index):
        row = json.loads(indexed.payload)
        locators.append(
            indexed.locator.bind(
                group=str(row["group"]),
                task_id=str(row["task_id"]),
            )
        )
    return locators


def _scan(
    index: ResumeSourceIndex,
    path: Path,
) -> list[ResumeRowLocator]:
    locators = _scan_source(index, path, source_index=0)
    index.seal()
    return locators


def test_completed_source_reports_authenticated_compact_state_without_io(
    tmp_path: Path,
) -> None:
    compact_dir = tmp_path / "compact"
    compact_dir.mkdir()
    compact_path, _manifest_path, _rows = _write_compact_source_bundle(
        compact_dir,
        [],
    )
    with ResumeSourceIndex([compact_path], force_spool=False) as compact_index:
        assert list(compact_index.iter_source(compact_path, source_index=0)) == []
        assert compact_index.source_is_compact_authenticated(source_index=0) is True
        with pytest.raises(
            DracoResumeSourceError,
            match="no completed source 1",
        ):
            compact_index.source_is_compact_authenticated(source_index=1)
        compact_index.seal()

    legacy_path = tmp_path / "empty-legacy.jsonl"
    legacy_path.write_text("", encoding="utf-8")
    with ResumeSourceIndex([legacy_path], force_spool=False) as legacy_index:
        assert list(legacy_index.iter_source(legacy_path, source_index=0)) == []
        assert legacy_index.source_is_compact_authenticated(source_index=0) is False
        legacy_index.seal()


def test_sealed_source_artifact_evidence_is_bounded_detached_and_no_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_dir = tmp_path / "compact-evidence"
    compact_dir.mkdir()
    result_path, manifest_path, _rows = _write_compact_source_bundle(
        compact_dir,
        [],
    )
    trace_path = compact_dir / "draco_run_20260811-120000.trace.jsonl"
    checkpoint_path = compact_dir / "draco_run_20260811-120000.checkpoint.json"
    index = ResumeSourceIndex([result_path], force_spool=False)
    assert list(index.iter_source(result_path, source_index=0)) == []
    with pytest.raises(DracoResumeSourceError, match="must be sealed"):
        index.source_artifact_evidence(source_index=0)
    with pytest.raises(DracoResumeSourceError, match="must be sealed"):
        index.source_durable_artifact_verification(source_index=0)
    index.seal()

    with monkeypatch.context() as isolated:
        isolated.setattr(
            resume_source_index,
            "_hash_fd",
            lambda *_args, **_kwargs: pytest.fail("artifact evidence performed I/O"),
        )
        isolated.setattr(
            SelectionPlanPackReader,
            "verify_snapshot",
            lambda *_args, **_kwargs: pytest.fail("artifact evidence rescanned pack"),
        )
        evidence = index.source_artifact_evidence(source_index=0)
        durable_verification = index.source_durable_artifact_verification(
            source_index=0
        )

    def signature(path: Path) -> tuple[int, ...]:
        info = path.stat()
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    def sha256(path: Path) -> str:
        return resume_source_index._sha256(path.read_bytes())

    assert evidence == {
        "schema": "opensquilla.draco-resume-source-artifact-evidence/v1",
        "source_index": 0,
        "result_snapshot": {
            "signature": signature(result_path),
            "sha256": sha256(result_path),
        },
        "compact_authenticated": True,
        "compact_artifact_evidence": {
            "durable_hashes": {
                "results_sha256": sha256(result_path),
                "trace_sha256": sha256(trace_path),
                "checkpoint_sha256": sha256(checkpoint_path),
            },
            "path_signatures": {
                "results_jsonl": signature(result_path),
                "trace_jsonl": signature(trace_path),
                "checkpoint_json": signature(checkpoint_path),
            },
            "manifest_snapshot": {
                "signature": signature(manifest_path),
                "sha256": sha256(manifest_path),
            },
        },
    }
    assert durable_verification is not None
    assert durable_verification["rows_written"] == 0
    assert durable_verification["results_sha256"] == sha256(result_path)
    assert durable_verification["trace_sha256"] == sha256(trace_path)
    assert durable_verification["checkpoint_sha256"] == sha256(checkpoint_path)
    evidence["result_snapshot"]["sha256"] = "mutated"
    evidence["compact_artifact_evidence"]["durable_hashes"][
        "trace_sha256"
    ] = "mutated"
    fresh = index.source_artifact_evidence(source_index=0)
    durable_verification["results_sha256"] = "mutated"
    fresh_durable = index.source_durable_artifact_verification(source_index=0)
    assert fresh["result_snapshot"]["sha256"] == sha256(result_path)
    assert (
        fresh["compact_artifact_evidence"]["durable_hashes"]["trace_sha256"]
        == sha256(trace_path)
    )
    assert fresh_durable is not None
    assert fresh_durable["results_sha256"] == sha256(result_path)
    index.close()
    with pytest.raises(DracoResumeSourceError, match="closed"):
        index.source_artifact_evidence(source_index=0)
    with pytest.raises(DracoResumeSourceError, match="closed"):
        index.source_durable_artifact_verification(source_index=0)


def test_legacy_source_artifact_evidence_and_invalid_lookup_gates(
    tmp_path: Path,
) -> None:
    active_path = tmp_path / "active.jsonl"
    _write_rows(active_path, [_sealed_row("active")])
    active_index = ResumeSourceIndex([active_path], force_spool=False)
    active_rows = active_index.iter_source(active_path, source_index=0)
    next(active_rows)
    with pytest.raises(DracoResumeSourceError, match="during a source scan"):
        active_index.source_artifact_evidence(source_index=0)
    with pytest.raises(DracoResumeSourceError, match="during a source scan"):
        active_index.source_durable_artifact_verification(source_index=0)
    active_rows.close()
    active_index.close(verify=False)

    legacy_path = tmp_path / "legacy.jsonl"
    legacy_path.write_text("", encoding="utf-8")
    index = ResumeSourceIndex([legacy_path], force_spool=False)
    rows = index.iter_source(legacy_path, source_index=4)
    with pytest.raises(DracoResumeSourceError):
        index.source_artifact_evidence(source_index=4)
    assert list(rows) == []
    index.seal()
    evidence = index.source_artifact_evidence(source_index=4)
    assert evidence["source_index"] == 4
    assert evidence["compact_authenticated"] is False
    assert evidence["compact_artifact_evidence"] is None
    assert evidence["result_snapshot"]["sha256"] == resume_source_index._sha256(b"")
    assert index.source_durable_artifact_verification(source_index=4) is None
    with pytest.raises(DracoResumeSourceError, match="non-negative"):
        index.source_artifact_evidence(source_index=True)
    with pytest.raises(DracoResumeSourceError, match="no completed source 0"):
        index.source_artifact_evidence(source_index=0)
    index.close()


def test_lazy_selection_plan_row_view_resolves_once_and_never_leaks_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_path = tmp_path / "selection-plan.pack.jsonl"
    plan = {
        "strategy": "router_dynamic",
        "selected_P": ["openrouter:model-a"],
        "request_context": {"task": "bounded"},
    }
    with SelectionPlanPackAppender(pack_path) as appender:
        compact = compact_selection_plan_evidence_row(
            {
                "group": "G1",
                "task_id": "task-1",
                "routing_trace": {"selection_plan": plan},
                "execution": {
                    "provider_calls": [{"selection_plan": plan}],
                },
            },
            appender=appender,
        )

    with SelectionPlanPackReader(pack_path) as reader:
        actual_expand = reader.expand_selection_plan
        expand_count = 0

        def recording_expand(value: object) -> object:
            nonlocal expand_count
            expand_count += 1
            return actual_expand(value)

        monkeypatch.setattr(reader, "expand_selection_plan", recording_expand)
        view = lazy_selection_plan_row_view(
            compact,
            reader=reader,
            require_references=True,
        )
        routing_plan = view["routing_trace"]["selection_plan"]
        call_plan = view["execution"]["provider_calls"][0]["selection_plan"]
        assert isinstance(routing_plan, LazySelectionPlanMapping)
        assert isinstance(call_plan, LazySelectionPlanMapping)
        assert expand_count == 0

        request_context = routing_plan["request_context"]
        assert isinstance(request_context, dict)
        request_context["task"] = "mutated"
        assert routing_plan["request_context"] == {"task": "bounded"}
        assert call_plan["strategy"] == "router_dynamic"
        assert expand_count == 1
        assert not selection_plan_reference_signal(dict(routing_plan))
        copied_plan = copy.deepcopy(call_plan)
        assert copied_plan == plan
        assert isinstance(copied_plan, dict)
        assert not selection_plan_reference_signal(copied_plan)
        assert expand_count == 1
        with pytest.raises(TypeError):
            json.dumps(routing_plan)
        assert isinstance(routing_plan, Mapping)


@pytest.mark.parametrize("force_spool", [False, True])
def test_compact_source_bundle_classifies_lazily_and_consumes_sealed_inline(
    tmp_path: Path,
    force_spool: bool,
) -> None:
    plan = {
        "strategy": "router_dynamic",
        "selected_P": ["openrouter:model-a"],
        "request_context": {"task": "bounded"},
    }
    results_path, _, _ = _write_compact_source_bundle(
        tmp_path,
        [
            {
                "group": "B1",
                "task_id": "task-1",
                "final_text": "accepted",
                "routing_trace": {"selection_plan": plan},
                "execution": {
                    "provider_calls": [{"selection_plan": plan}],
                },
            }
        ],
    )
    index = ResumeSourceIndex([results_path], force_spool=force_spool)
    with index.open_source(results_path, source_index=0) as source:
        indexed = next(source)
        raw = json.loads(indexed.payload)
        locator = indexed.locator.bind(group="B1", task_id="task-1")
        view, preverified = index.classification_row(locator, raw)
        assert preverified is True
        assert SELECTION_PLAN_EVIDENCE_ROW_FIELD not in view
        routing_plan = view["routing_trace"]["selection_plan"]
        call_plan = view["execution"]["provider_calls"][0]["selection_plan"]
        assert isinstance(routing_plan, LazySelectionPlanMapping)
        assert index.selection_plan_classification_materialization_count == 0
        assert routing_plan["strategy"] == "router_dynamic"
        assert call_plan["request_context"] == {"task": "bounded"}
        assert index.selection_plan_classification_materialization_count == 1
        with pytest.raises(StopIteration):
            next(source)
    index.seal()

    assert index.materialized_row_count == 0
    assert index.selection_plan_materialized_row_count == 0
    consumed = index.consume_row(locator)
    assert verify_result_row_evidence(consumed)
    assert SELECTION_PLAN_EVIDENCE_ROW_FIELD not in consumed
    assert not selection_plan_reference_signal(consumed)
    assert consumed["routing_trace"]["selection_plan"] == plan
    assert consumed["execution"]["provider_calls"][0]["selection_plan"] == plan
    assert index.materialized_row_count == 1
    assert index.selection_plan_materialized_row_count == 1
    index.close()


def test_compact_header_only_source_never_materializes_a_plan(tmp_path: Path) -> None:
    results_path, _, _ = _write_compact_source_bundle(
        tmp_path,
        [
            {
                "group": "B1",
                "task_id": "task-1",
                "final_text": "accepted",
            }
        ],
    )
    index = ResumeSourceIndex([results_path])
    with index.open_source(results_path, source_index=0) as source:
        indexed = next(source)
        raw = json.loads(indexed.payload)
        locator = indexed.locator.bind(group="B1", task_id="task-1")
        view, preverified = index.classification_row(locator, raw)
        assert preverified is True
        assert SELECTION_PLAN_EVIDENCE_ROW_FIELD not in view
        assert index.selection_plan_classification_materialization_count == 0
        with pytest.raises(StopIteration):
            next(source)
    index.seal()
    consumed = index.consume_row(locator)
    assert verify_result_row_evidence(consumed)
    assert SELECTION_PLAN_EVIDENCE_ROW_FIELD not in consumed
    assert index.selection_plan_materialized_row_count == 0
    index.close()


@pytest.mark.parametrize("force_spool", [False, True])
def test_compact_complete_scan_has_no_eager_expand_or_per_row_pack_rescan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force_spool: bool,
) -> None:
    plan = {
        "strategy": "router_dynamic",
        "request_context": {"padding": "x" * (256 * 1024)},
    }
    results_path, _, _ = _write_compact_source_bundle(
        tmp_path,
        [
            {
                "group": "B1",
                "task_id": f"task-{index}",
                "final_text": "accepted",
                "routing_trace": {"selection_plan": plan},
            }
            for index in range(8)
        ],
    )
    actual_scan = plan_evidence._scan_pack_fd
    actual_expand = SelectionPlanPackReader.expand_selection_plan
    scan_count = 0
    expand_count = 0

    def recording_scan(*args, **kwargs):
        nonlocal scan_count
        scan_count += 1
        return actual_scan(*args, **kwargs)

    def recording_expand(self, value):
        nonlocal expand_count
        expand_count += 1
        return actual_expand(self, value)

    monkeypatch.setattr(plan_evidence, "_scan_pack_fd", recording_scan)
    monkeypatch.setattr(
        SelectionPlanPackReader,
        "expand_selection_plan",
        recording_expand,
    )
    index = ResumeSourceIndex([results_path], force_spool=force_spool)
    with index.open_source(results_path, source_index=0) as source:
        for indexed in source:
            raw = json.loads(indexed.payload)
            locator = indexed.locator.bind(
                group=str(raw["group"]),
                task_id=str(raw["task_id"]),
            )
            view, preverified = index.classification_row(locator, raw)
            assert preverified is True
            assert isinstance(
                view["routing_trace"]["selection_plan"],
                LazySelectionPlanMapping,
            )
    index.seal()

    assert scan_count == 1
    assert expand_count == 0
    assert index.selection_plan_classification_materialization_count == 0
    index.close()
    assert scan_count == 2
    assert expand_count == 0


@pytest.mark.parametrize("force_spool", [False, True])
def test_lazy_classification_plan_expires_with_its_source_line_scope(
    tmp_path: Path,
    force_spool: bool,
) -> None:
    results_path, _, _ = _write_compact_source_bundle(
        tmp_path,
        [
            {
                "group": "B1",
                "task_id": "task-1",
                "final_text": "accepted",
                "routing_trace": {
                    "selection_plan": {"strategy": "router_dynamic"}
                },
            }
        ],
    )
    index = ResumeSourceIndex([results_path], force_spool=force_spool)
    with index.open_source(results_path, source_index=0) as source:
        indexed = next(source)
        raw = json.loads(indexed.payload)
        locator = indexed.locator.bind(group="B1", task_id="task-1")
        view, _ = index.classification_row(locator, raw)
        lazy_plan = view["routing_trace"]["selection_plan"]
        assert isinstance(lazy_plan, LazySelectionPlanMapping)
        with pytest.raises(StopIteration):
            next(source)

    with pytest.raises(SelectionPlanEvidenceError, match="escaped.*scan scope"):
        lazy_plan["strategy"]
    index.seal()
    index.close()


def test_compact_strict_attempt_batch_loads_parent_once_without_pack_rescan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt_ids = [character * 32 for character in ("a", "b", "c", "d")]
    attempt_plans = [
        {
            "strategy": f"attempt-{index}",
            "request_context": {"padding": character * 64_000},
        }
        for index, character in enumerate(("a", "b", "c", "d"), start=1)
    ]
    results_path, _, _ = _write_compact_source_bundle(
        tmp_path,
        [
            {
                "group": "G1",
                "task_id": "task-1",
                "final_text": "accepted",
                "routing_trace": {
                    "selection_plan": {
                        "strategy": "unrelated-parent",
                        "request_context": {"padding": "p" * 64_000},
                    }
                },
                "execution": {
                    "generation_attempts": [
                        {
                            "attempt_id": attempt_id,
                            "attempt": index + 1,
                            "selection_plan": attempt_plans[index],
                        }
                        for index, attempt_id in enumerate(attempt_ids)
                    ]
                },
            }
        ],
    )
    actual_scan = plan_evidence._scan_pack_fd
    actual_expand = SelectionPlanPackReader.expand_selection_plan
    scan_count = 0
    expand_count = 0

    def recording_scan(*args, **kwargs):
        nonlocal scan_count
        scan_count += 1
        return actual_scan(*args, **kwargs)

    def recording_expand(self, value):
        nonlocal expand_count
        expand_count += 1
        return actual_expand(self, value)

    monkeypatch.setattr(plan_evidence, "_scan_pack_fd", recording_scan)
    monkeypatch.setattr(
        SelectionPlanPackReader,
        "expand_selection_plan",
        recording_expand,
    )
    index = ResumeSourceIndex([results_path], force_spool=True)
    locator = _scan(index, results_path)[0]
    requested_indices = (1, 3)
    attempts = index.load_attempts(
        [
            (locator, attempt_index, attempt_ids[attempt_index])
            for attempt_index in requested_indices
        ]
    )

    assert [attempt["attempt_id"] for attempt in attempts] == [
        attempt_ids[index] for index in requested_indices
    ]
    assert [attempt["selection_plan"] for attempt in attempts] == [
        attempt_plans[index] for index in requested_indices
    ]
    assert index.attempt_payload_load_count == 1
    assert index.selection_plan_materialized_row_count == 0
    assert scan_count == 1
    assert expand_count == 2
    index.close()
    assert scan_count == 2


@pytest.mark.parametrize(
    "mutation",
    [
        "running",
        "unknown_terminal",
        "missing_binding",
        "missing_capability",
        "wrong_pack_path",
    ],
)
def test_compact_source_rejects_partial_or_nonterminal_manifest(
    tmp_path: Path,
    mutation: str,
) -> None:
    results_path, manifest_path, _ = _write_compact_source_bundle(
        tmp_path,
        [{"group": "B1", "task_id": "task-1", "final_text": "accepted"}],
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "running":
        manifest["status"] = "running"
    elif mutation == "unknown_terminal":
        manifest["status"] = "purported_terminal"
    elif mutation == "missing_binding":
        manifest.pop(SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD)
    elif mutation == "missing_capability":
        manifest.pop(SELECTION_PLAN_EVIDENCE_ROW_FIELD)
    else:
        manifest["artifacts"][SELECTION_PLAN_PACK_ARTIFACT_FIELD] = str(
            tmp_path / "wrong.selection-plan.pack.jsonl"
        )
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    index = ResumeSourceIndex([results_path])
    with pytest.raises(DracoResumeSourceError, match="compact resume manifest"):
        _scan(index, results_path)
    index.close(verify=False)


def test_legacy_load_attempt_preserves_stale_parent_seal_compatibility(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "legacy-strict-attempt.jsonl"
    row = seal_result_row(
        {
            "group": "G1",
            "task_id": "task-1",
            "final_text": "accepted",
            "execution": {
                "generation_attempts": [
                    {
                        "attempt_id": "attempt-1",
                        "attempt": 1,
                        "run": {"llm_request_count": 1},
                    }
                ]
            },
        }
    )
    row["final_text"] = "historically repaired without resealing"
    _write_rows(source_path, [row])
    index = ResumeSourceIndex([source_path])
    locator = _scan(index, source_path)[0]

    attempt = index.load_attempt(
        locator,
        attempt_index=0,
        attempt_id="attempt-1",
    )

    assert attempt["attempt"] == 1
    assert index.attempt_payload_load_count == 1
    index.close()


def test_compact_row_without_sibling_manifest_fails_closed(tmp_path: Path) -> None:
    results_path, manifest_path, _ = _write_compact_source_bundle(
        tmp_path,
        [{"group": "B1", "task_id": "task-1", "final_text": "accepted"}],
    )
    manifest_path.unlink()
    index = ResumeSourceIndex([results_path])
    with pytest.raises(DracoResumeSourceError, match="undeclared compact"):
        _scan(index, results_path)
    index.close(verify=False)


def test_compact_sibling_manifest_rejects_utf8_bom(tmp_path: Path) -> None:
    results_path, manifest_path, _ = _write_compact_source_bundle(
        tmp_path,
        [{"group": "B1", "task_id": "task-1", "final_text": "accepted"}],
    )
    manifest_path.write_bytes(b"\xef\xbb\xbf" + manifest_path.read_bytes())
    index = ResumeSourceIndex([results_path])

    with pytest.raises(DracoResumeSourceError, match="not valid JSON"):
        _scan(index, results_path)
    index.close(verify=False)


def test_compact_pack_tamper_fails_before_any_source_row(tmp_path: Path) -> None:
    results_path, manifest_path, _ = _write_compact_source_bundle(
        tmp_path,
        [
            {
                "group": "B1",
                "task_id": "task-1",
                "final_text": "accepted",
                "routing_trace": {"selection_plan": {"strategy": "fixed"}},
            }
        ],
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pack_path = Path(manifest["artifacts"][SELECTION_PLAN_PACK_ARTIFACT_FIELD])
    with pack_path.open("ab") as handle:
        handle.write(b"tamper")
        handle.flush()
        os.fsync(handle.fileno())

    index = ResumeSourceIndex([results_path])
    with pytest.raises(DracoResumeSourceError, match="binding failed"):
        _scan(index, results_path)
    index.close(verify=False)


def test_compact_dangling_row_ref_fails_even_with_rebound_pack_manifest(
    tmp_path: Path,
) -> None:
    results_path, manifest_path, _ = _write_compact_source_bundle(
        tmp_path,
        [
            {
                "group": "B1",
                "task_id": "task-1",
                "final_text": "accepted",
                "routing_trace": {"selection_plan": {"strategy": "fixed"}},
            }
        ],
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifacts = manifest["artifacts"]
    pack_path = Path(artifacts[SELECTION_PLAN_PACK_ARTIFACT_FIELD])
    pack_lines = pack_path.read_bytes().splitlines(keepends=True)
    assert len(pack_lines) >= 2
    pack_path.write_bytes(b"".join(pack_lines[:-1]))
    pack_path.chmod(0o600)
    verification = verify_durable_draco_artifacts(
        results_path=results_path,
        trace_path=Path(artifacts["trace_jsonl"]),
        checkpoint_path=Path(artifacts["checkpoint_json"]),
    )
    with SelectionPlanPackReader(pack_path, owner_only=True) as reader:
        manifest[SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD] = (
            selection_plan_evidence_manifest_binding(
                pack_index=reader.index,
                durable_artifact_verification=verification,
                compact_row_count=1,
            )
        )
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    index = ResumeSourceIndex([results_path])
    with pytest.raises(DracoResumeSourceError, match="binding failed"):
        _scan(index, results_path)
    index.close(verify=False)


def test_compact_raw_seal_is_checked_before_lazy_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_path, _, _ = _write_compact_source_bundle(
        tmp_path,
        [
            {
                "group": "B1",
                "task_id": "task-1",
                "final_text": "accepted",
                "routing_trace": {"selection_plan": {"strategy": "fixed"}},
            }
        ],
    )
    index = ResumeSourceIndex([results_path])
    source = index.iter_source(results_path, source_index=0)
    indexed = next(source)
    raw = json.loads(indexed.payload)
    locator = indexed.locator.bind(group="B1", task_id="task-1")
    raw["final_text"] = "tampered after parse"
    lazy_called = False

    def forbidden_lazy_view(*_args, **_kwargs):
        nonlocal lazy_called
        lazy_called = True
        raise AssertionError("lazy view must not run before raw seal verification")

    monkeypatch.setattr(
        resume_source_index,
        "lazy_selection_plan_row_view",
        forbidden_lazy_view,
    )
    with pytest.raises(DracoResumeSourceError, match="evidence verification"):
        index.classification_row(locator, raw)
    assert lazy_called is False
    source.close()
    index.close(verify=False)


def test_compact_pack_fifo_fails_fast(tmp_path: Path) -> None:
    results_path, manifest_path, _ = _write_compact_source_bundle(
        tmp_path,
        [{"group": "B1", "task_id": "task-1", "final_text": "accepted"}],
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pack_path = Path(manifest["artifacts"][SELECTION_PLAN_PACK_ARTIFACT_FIELD])
    pack_path.unlink()
    os.mkfifo(pack_path, 0o600)

    index = ResumeSourceIndex([results_path])
    started = os.times().elapsed
    with pytest.raises(DracoResumeSourceError, match="binding failed"):
        _scan(index, results_path)
    assert os.times().elapsed - started < 1.0
    index.close(verify=False)


@pytest.mark.parametrize("artifact", ["manifest", "pack"])
def test_compact_sibling_path_replacement_fails_final_snapshot(
    tmp_path: Path,
    artifact: str,
) -> None:
    results_path, manifest_path, _ = _write_compact_source_bundle(
        tmp_path,
        [{"group": "B1", "task_id": "task-1", "final_text": "accepted"}],
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target = (
        manifest_path
        if artifact == "manifest"
        else Path(manifest["artifacts"][SELECTION_PLAN_PACK_ARTIFACT_FIELD])
    )
    replacement_payload = target.read_bytes()
    index = ResumeSourceIndex([results_path], force_spool=False)
    _scan(index, results_path)
    target.rename(target.with_suffix(target.suffix + ".original"))
    target.write_bytes(replacement_payload)
    if artifact == "pack":
        target.chmod(0o600)

    with pytest.raises(DracoResumeSourceError, match="changed|replaced"):
        index.close()
    assert index.closed


def test_forced_spool_rejects_same_bytes_pack_inode_replacement(
    tmp_path: Path,
) -> None:
    results_path, manifest_path, _ = _write_compact_source_bundle(
        tmp_path,
        [{"group": "B1", "task_id": "task-1", "final_text": "accepted"}],
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pack_path = Path(manifest["artifacts"][SELECTION_PLAN_PACK_ARTIFACT_FIELD])
    pack_payload = pack_path.read_bytes()
    index = ResumeSourceIndex([results_path], force_spool=True)
    _scan(index, results_path)
    pack_path.rename(pack_path.with_suffix(pack_path.suffix + ".original"))
    pack_path.write_bytes(pack_payload)
    pack_path.chmod(0o600)

    with pytest.raises(DracoResumeSourceError, match="bound inode"):
        index.close()
    assert index.closed


def test_manifest_path_replacement_during_final_hash_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_path, manifest_path, _ = _write_compact_source_bundle(
        tmp_path,
        [{"group": "B1", "task_id": "task-1", "final_text": "accepted"}],
    )
    index = ResumeSourceIndex([results_path], force_spool=False)
    _scan(index, results_path)
    manifest_stat = manifest_path.stat()
    manifest_payload = manifest_path.read_bytes()
    actual_hash_fd = resume_source_index._hash_fd
    replaced = False

    def replace_after_hash(fd: int, *, expected_size: int) -> str:
        nonlocal replaced
        digest = actual_hash_fd(fd, expected_size=expected_size)
        current = os.fstat(fd)
        if not replaced and (
            current.st_dev,
            current.st_ino,
        ) == (manifest_stat.st_dev, manifest_stat.st_ino):
            replaced = True
            manifest_path.rename(
                manifest_path.with_suffix(manifest_path.suffix + ".original")
            )
            manifest_path.write_bytes(manifest_payload)
        return digest

    monkeypatch.setattr(resume_source_index, "_hash_fd", replace_after_hash)
    with pytest.raises(
        DracoResumeSourceError,
        match="changed during verification|path was replaced",
    ):
        index.close()
    assert replaced is True
    assert index.closed


def test_compact_close_does_not_mask_primary_error(tmp_path: Path) -> None:
    class PrimaryError(RuntimeError):
        pass

    results_path, manifest_path, _ = _write_compact_source_bundle(
        tmp_path,
        [{"group": "B1", "task_id": "task-1", "final_text": "accepted"}],
    )
    index = ResumeSourceIndex([results_path], force_spool=False)
    _scan(index, results_path)
    with pytest.raises(PrimaryError, match="business failure"):
        with index:
            manifest_path.rename(manifest_path.with_suffix(".replaced"))
            raise PrimaryError("business failure")
    assert index.closed


def test_compact_cancellation_closes_bound_pack_reader(tmp_path: Path) -> None:
    results_path, _, _ = _write_compact_source_bundle(
        tmp_path,
        [{"group": "B1", "task_id": "task-1", "final_text": "accepted"}],
    )
    index = ResumeSourceIndex([results_path], force_spool=False)
    _scan(index, results_path)
    bundle = index._sources[0].compact_bundle  # noqa: SLF001 - close gate.
    assert bundle is not None
    assert bundle.reader is not None
    pack_fd = bundle.reader._fd  # noqa: SLF001 - close gate.

    with pytest.raises(asyncio.CancelledError):
        with index:
            raise asyncio.CancelledError

    assert index.closed
    with pytest.raises(OSError):
        os.fstat(pack_fd)


def test_large_complete_history_keeps_only_locators_until_one_pending_consume(
    tmp_path: Path,
) -> None:
    path = tmp_path / "large-history.jsonl"
    padding = "x" * (1024 * 1024)
    with path.open("wb") as handle:
        for row_index in range(101):
            row = _sealed_row(
                f"complete-{row_index}" if row_index < 100 else "pending",
                padding=padding,
            )
            handle.write(json.dumps(row, separators=(",", ":")).encode())
            handle.write(b"\n")
            del row
    del padding
    gc.collect()

    tracemalloc.start()
    index = ResumeSourceIndex([path])
    locators = _scan(index, path)
    _, classification_peak = tracemalloc.get_traced_memory()

    assert len(locators) == 101
    assert index.materialized_row_count == 0
    assert classification_peak < 12 * 1024 * 1024

    pending = index.consume_row(locators[-1])
    _, consume_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert pending["task_id"] == "pending"
    assert index.materialized_row_count == 1
    assert consume_peak < 16 * 1024 * 1024
    index.close()


def test_lf_only_short_line_scan_is_single_pass_per_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    line = b'{"x":1}\n'
    line_count = (1024 * 1024 // len(line)) + 1
    payload = line * line_count
    chunk_size = 64 * 1024
    actual_pattern = resume_source_index._UNIVERSAL_NEWLINE_RE
    scanned_bytes = 0
    scan_calls = 0

    class CountingPattern:
        def finditer(
            self,
            value: bytes,
            pos: int = 0,
        ) -> Iterator[re.Match[bytes]]:
            nonlocal scanned_bytes, scan_calls
            scanned_bytes += len(value) - pos
            scan_calls += 1
            return actual_pattern.finditer(value, pos)

    monkeypatch.setattr(
        resume_source_index,
        "_SOURCE_READ_CHUNK_BYTES",
        chunk_size,
    )
    monkeypatch.setattr(
        resume_source_index,
        "_UNIVERSAL_NEWLINE_RE",
        CountingPattern(),
    )

    observed_count = 0
    observed_bytes = 0
    for observed in resume_source_index._iter_universal_binary_lines(
        io.BytesIO(payload)
    ):
        assert observed == line
        observed_count += 1
        observed_bytes += len(observed)

    assert observed_count == line_count
    assert observed_bytes == len(payload)
    assert scanned_bytes == len(payload)
    assert scan_calls == (len(payload) + chunk_size - 1) // chunk_size


@pytest.mark.parametrize("force_spool", [False, True])
def test_path_replacement_cannot_change_bound_row_and_fails_final_snapshot(
    tmp_path: Path,
    force_spool: bool,
) -> None:
    path = tmp_path / "source.jsonl"
    _write_rows(path, [_sealed_row("original")])
    index = ResumeSourceIndex([path], force_spool=force_spool)
    locator = _scan(index, path)[0]

    path.rename(tmp_path / "original.jsonl")
    _write_rows(path, [_sealed_row("replacement")])

    if force_spool:
        assert index.consume_row(locator)["task_id"] == "original"
    else:
        with pytest.raises(DracoResumeSourceError, match="changed after indexing"):
            index.consume_row(locator)
    with pytest.raises(DracoResumeSourceError, match="path was replaced|changed after"):
        index.close()
    assert index.closed


def test_same_inode_tamper_fails_before_row_delivery_and_does_not_consume(
    tmp_path: Path,
) -> None:
    path = tmp_path / "source.jsonl"
    _write_rows(path, [_sealed_row("task-1", padding="abc")])
    index = ResumeSourceIndex([path], force_spool=False)
    locator = _scan(index, path)[0]

    fd = os.open(path, os.O_RDWR)
    try:
        os.pwrite(fd, b"Z", locator.offset + 1)
        os.fsync(fd)
    finally:
        os.close(fd)

    with pytest.raises(DracoResumeSourceError, match="changed after indexing"):
        index.consume_row(locator)
    assert index.materialized_row_count == 0
    with pytest.raises(DracoResumeSourceError, match="changed after indexing"):
        index.consume_row(locator)
    index.close(verify=False)


def test_context_primary_error_survives_tamper_and_closes_descriptor(
    tmp_path: Path,
) -> None:
    class PrimaryError(RuntimeError):
        pass

    path = tmp_path / "source.jsonl"
    _write_rows(path, [_sealed_row("task-1", padding="abc")])
    index = ResumeSourceIndex([path], force_spool=False)
    locator = _scan(index, path)[0]
    bound_fd = index._sources[0].fd  # noqa: SLF001 - descriptor lifecycle gate.

    with pytest.raises(PrimaryError, match="business failure"):
        with index:
            tamper_fd = os.open(path, os.O_RDWR)
            try:
                os.pwrite(tamper_fd, b"Z", locator.offset + 1)
                os.fsync(tamper_fd)
            finally:
                os.close(tamper_fd)
            raise PrimaryError("business failure")

    assert index.closed
    assert bound_fd is not None
    with pytest.raises(OSError):
        os.fstat(bound_fd)


@pytest.mark.parametrize("force_spool", [False, True])
def test_two_sources_preserve_universal_newlines_and_exact_locators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force_spool: bool,
) -> None:
    paths = [tmp_path / "one.jsonl", tmp_path / "two.jsonl"]
    encoded = [
        json.dumps(_sealed_row(task_id), separators=(",", ":")).encode()
        for task_id in ("a", "b", "c", "d", "e")
    ]
    # Make the first CR in each source land at a chunk boundary.  This covers
    # both a bare-CR record and a CRLF split across two reads.
    monkeypatch.setattr(
        resume_source_index,
        "_SOURCE_READ_CHUNK_BYTES",
        len(encoded[0]) + 1,
    )
    paths[0].write_bytes(encoded[0] + b"\r" + encoded[1] + b"\r")
    paths[1].write_bytes(
        encoded[2] + b"\r\n" + encoded[3] + b"\n" + encoded[4]
    )

    index = ResumeSourceIndex(paths, force_spool=force_spool)
    first = _scan_source(index, paths[0], source_index=0)
    second = _scan_source(index, paths[1], source_index=1)
    index.seal()

    assert [locator.line_number for locator in first] == [1, 2]
    assert [locator.line_number for locator in second] == [1, 2, 3]
    expected_first_offsets = [0, len(encoded[0]) + 1]
    second_base = paths[0].stat().st_size if force_spool else 0
    expected_second_offsets = [
        second_base,
        second_base + len(encoded[2]) + 2,
        second_base + len(encoded[2]) + 2 + len(encoded[3]) + 1,
    ]
    assert [locator.offset for locator in first] == expected_first_offsets
    assert [locator.offset for locator in second] == expected_second_offsets
    assert [index.consume_row(locator)["task_id"] for locator in first + second] == [
        "a",
        "b",
        "c",
        "d",
        "e",
    ]
    index.close()


@pytest.mark.parametrize("force_spool", [False, True])
def test_utf8_bom_is_rejected_for_direct_and_spooled_rows(
    tmp_path: Path,
    force_spool: bool,
) -> None:
    path = tmp_path / "bom.jsonl"
    encoded = json.dumps(_sealed_row("task-1"), separators=(",", ":")).encode()
    path.write_bytes(b"\xef\xbb\xbf" + encoded + b"\n")
    index = ResumeSourceIndex([path], force_spool=force_spool)
    indexed = list(index.iter_source(path, source_index=0))
    assert len(indexed) == 1
    locator = indexed[0].locator.bind(group="B1", task_id="task-1")
    index.seal()

    with pytest.raises(DracoResumeSourceError, match="invalid JSON"):
        index.consume_row(locator)
    index.close()


def test_consume_is_exactly_once_and_close_releases_bound_descriptor(
    tmp_path: Path,
) -> None:
    path = tmp_path / "source.jsonl"
    _write_rows(path, [_sealed_row("task-1")])
    index = ResumeSourceIndex([path], force_spool=False)
    locator = _scan(index, path)[0]
    bound_fd = index._sources[0].fd  # noqa: SLF001 - descriptor lifecycle gate.

    assert index.consume_row(locator)["task_id"] == "task-1"
    with pytest.raises(DracoResumeSourceError, match="already consumed"):
        index.consume_row(locator)
    index.close()

    assert bound_fd is not None
    with pytest.raises(OSError):
        os.fstat(bound_fd)


def test_failed_consume_keeps_locator_for_auditable_retry(tmp_path: Path) -> None:
    path = tmp_path / "unsealed.jsonl"
    _write_rows(
        path,
        [{"group": "B1", "task_id": "task-1", "final_text": "unsealed"}],
    )
    index = ResumeSourceIndex([path], force_spool=False)
    locator = _scan(index, path)[0]
    states = ResumeGroupTaskStates(
        {
            ("B1", "task-1"): {
                ResumeGroupTaskStates._LOCATOR_KEY: locator,
            }
        },
        source_index=index,
    )

    with pytest.raises(DracoResumeSourceError, match="evidence verification"):
        states.consume_row(("B1", "task-1"))
    assert (
        states[("B1", "task-1")][ResumeGroupTaskStates._LOCATOR_KEY]
        == locator
    )
    assert index.materialized_row_count == 0
    states.close()


def test_private_spool_rolling_hash_detects_tamper(tmp_path: Path) -> None:
    path = tmp_path / "source.jsonl"
    _write_rows(path, [_sealed_row("task-1")])
    index = ResumeSourceIndex([path], force_spool=True)
    locator = _scan(index, path)[0]
    assert index._spool_fd is not None  # noqa: SLF001 - adversarial gate.
    os.pwrite(index._spool_fd, b"Z", locator.offset + 1)  # noqa: SLF001
    os.fsync(index._spool_fd)  # noqa: SLF001

    with pytest.raises(DracoResumeSourceError, match="spool changed"):
        index.consume_row(locator)
    with pytest.raises(DracoResumeSourceError, match="spool changed"):
        index.close()
    assert index.closed


def test_source_count_over_fd_reserve_uses_private_0600_spool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resume_source_index, "_safe_bound_source_limit", lambda: 1)
    paths = [tmp_path / "one.jsonl", tmp_path / "two.jsonl"]
    index = ResumeSourceIndex(paths)

    assert index.backing == "spool"
    assert index._spool_fd is not None  # noqa: SLF001 - private spool gate.
    assert os.fstat(index._spool_fd).st_mode & 0o777 == 0o600  # noqa: SLF001
    index.seal()
    index.close()


def test_forced_spool_multisource_offsets_fsync_once_and_close_reverifies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [tmp_path / "one.jsonl", tmp_path / "two.jsonl"]
    _write_rows(paths[0], [_sealed_row("a"), _sealed_row("b")])
    _write_rows(paths[1], [_sealed_row("c")])
    actual_fsync = os.fsync
    fsync_calls: list[int] = []
    actual_hash_fd = resume_source_index._hash_fd
    hash_calls: list[tuple[int, int]] = []

    def recording_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        actual_fsync(fd)

    def recording_hash_fd(fd: int, *, expected_size: int) -> str:
        hash_calls.append((fd, expected_size))
        return actual_hash_fd(fd, expected_size=expected_size)

    monkeypatch.setattr(resume_source_index.os, "fsync", recording_fsync)
    monkeypatch.setattr(resume_source_index, "_hash_fd", recording_hash_fd)
    index = ResumeSourceIndex(paths, force_spool=True)
    spool_fd = index._spool_fd  # noqa: SLF001 - spool durability gate.
    first = _scan_source(index, paths[0], source_index=0)
    second = _scan_source(index, paths[1], source_index=1)
    index.seal()
    index.seal()

    assert spool_fd is not None
    assert fsync_calls == [spool_fd]
    assert [locator.offset for locator in first + second] == [
        0,
        first[0].length,
        first[0].length + first[1].length,
    ]
    assert index.consume_row(first[1])["task_id"] == "b"
    assert index.consume_row(second[0])["task_id"] == "c"
    index.close()

    # Close authenticates the private spool and every original source before
    # releasing descriptors; one call per backing object is expected.
    assert len(hash_calls) == 3
    assert sorted(expected_size for _, expected_size in hash_calls) == sorted(
        [
            paths[0].stat().st_size,
            paths[1].stat().st_size,
            sum(path.stat().st_size for path in paths),
        ]
    )
