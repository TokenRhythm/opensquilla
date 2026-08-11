from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

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
from opensquilla.eval.draco_selection_plan_evidence import (
    SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD,
    SELECTION_PLAN_EVIDENCE_ROW_FIELD,
    SELECTION_PLAN_PACK_ARTIFACT_FIELD,
    SelectionPlanPackAppender,
    SelectionPlanPackReader,
    compact_selection_plan_evidence_row,
    selection_plan_evidence_capability_contract,
    selection_plan_evidence_manifest_binding,
    selection_plan_reference_signal,
    selection_plan_row_capability_signal,
)

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "audit_draco_results.py"


def _load_audit_module():
    spec = importlib.util.spec_from_file_location("audit_draco_results_under_test", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit = _load_audit_module()


def _valid_row(group: str, task_hash: str, fingerprint: str) -> dict[str, object]:
    row: dict[str, object] = {
        "group": group,
        "task_id": "task-1",
        "task_input_sha256": task_hash,
        "run_compatibility_fingerprint": fingerprint,
        "error": None,
        "final_text": "answer",
        "quality_total": 80.0,
        "judge": {"score_status": "complete", "judge_error_count": 0},
        "ensemble_trace": {},
    }
    if group in audit.FIXED_MODELS:
        row["provider_spec"] = {"model": audit.FIXED_MODELS[group]}
        row["usage"] = {"model": audit.FIXED_MODELS[group]}
    return row


def _write_compact_bundle(
    directory: Path,
    row: dict[str, object],
    *,
    fingerprint: str,
    stamp: str = "20260811-120000",
) -> tuple[Path, Path, Path]:
    results_path = directory / f"draco_ensemble_{stamp}.jsonl"
    trace_path = directory / f"draco_run_{stamp}.trace.jsonl"
    checkpoint_path = directory / f"draco_run_{stamp}.checkpoint.json"
    manifest_path = directory / f"draco_run_{stamp}.manifest.json"
    pack_path = directory / f"draco_run_{stamp}.selection-plan.pack.jsonl"
    durable_capability = durable_artifact_capability_contract()
    with SelectionPlanPackAppender(pack_path) as appender:
        compact = compact_selection_plan_evidence_row(row, appender=appender)
        compact[DRACO_DURABLE_RESULT_ROW_FIELD] = durable_capability
        compact = seal_result_row(compact)
    with DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    ) as writer:
        assert writer.append(compact, trace_row_from_result(compact))
    verification = verify_durable_draco_artifacts(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )
    with SelectionPlanPackReader(pack_path, owner_only=True) as reader:
        binding = selection_plan_evidence_manifest_binding(
            pack_index=reader.index,
            durable_artifact_verification=verification,
            compact_row_count=1,
        )
    group = str(row["group"])
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
        "groups": [group],
        "durable_artifact_capability": durable_capability,
        "run_compatibility": {
            "contracts": {
                group: {"durable_artifact_capability": durable_capability}
            },
            "fingerprints": {group: fingerprint},
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
    return results_path, manifest_path, pack_path


def test_audit_excludes_incompatible_duplicate_shard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = {"id": "task-1", "prompt": "prompt", "rubric": {"criteria": ["a"]}}
    input_path = tmp_path / "input.jsonl"
    input_path.write_text(json.dumps(task) + "\n", encoding="utf-8")
    task_hash = audit.canonical_json_sha256(task)
    fingerprints = {group: f"sha256:{group.lower()}" for group in audit.GROUPS}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"run_compatibility": {"fingerprints": fingerprints}}),
        encoding="utf-8",
    )
    rows = [
        _valid_row(group, task_hash, fingerprints[group]) for group in audit.GROUPS
    ]
    rows.append(_valid_row("B1", task_hash, "sha256:incompatible"))
    result_path = tmp_path / "results.jsonl"
    result_path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "audit"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            "--input",
            str(input_path),
            "--result",
            str(result_path),
            "--output-dir",
            str(output_dir),
            "--expected-manifest",
            str(manifest_path),
        ],
    )

    assert audit.main() == 0

    report = json.loads((output_dir / "draco_audit.json").read_text(encoding="utf-8"))
    assert report["complete"] is True
    assert report["raw_duplicate_attempts"] == 1
    assert report["all_attempt_invalid_reason_counts"][
        "run_compatibility_fingerprint_mismatch"
    ] == 1
    final_rows = audit.read_jsonl(output_dir / "draco_audit.final.jsonl")
    assert len(final_rows) == 6
    b1 = next(row for row in final_rows if row["group"] == "B1")
    assert b1["run_compatibility_fingerprint"] == fingerprints["B1"]


def test_expected_manifest_without_fingerprints_fails(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="lacks run_compatibility"):
        audit.load_expected_fingerprints(path)


def test_legacy_read_keeps_line_metadata_and_does_not_open_compact_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "draco_ensemble_20260811-120001.jsonl"
    (tmp_path / "draco_run_20260811-120001.manifest.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    path.write_text(
        "\n{\"group\":\"B2\",\"task_id\":\"task-1\"}\n",
        encoding="utf-8",
    )

    def unexpected_index(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("legacy result unexpectedly opened the compact index")

    monkeypatch.setattr(audit.ResumeSourceIndex, "__init__", unexpected_index)
    assert audit.read_jsonl(path) == [
        {
            "group": "B2",
            "task_id": "task-1",
            "_audit_source": str(path),
            "_audit_source_line": 2,
        }
    ]

    path.write_text("{invalid\n", encoding="utf-8")
    with pytest.raises(ValueError, match=f"invalid JSONL at {path}:1"):
        audit.read_jsonl(path)


@pytest.mark.parametrize(
    "unsafe_sibling, expected_error",
    [
        ("fifo_manifest", "is not a regular file"),
        ("symlink_manifest", "cannot open resume sibling manifest"),
        ("oversize_manifest", "outside its byte bound"),
        ("malformed_manifest", "is not valid JSON"),
        ("broken_pack_symlink", "lacks its authenticated sibling binding"),
    ],
)
def test_unsafe_standard_sibling_is_never_treated_as_legacy(
    tmp_path: Path,
    unsafe_sibling: str,
    expected_error: str,
) -> None:
    stamp = "20260811-120002"
    result_path = tmp_path / f"draco_ensemble_{stamp}.jsonl"
    result_path.write_text(
        '{"group":"B2","task_id":"task-1"}\n',
        encoding="utf-8",
    )
    manifest_path = tmp_path / f"draco_run_{stamp}.manifest.json"
    pack_path = tmp_path / f"draco_run_{stamp}.selection-plan.pack.jsonl"
    if unsafe_sibling == "fifo_manifest":
        os.mkfifo(manifest_path)
    elif unsafe_sibling == "symlink_manifest":
        target = tmp_path / "manifest-target.json"
        target.write_text("{}\n", encoding="utf-8")
        manifest_path.symlink_to(target)
    elif unsafe_sibling == "oversize_manifest":
        with manifest_path.open("wb") as handle:
            handle.seek(64 * 1024 * 1024)
            handle.write(b"\n")
    elif unsafe_sibling == "malformed_manifest":
        manifest_path.write_text("{invalid\n", encoding="utf-8")
    elif unsafe_sibling == "broken_pack_symlink":
        pack_path.symlink_to(tmp_path / "missing-pack.jsonl")
    else:  # pragma: no cover - parameter list is closed above.
        raise AssertionError(unsafe_sibling)

    with pytest.raises(ValueError, match=expected_error):
        audit.read_jsonl(result_path)


def test_audit_supports_single_group_and_max_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks = [
        {"id": "task-1", "prompt": "first"},
        {"id": "task-2", "prompt": "second"},
    ]
    input_path = tmp_path / "input.jsonl"
    input_path.write_text(
        "\n".join(json.dumps(task) for task in tasks) + "\n",
        encoding="utf-8",
    )
    fingerprint = "sha256:b2"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {"run_compatibility": {"fingerprints": {"B2": fingerprint}}}
        ),
        encoding="utf-8",
    )
    result_path = tmp_path / "result.jsonl"
    result_path.write_text(
        json.dumps(
            _valid_row(
                "B2",
                audit.canonical_json_sha256(tasks[0]),
                fingerprint,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "audit"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            "--input",
            str(input_path),
            "--result",
            str(result_path),
            "--output-dir",
            str(output_dir),
            "--expected-manifest",
            str(manifest_path),
            "--groups",
            "B2",
            "--max-tasks",
            "1",
        ],
    )

    assert audit.main() == 0
    report = json.loads((output_dir / "draco_audit.json").read_text())
    assert report["groups"] == ["B2"]
    assert report["input_task_count"] == 1
    assert report["expected_unique"] == 1


def test_required_result_evidence_rejects_a_mutated_sealed_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = {"id": "task-1", "prompt": "prompt"}
    input_path = tmp_path / "input.jsonl"
    input_path.write_text(json.dumps(task) + "\n", encoding="utf-8")
    fingerprint = "sha256:b2"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {"run_compatibility": {"fingerprints": {"B2": fingerprint}}}
        ),
        encoding="utf-8",
    )
    sealed = seal_result_row(
        _valid_row(
            "B2",
            audit.canonical_json_sha256(task),
            fingerprint,
        )
    )
    sealed["final_text"] = "mutated after sealing"
    result_path = tmp_path / "result.jsonl"
    result_path.write_text(json.dumps(sealed) + "\n", encoding="utf-8")
    output_dir = tmp_path / "audit"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            "--input",
            str(input_path),
            "--result",
            str(result_path),
            "--output-dir",
            str(output_dir),
            "--expected-manifest",
            str(manifest_path),
            "--groups",
            "B2",
            "--require-result-evidence",
        ],
    )

    assert audit.main() == 2
    report = json.loads((output_dir / "draco_audit.json").read_text())
    assert report["complete"] is False
    assert report["result_evidence_enforced"] is True
    assert report["all_attempt_invalid_reason_counts"] == {
        "invalid_result_evidence": 1
    }


def test_audit_materializes_bound_compact_rows_and_writes_resealed_inline_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = {"id": "task-1", "prompt": "prompt"}
    input_path = tmp_path / "input.jsonl"
    input_path.write_text(json.dumps(task) + "\n", encoding="utf-8")
    fingerprint = "sha256:b2"
    row = _valid_row("B2", audit.canonical_json_sha256(task), fingerprint)
    plan = {
        "strategy": "router_dynamic",
        "selected_P": ["openrouter:model-a"],
        "request_context": {"task": "bounded"},
    }
    row["routing_trace"] = {"selection_plan": plan}
    row["execution"] = {
        "generation_attempts": [
            {"attempt_id": "attempt-1", "selection_plan": plan}
        ]
    }
    result_path, manifest_path, _pack_path = _write_compact_bundle(
        tmp_path,
        row,
        fingerprint=fingerprint,
    )
    output_dir = tmp_path / "audit"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            "--input",
            str(input_path),
            "--result",
            str(result_path),
            "--output-dir",
            str(output_dir),
            "--expected-manifest",
            str(manifest_path),
            "--groups",
            "B2",
            "--require-result-evidence",
        ],
    )

    assert audit.main() == 0

    final_path = output_dir / "draco_audit.final.jsonl"
    final_row = json.loads(final_path.read_text(encoding="utf-8"))
    assert final_row["routing_trace"]["selection_plan"] == plan
    assert final_row["execution"]["generation_attempts"][0]["selection_plan"] == plan
    assert not selection_plan_row_capability_signal(final_row)
    assert not selection_plan_reference_signal(final_row)
    assert verify_result_row_evidence(final_row)


@pytest.mark.parametrize(
    "mutation, expected_error",
    [
        ("missing_manifest", "undeclared compact selection-plan evidence"),
        ("removed_binding", "lacks its terminal binding"),
        ("missing_pack", "compact resume evidence binding failed"),
        ("tampered_pack", "compact resume evidence binding failed"),
        ("downgraded_row", "compact resume evidence binding failed"),
    ],
)
def test_compact_audit_fails_closed_when_bundle_is_missing_or_tampered(
    tmp_path: Path,
    mutation: str,
    expected_error: str,
) -> None:
    fingerprint = "sha256:b2"
    row = _valid_row("B2", "sha256:task", fingerprint)
    plan = {
        "strategy": "router_dynamic",
        "selected_P": ["openrouter:model-a"],
        "request_context": {"task": "bounded"},
    }
    row["routing_trace"] = {"selection_plan": plan}
    result_path, manifest_path, pack_path = _write_compact_bundle(
        tmp_path,
        row,
        fingerprint=fingerprint,
    )
    if mutation == "missing_manifest":
        manifest_path.unlink()
    elif mutation == "removed_binding":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop(SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD)
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    elif mutation == "missing_pack":
        pack_path.unlink()
    elif mutation == "tampered_pack":
        pack_path.write_bytes(pack_path.read_bytes() + b"tampered")
    elif mutation == "downgraded_row":
        downgraded = json.loads(result_path.read_text(encoding="utf-8"))
        downgraded.pop(SELECTION_PLAN_EVIDENCE_ROW_FIELD)
        downgraded["routing_trace"]["selection_plan"] = plan
        result_path.write_text(
            json.dumps(seal_result_row(downgraded)) + "\n",
            encoding="utf-8",
        )
    else:  # pragma: no cover - parameter list is closed above.
        raise AssertionError(mutation)

    with pytest.raises(ValueError, match=expected_error):
        audit.read_jsonl(result_path)
