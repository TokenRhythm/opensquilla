from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from opensquilla.eval import draco_artifact_io as artifact_io
from opensquilla.eval.draco_artifact_integrity import seal_result_row

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "experiments"
    / "recover_draco_run_artifacts.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("recover_draco_run_artifacts_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _running_half_pair(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    stamp = "20260811-010203"
    results_path = tmp_path / f"draco_ensemble_{stamp}.jsonl"
    trace_path = tmp_path / f"draco_run_{stamp}.trace.jsonl"
    checkpoint_path = tmp_path / f"draco_run_{stamp}.checkpoint.json"
    manifest_path = tmp_path / f"draco_run_{stamp}.manifest.json"
    writer = artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )
    writer.close()
    capability = artifact_io.durable_artifact_capability_contract()
    result = seal_result_row(
        {
            "group": "B0",
            "task_id": "task-1",
            "row_index": 1,
            "final_text": "paid answer",
            "error": None,
            artifact_io.DRACO_DURABLE_RESULT_ROW_FIELD: capability,
        }
    )
    results_path.write_bytes(
        (json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n").encode()
    )
    manifest = {
        "schema": artifact_io.DRACO_RUN_MANIFEST_SCHEMA_V2,
        "benchmark": "DRACO",
        "durable_artifact_capability": capability,
        "stamp": stamp,
        "status": "running",
        "started_at": 1.0,
        "finished_at": None,
        "groups": ["B0"],
        "task_ids": ["task-1", "task-2"],
        "rows_written": 0,
        "run_compatibility": {
            "contracts": {
                "B0": {"durable_artifact_capability": capability},
            }
        },
        "artifacts": {
            "results_jsonl": str(results_path),
            "trace_jsonl": str(trace_path),
            "checkpoint_json": str(checkpoint_path),
            "manifest_json": str(manifest_path),
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    return manifest_path, results_path, trace_path, checkpoint_path


def test_recover_run_repairs_half_pair_and_publishes_terminal_manifest(
    tmp_path: Path,
) -> None:
    module = _load()
    manifest_path, results_path, trace_path, checkpoint_path = _running_half_pair(tmp_path)

    recovery = module.recover_run(manifest_path)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "result_incomplete"
    assert manifest["rows_written"] == 1
    assert manifest["artifact_recovery"] == recovery
    assert recovery["prior_status"] == "running"
    assert recovery["ambiguous_pairs"] == [{"group": "B0", "task_id": "task-2"}]
    assert recovery["durable_scheduled_pairs"] == []
    assert recovery["physical_request_policy"] == "no_model_or_provider_calls"
    assert recovery["automatic_rerun_allowed"] is False
    prior_manifest_path = Path(recovery["prior_manifest_path"])
    assert prior_manifest_path.is_file()
    assert (
        module._sha256_bytes(prior_manifest_path.read_bytes()) == recovery["prior_manifest_sha256"]
    )
    verification = artifact_io.verify_durable_draco_artifacts(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )
    assert verification["rows_written"] == 1
    assert module.recover_run(manifest_path) == recovery


def test_recover_run_refuses_an_active_runner_lock(tmp_path: Path) -> None:
    module = _load()
    manifest_path, _, _, checkpoint_path = _running_half_pair(tmp_path)
    lock_path = checkpoint_path.with_suffix(checkpoint_path.suffix + ".lock")

    with artifact_io.DracoArtifactRunLock(lock_path):
        with pytest.raises(
            artifact_io.DracoArtifactDurabilityError,
            match="locked by an active",
        ):
            module.recover_run(manifest_path)

    assert json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "running"


def test_recover_run_preserves_only_durable_subset_of_resume_schedule(
    tmp_path: Path,
) -> None:
    module = _load()
    manifest_path, _, _, _ = _running_half_pair(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["resume_selection"] = {
        "scheduled_pairs": [
            {"group": "B0", "task_id": "task-1", "action": "metadata_only"},
            {"group": "B0", "task_id": "task-2", "action": "regenerate"},
        ]
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    recovery = module.recover_run(manifest_path)

    assert recovery["durable_scheduled_pairs"] == [
        {"group": "B0", "task_id": "task-1", "action": "metadata_only"}
    ]
    recovered_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "resume_selection" not in recovered_manifest
    prior_manifest = json.loads(Path(recovery["prior_manifest_path"]).read_text())
    assert len(prior_manifest["resume_selection"]["scheduled_pairs"]) == 2


def test_recover_run_retries_terminal_manifest_directory_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    manifest_path, _, _, _ = _running_half_pair(tmp_path)
    real_atomic_write_text = module.atomic_write_text

    def publish_then_fail_directory(path: Path, document: str) -> None:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                artifact_io,
                "fsync_directory",
                lambda _path: (_ for _ in ()).throw(
                    OSError("synthetic terminal directory fsync failure")
                ),
            )
            real_atomic_write_text(path, document)

    with monkeypatch.context() as scoped:
        scoped.setattr(module, "atomic_write_text", publish_then_fail_directory)
        with pytest.raises(OSError, match="terminal directory fsync failure"):
            module.recover_run(manifest_path)

    assert json.loads(manifest_path.read_text())["status"] == "result_incomplete"
    real_fsync_directory = module.fsync_directory
    retried_barriers: list[Path] = []

    def record_barrier(path: Path) -> None:
        retried_barriers.append(Path(path))
        real_fsync_directory(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(module, "fsync_directory", record_barrier)
        recovery = module.recover_run(manifest_path)
    assert recovery["status"] == "sealed_after_unclean_exit"
    assert manifest_path.parent in retried_barriers


@pytest.mark.parametrize("tamper", ["stamp", "result_path", "relative_path"])
def test_recover_run_rejects_nonstandard_or_unbound_artifact_paths(
    tmp_path: Path,
    tamper: str,
) -> None:
    module = _load()
    manifest_path, results_path, _, checkpoint_path = _running_half_pair(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    if tamper == "stamp":
        manifest["stamp"] = "20260811-010204"
    elif tamper == "result_path":
        manifest["artifacts"]["results_jsonl"] = str(tmp_path / "other.jsonl")
    else:
        manifest["artifacts"]["results_jsonl"] = results_path.name
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    before = checkpoint_path.read_bytes()

    with pytest.raises(
        artifact_io.DracoArtifactDurabilityError,
        match="stamp|standard|absolute",
    ):
        module.recover_run(manifest_path)

    assert checkpoint_path.read_bytes() == before


def test_recover_run_rejects_artifact_target_change_while_locking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    manifest_path, _, _, _ = _running_half_pair(tmp_path)
    real_acquire = module.DracoArtifactRunLock.acquire

    def acquire_then_tamper(lock) -> None:
        real_acquire(lock)
        manifest = json.loads(manifest_path.read_text())
        manifest["artifacts"]["trace_jsonl"] = str(tmp_path / "other.trace.jsonl")
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    monkeypatch.setattr(module.DracoArtifactRunLock, "acquire", acquire_then_tamper)
    with pytest.raises(
        artifact_io.DracoArtifactDurabilityError,
        match="changed while recovery acquired",
    ):
        module.recover_run(manifest_path)


@pytest.mark.parametrize("ledger", ["ambiguous_pairs", "durable_scheduled_pairs"])
def test_recover_run_idempotency_recomputes_recovery_ledgers(
    tmp_path: Path,
    ledger: str,
) -> None:
    module = _load()
    manifest_path, _, _, _ = _running_half_pair(tmp_path)
    module.recover_run(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    if ledger == "ambiguous_pairs":
        manifest["artifact_recovery"]["ambiguous_pairs"] = [{"group": "B0", "task_id": "forged"}]
    else:
        manifest["artifact_recovery"]["durable_scheduled_pairs"] = [
            {"group": "B0", "task_id": "task-1", "action": "metadata_only"}
        ]
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    with pytest.raises(
        artifact_io.DracoArtifactDurabilityError,
        match="not idempotently valid",
    ):
        module.recover_run(manifest_path)


def test_recover_run_rejects_unknown_recovered_resume_action(tmp_path: Path) -> None:
    module = _load()
    manifest_path, _, _, _ = _running_half_pair(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["resume_selection"] = {
        "scheduled_pairs": [{"group": "B0", "task_id": "task-1", "action": "unsafe_action"}]
    }
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    with pytest.raises(
        artifact_io.DracoArtifactDurabilityError,
        match="not uniquely recoverable",
    ):
        module.recover_run(manifest_path)
