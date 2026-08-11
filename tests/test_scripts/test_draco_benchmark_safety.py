from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opensquilla.eval import draco_selection_plan_evidence as plan_evidence
from opensquilla.eval.draco_artifact_integrity import (
    seal_result_row,
    trace_row_from_result,
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
)

ROOT = Path(__file__).resolve().parents[2]
CAPTURE_SCRIPT = ROOT / "scripts" / "experiments" / "capture_openrouter_account_usage.py"
COST_AUDIT_SCRIPT = (
    ROOT / "scripts" / "experiments" / "audit_draco_mini_cost_validation.py"
)
CREDENTIAL_LOADER = ROOT / "scripts" / "lib" / "load_draco_benchmark_credentials.sh"
FORMAL_WRAPPER = (
    ROOT / "scripts" / "experiments" / "run_draco_mini_b2_fullconfig_newkey.sh"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cost_audit_accepts_only_exact_or_dated_openrouter_model_revisions() -> None:
    module = _load_module(COST_AUDIT_SCRIPT, "audit_draco_model_revision_test")
    expected = "deepseek/deepseek-v4-pro"
    frozen = module.EXPECTED_MODEL_REVISIONS[expected]

    assert module.canonical_frozen_model(frozen, (expected,)) == expected
    assert module.canonical_frozen_model(expected, (expected,)) is None
    assert (
        module.canonical_frozen_model(
            expected, (expected,), allow_requested_base=True
        )
        == expected
    )
    assert module.canonical_frozen_model(f"{expected}-latest", (expected,)) is None
    assert module.canonical_frozen_model(f"{expected}-202604230", (expected,)) is None
    assert module.canonical_frozen_model(f"{expected}-evil-20260423", (expected,)) is None
    assert module.canonical_frozen_model(f"{expected}-20270101", (expected,)) is None


def test_cost_audit_rejects_non_object_jsonl_rows(tmp_path: Path) -> None:
    module = _load_module(COST_AUDIT_SCRIPT, "audit_draco_non_object_row_test")
    path = tmp_path / "invalid.jsonl"
    path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="must be an object"):
        module.load_rows(path)


def _write_compact_cost_audit_bundle(
    tmp_path: Path,
    *,
    task_id: str = "task-1",
) -> dict[str, Path]:
    stamp = "20260811-120000"
    result = tmp_path / f"draco_ensemble_{stamp}.jsonl"
    trace = tmp_path / f"draco_run_{stamp}.trace.jsonl"
    checkpoint = tmp_path / f"draco_run_{stamp}.checkpoint.json"
    manifest = tmp_path / f"draco_run_{stamp}.manifest.json"
    pack = tmp_path / f"draco_run_{stamp}.selection-plan.pack.jsonl"
    durable_capability = durable_artifact_capability_contract()
    with SelectionPlanPackAppender(pack) as appender:
        compact_row = compact_selection_plan_evidence_row(
            {
                "group": "B2",
                "task_id": task_id,
                "row_index": 1,
                "task_input_sha256": "sha256:" + "a" * 64,
                "run_compatibility_fingerprint": "sha256:" + "b" * 64,
                "routing_trace": {
                    "selection_plan": {
                        "strategy": "router_dynamic",
                        "selected_P": ["openrouter:model-a"],
                    }
                },
            },
            appender=appender,
        )
    compact_row[DRACO_DURABLE_RESULT_ROW_FIELD] = durable_capability
    compact_row = seal_result_row(compact_row)
    with DurableDracoArtifactWriter(
        results_path=result,
        trace_path=trace,
        checkpoint_path=checkpoint,
    ) as writer:
        assert writer.append(compact_row, trace_row_from_result(compact_row))
    durable_verification = verify_durable_draco_artifacts(
        results_path=result,
        trace_path=trace,
        checkpoint_path=checkpoint,
    )
    with SelectionPlanPackReader(pack, owner_only=True) as reader:
        binding = selection_plan_evidence_manifest_binding(
            pack_index=reader.index,
            durable_artifact_verification=durable_verification,
            compact_row_count=int(selection_plan_reference_signal(compact_row)),
        )
    manifest.write_text(
        json.dumps(
            {
                "schema": DRACO_RUN_MANIFEST_SCHEMA_V2,
                "stamp": stamp,
                "status": "complete",
                "groups": ["B2"],
                "durable_artifact_capability": durable_capability,
                "run_compatibility": {
                    "contracts": {
                        "B2": {
                            "durable_artifact_capability": durable_capability,
                        }
                    }
                },
                "artifacts": {
                    "results_jsonl": str(result),
                    "trace_jsonl": str(trace),
                    "checkpoint_json": str(checkpoint),
                    "manifest_json": str(manifest),
                    SELECTION_PLAN_PACK_ARTIFACT_FIELD: str(pack),
                },
                SELECTION_PLAN_EVIDENCE_ROW_FIELD: (
                    selection_plan_evidence_capability_contract()
                ),
                SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD: binding,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "result": result,
        "trace": trace,
        "checkpoint": checkpoint,
        "manifest": manifest,
        "pack": pack,
    }


def test_cost_audit_authenticates_compact_bundle_without_expansion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module(COST_AUDIT_SCRIPT, "audit_draco_compact_bundle_test")
    paths = _write_compact_cost_audit_bundle(tmp_path)

    def reject_expansion(*_args, **_kwargs):
        raise AssertionError("compact audit preflight must not expand selection plans")

    monkeypatch.setattr(
        plan_evidence.SelectionPlanPackReader,
        "expand_selection_plan",
        reject_expansion,
    )
    rows, trace_rows, manifest_path, manifest = module.load_audit_artifact_rows(
        paths["result"],
        trace_jsonl=paths["trace"],
        manifest_path=None,
    )

    assert manifest_path == paths["manifest"]
    assert manifest == json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert selection_plan_reference_signal(rows[0])
    assert trace_rows == [trace_row_from_result(rows[0])]


@pytest.mark.parametrize(
    "mutation",
    ["missing_manifest", "missing_pack", "tampered_pack", "downgraded_binding"],
)
def test_cost_audit_fails_closed_for_invalid_compact_bundle(
    mutation: str,
    tmp_path: Path,
) -> None:
    module = _load_module(
        COST_AUDIT_SCRIPT,
        f"audit_draco_invalid_compact_bundle_{mutation}_test",
    )
    paths = _write_compact_cost_audit_bundle(tmp_path)
    if mutation == "missing_manifest":
        paths["manifest"].unlink()
    elif mutation == "missing_pack":
        paths["pack"].unlink()
    elif mutation == "tampered_pack":
        with paths["pack"].open("ab") as handle:
            handle.write(b"{}\n")
    else:
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        manifest.pop(SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD)
        paths["manifest"].write_text(
            json.dumps(manifest, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    with pytest.raises(SystemExit, match="Invalid compact DRACO artifact bundle"):
        module.load_audit_artifact_rows(
            paths["result"],
            trace_jsonl=paths["trace"],
            manifest_path=paths["manifest"],
        )


def test_cost_audit_compact_bundle_rejects_unbound_trace(tmp_path: Path) -> None:
    module = _load_module(COST_AUDIT_SCRIPT, "audit_draco_unbound_compact_trace_test")
    paths = _write_compact_cost_audit_bundle(tmp_path)
    unbound_trace = tmp_path / "copied.trace.jsonl"
    unbound_trace.write_bytes(paths["trace"].read_bytes())

    with pytest.raises(SystemExit, match="bound standard trace sibling"):
        module.load_audit_artifact_rows(
            paths["result"],
            trace_jsonl=unbound_trace,
            manifest_path=paths["manifest"],
        )


@pytest.mark.parametrize("replacement_kind", ["legacy", "compact"])
def test_cost_audit_rejects_parent_directory_snapshot_replacement(
    replacement_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module(
        COST_AUDIT_SCRIPT,
        f"audit_draco_parent_snapshot_{replacement_kind}_test",
    )
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    paths = _write_compact_cost_audit_bundle(bundle_dir)
    saved_dir = tmp_path / "saved-bundle"
    real_directory_snapshot = module._BoundDirectorySnapshot

    class ReplacingDirectorySnapshot(real_directory_snapshot):
        def __enter__(self):
            value = super().__enter__()
            bundle_dir.rename(saved_dir)
            bundle_dir.mkdir()
            if replacement_kind == "compact":
                _write_compact_cost_audit_bundle(bundle_dir, task_id="unbound-task")
            else:
                paths["result"].write_text(
                    json.dumps({"group": "B2", "task_id": "unbound-task"}) + "\n",
                    encoding="utf-8",
                )
                paths["trace"].write_text(
                    json.dumps({"group": "ATTACK", "task_id": "unbound"}) + "\n",
                    encoding="utf-8",
                )
            return value

        def __exit__(self, exc_type, exc, traceback):
            shutil.rmtree(bundle_dir)
            saved_dir.rename(bundle_dir)
            return super().__exit__(exc_type, exc, traceback)

    monkeypatch.setattr(
        module,
        "_BoundDirectorySnapshot",
        ReplacingDirectorySnapshot,
    )

    with pytest.raises(SystemExit, match="authenticated bundle"):
        module.load_audit_artifact_rows(
            paths["result"],
            trace_jsonl=paths["trace"],
            manifest_path=paths["manifest"],
        )


def test_cost_audit_preserves_legacy_row_loading(tmp_path: Path) -> None:
    module = _load_module(COST_AUDIT_SCRIPT, "audit_draco_legacy_bundle_test")
    result = tmp_path / "legacy-result.jsonl"
    trace = tmp_path / "legacy-trace.jsonl"
    result_row = {"group": "B2", "task_id": "legacy-task"}
    trace_row = {"group": "B2", "task_id": "legacy-task"}
    result.write_text(json.dumps(result_row) + "\n", encoding="utf-8")
    trace.write_text(json.dumps(trace_row) + "\n", encoding="utf-8")

    rows, trace_rows, manifest_path, manifest = module.load_audit_artifact_rows(
        result,
        trace_jsonl=trace,
        manifest_path=None,
    )

    assert rows == [result_row]
    assert trace_rows == [trace_row]
    assert manifest_path is None
    assert manifest is None


@pytest.mark.parametrize("symlink_kind", ["directory", "file"])
def test_cost_audit_preserves_legacy_symlink_loading(
    symlink_kind: str,
    tmp_path: Path,
) -> None:
    module = _load_module(
        COST_AUDIT_SCRIPT,
        f"audit_draco_legacy_{symlink_kind}_symlink_test",
    )
    actual = tmp_path / "actual"
    actual.mkdir()
    actual_result = actual / "legacy-result.jsonl"
    actual_trace = actual / "legacy-trace.jsonl"
    result_row = {"group": "B2", "task_id": "legacy-task"}
    trace_row = {"group": "B2", "task_id": "legacy-task"}
    actual_result.write_text(json.dumps(result_row) + "\n", encoding="utf-8")
    actual_trace.write_text(json.dumps(trace_row) + "\n", encoding="utf-8")
    if symlink_kind == "directory":
        alias = tmp_path / "alias"
        alias.symlink_to(actual, target_is_directory=True)
        result = alias / actual_result.name
        trace = alias / actual_trace.name
    else:
        result = actual / "result-link.jsonl"
        trace = actual / "trace-link.jsonl"
        result.symlink_to(actual_result)
        trace.symlink_to(actual_trace)

    rows, trace_rows, manifest_path, manifest = module.load_audit_artifact_rows(
        result,
        trace_jsonl=trace,
        manifest_path=None,
    )

    assert rows == [result_row]
    assert trace_rows == [trace_row]
    assert manifest_path is None
    assert manifest is None


def test_formal_wrapper_keeps_reference_input_and_direct_openrouter_runtime() -> None:
    script = FORMAL_WRAPPER.read_text(encoding="utf-8")

    assert "benchmark_input.enforce_reference_input=false" not in script
    assert 'if [[ "${DRACO_DRY_RUN:-0}" != "0" ]]' in script
    assert "unset OPENROUTER_BASE_URL OPENSQUILLA_LLM_PROXY" in script
    assert "export OPENSQUILLA_TRUST_ENV=0" in script
    assert "export OPENSQUILLA_PROVIDER_ROUTING_STRICT=1" in script
    assert "export OPENSQUILLA_OPENROUTER_METADATA_REQUIRED=1" in script
    assert "export OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS=1" in script
    assert "export OPENSQUILLA_OPENROUTER_DISABLE_RESPONSE_CACHE=1" in script
    assert 'extra_args+=(--local-web-tools-smoke-only)' in script
    assert '"${extra_args[@]}"' in script
    assert "prepare_draco_b2_canary.py" in script
    assert "validate_openrouter_b2_routes.py" in script
    assert "--reference-effective-config" in script
    assert "--expected-cache-namespace-sha256" in script
    assert "FORMAL_RUN_SUCCESS.json" in script
    assert "--require-result-evidence" in script
    assert "--max-selected-tool-failure-rate 0.5" in script
    assert '"web_search,web_fetch" 1' in script
    assert "pyproject.toml uv.lock" in script
    assert "capture_draco_runtime_environment.py capture" in script
    assert "capture_draco_runtime_environment.py verify" in script
    assert "artifact-snapshot.json" in script
    assert "--recursive" in script
    assert "--allow-after FORMAL_RUN_SUCCESS.json" in script
    assert "DRACO_OPENROUTER_MIN_REMAINING_USD:-100" in script
    assert "(.limit_remaining | tonumber) >= $minimum" in script


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return b'{"data":{"usage":1.25,"byok_usage":0}}'


class _PayloadResponse(_FakeResponse):
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_account_snapshot_verifies_benchmark_environment_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_module(CAPTURE_SCRIPT, "capture_openrouter_account_usage_test")
    key = "openrouter-test-key"
    secret = tmp_path / "openrouter.key"
    output = tmp_path / "snapshot.json"
    secret.write_text(key, encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_a, **_kw: _FakeResponse())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(CAPTURE_SCRIPT),
            str(output),
            "--secret-file",
            str(secret),
            "--expected-key-env",
            "OPENROUTER_API_KEY",
        ],
    )

    assert module.main() == 0
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    assert snapshot["api_key_sha256"] == hashlib.sha256(key.encode()).hexdigest()
    assert snapshot["benchmark_environment_key_verified"] is True
    assert snapshot["usage"] == "1.25"
    assert snapshot["byok_usage"] == "0"
    assert os.stat(output).st_mode & 0o777 == 0o600


def test_account_snapshot_rejects_different_environment_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_module(CAPTURE_SCRIPT, "capture_openrouter_account_usage_mismatch_test")
    secret = tmp_path / "openrouter.key"
    output = tmp_path / "snapshot.json"
    secret.write_text("file-key", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "different-process-key")
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *_a, **_kw: pytest.fail("network must not be called after key mismatch"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(CAPTURE_SCRIPT),
            str(output),
            "--secret-file",
            str(secret),
            "--expected-key-env",
            "OPENROUTER_API_KEY",
        ],
    )

    with pytest.raises(SystemExit, match="does not match"):
        module.main()
    assert not output.exists()


def test_account_snapshot_waits_for_recorded_cost_settlement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_module(CAPTURE_SCRIPT, "capture_openrouter_settlement_test")
    key = "dedicated-key"
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    secret = tmp_path / "openrouter.key"
    baseline = tmp_path / "before.json"
    result = tmp_path / "result.jsonl"
    output = tmp_path / "after.json"
    secret.write_text(key, encoding="utf-8")
    baseline.write_text(
        json.dumps(
            {
                "usage": "1.0",
                "byok_usage": "0",
                "api_key_sha256": fingerprint,
                "benchmark_environment_key_verified": True,
            }
        ),
        encoding="utf-8",
    )
    result.write_text(
        json.dumps(
            {
                "cost_accounting": {
                    "llm_total": {
                        "recorded_cost_usd": "0.5",
                        "request_count": 1,
                        "exact_request_count": 1,
                        "cost_exact": True,
                    }
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    responses = iter(
        [
            _PayloadResponse({"data": {"usage": 1.1, "byok_usage": 0}}),
            _PayloadResponse({"data": {"usage": 1.5, "byok_usage": 0}}),
        ]
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_a, **_kw: next(responses))
    monkeypatch.setattr(module.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(CAPTURE_SCRIPT),
            str(output),
            "--secret-file",
            str(secret),
            "--expected-key-env",
            "OPENROUTER_API_KEY",
            "--settle-from",
            str(baseline),
            "--settle-result-jsonl",
            str(result),
        ],
    )

    assert module.main() == 0
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    assert snapshot["settlement"]["attempts"] == 2
    assert snapshot["settlement"]["expected_recorded_cost_usd"] == "0.5"
    assert snapshot["settlement"]["observed_usage_delta_usd"] == "0.5"


def test_account_settlement_rejects_wrong_baseline_key_before_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_module(CAPTURE_SCRIPT, "capture_openrouter_baseline_key_test")
    key = "dedicated-key"
    secret = tmp_path / "openrouter.key"
    baseline = tmp_path / "before.json"
    result = tmp_path / "result.jsonl"
    secret.write_text(key, encoding="utf-8")
    baseline.write_text(
        json.dumps(
            {
                "usage": "1",
                "byok_usage": "0",
                "api_key_sha256": "0" * 64,
                "benchmark_environment_key_verified": True,
            }
        ),
        encoding="utf-8",
    )
    result.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *_a, **_kw: pytest.fail("network must not be called"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(CAPTURE_SCRIPT),
            str(tmp_path / "after.json"),
            "--secret-file",
            str(secret),
            "--expected-key-env",
            "OPENROUTER_API_KEY",
            "--settle-from",
            str(baseline),
            "--settle-result-jsonl",
            str(result),
        ],
    )

    with pytest.raises(ValueError, match="fingerprint does not match"):
        module.main()


def test_brave_env_cannot_override_openrouter_key(tmp_path: Path) -> None:
    openrouter = tmp_path / "openrouter.key"
    brave = tmp_path / "brave.env"
    openrouter.write_text("expected-openrouter-key\n", encoding="utf-8")
    brave.write_text(
        "BRAVE_SEARCH_API_KEY=expected-brave-key\n"
        "OPENROUTER_API_KEY=must-not-escape-subshell\n",
        encoding="utf-8",
    )
    openrouter.chmod(0o600)
    brave.chmod(0o600)
    command = f'''
      source "{CREDENTIAL_LOADER}"
      export OPENSQUILLA_OPENROUTER_SECRET_FILE="{openrouter}"
      export OPENSQUILLA_BRAVE_ENV_FILE="{brave}"
      load_draco_benchmark_credentials
      [[ "$OPENROUTER_API_KEY" == expected-openrouter-key ]]
      [[ "$BRAVE_SEARCH_API_KEY" == expected-brave-key ]]
    '''
    subprocess.run(["bash", "-euo", "pipefail", "-c", command], check=True)


def test_cost_audit_rejects_snapshot_that_does_not_cover_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_module(COST_AUDIT_SCRIPT, "audit_draco_mini_cost_window_test")
    result = tmp_path / "result.jsonl"
    manifest = tmp_path / "manifest.json"
    trace = tmp_path / "trace.jsonl"
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    validation_before = tmp_path / "validation-before.json"
    validation_after = tmp_path / "validation-after.json"
    output = tmp_path / "audit.json"
    result.write_text(
        json.dumps(
            {
                "group": "B2",
                "task_id": "task-1",
                "final_text": "done",
                "usage_unknown_count": 0,
                "total_tool_call_count": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    trace.write_text(
        json.dumps(
            {
                "row_index": 1,
                "group": "B2",
                "task_id": "task-1",
                "task_input_sha256": "sha256:" + "b" * 64,
                "run_compatibility_fingerprint": "sha256:" + "c" * 64,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC).timestamp()
    manifest.write_text(
        json.dumps(
            {
                "status": "complete",
                "groups": ["B2"],
                "rows_written": 1,
                "task_count": 1,
                "started_at": run_start,
                "finished_at": run_start + 60,
            }
        ),
        encoding="utf-8",
    )
    snapshot_base = {
        "api_key_sha256": "a" * 64,
        "benchmark_environment_key_verified": True,
        "usage": "0",
        "byok_usage": "0",
    }
    before.write_text(
        json.dumps(
            {
                **snapshot_base,
                "captured_at": datetime.fromtimestamp(run_start + 5, UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    after.write_text(
        json.dumps(
            {
                **snapshot_base,
                "captured_at": datetime.fromtimestamp(run_start + 70, UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    validation_before.write_text(
        json.dumps(
            {
                **snapshot_base,
                "captured_at": datetime.fromtimestamp(run_start - 20, UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    validation_after.write_text(
        json.dumps(
            {
                **snapshot_base,
                "usage": "0.25",
                "captured_at": datetime.fromtimestamp(run_start - 10, UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(COST_AUDIT_SCRIPT),
            str(result),
            "--expected-tasks",
            "1",
            "--manifest",
            str(manifest),
            "--trace-jsonl",
            str(trace),
            "--account-before",
            str(before),
            "--account-after",
            str(after),
            "--validation-account-before",
            str(validation_before),
            "--validation-account-after",
            str(validation_after),
            "--require-account-reconciliation",
            "--output-json",
            str(output),
            "--output-md",
            str(tmp_path / "audit.md"),
        ],
    )

    assert module.main() == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    checks = {item["name"]: item for item in report["checks"]}
    assert checks["OpenRouter 对账窗口完整覆盖 benchmark"]["pass"] is False
    assert checks["canary 在正式 benchmark 前完成"]["pass"] is True
    assert report["validation_account_usage_delta_usd"] == 0.25
    assert report["launcher_account_usage_delta_usd"] == 0.25


def test_cost_audit_accepts_fully_reconciled_strict_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_module(COST_AUDIT_SCRIPT, "audit_draco_mini_strict_pass_test")
    source_head = "d" * 40
    key_fingerprint = "a" * 64
    task_prompt = "Research the synthetic benchmark topic."
    input_row = {
        "task_id": "task-1",
        "prompt": task_prompt,
        "answer": json.dumps(
            {
                "id": "rubric-1",
                "sections": [
                    {
                        "id": "section-1",
                        "title": "Evidence",
                        "criteria": [
                            {
                                "id": "criterion-1",
                                "weight": 1,
                                "requirement": "The answer contains supported evidence.",
                            }
                        ],
                    }
                ],
            }
        ),
    }
    expected_input = tmp_path / "input.jsonl"
    expected_input.write_text(json.dumps(input_row) + "\n", encoding="utf-8")
    task_hash = module.canonical_json_sha256(
        module.normalize_expected_draco_task(input_row)
    )
    tool_policy = {
        "tool_mode": "local_web_tools",
        "tools_enabled": True,
        "tool_names": ["web_search", "web_fetch"],
        "contamination_blocked_domains": list(module.EXPECTED_BLOCKED_DOMAINS),
        "local_web_tools": {
            "web_search": {
                "provider": "brave",
                "api_key_env": "BRAVE_SEARCH_API_KEY",
                "max_results": 5,
                "excluded_domains": list(module.EXPECTED_BLOCKED_DOMAINS),
            },
            "web_fetch": {
                "blocked_domains": list(module.EXPECTED_BLOCKED_DOMAINS),
                "max_content_tokens": 50_000,
                "max_content_chars": 200_000,
                "allow_firecrawl": False,
            },
            "search_runtime": {
                "configured_provider": "brave",
                "provider": "brave",
                "max_results": 5,
                "api_key_configured": True,
                "api_key_source": "env:BRAVE_SEARCH_API_KEY",
                "api_key_env": "BRAVE_SEARCH_API_KEY",
                "credential_status": "configured",
                "runtime_configured": True,
                "proxy_configured": False,
                "use_env_proxy": False,
                "fallback_policy": "off",
                "diagnostics": False,
            },
            "sandbox_runtime": {
                "configured": True,
                "backend": "bubblewrap",
                "approval_queue": "auto_deny_unattended",
                "effective": {
                    "sandbox_enabled": True,
                    "grading_enabled": True,
                    "default_level": "L1-standard",
                    "backend": "auto",
                    "insecure_mode": False,
                    "notes": [],
                },
            },
            "fetch_runtime": {
                "extractor_mode": "auto_local_first",
                "firecrawl_allowed": False,
                "firecrawl_api_key_active": False,
                "external_fetch_cost_tracking": "not_applicable",
            },
            "preflight": {
                "status": "passed",
                "preflight_calls": {"web_search": 1, "web_fetch": 1},
            },
        },
    }
    tool_policy["group_tool_policies"] = {
        "B2": {key: value for key, value in tool_policy.items()}
    }
    contract = {
        "tools": tool_policy["group_tool_policies"]["B2"],
        "resolved_llm_runtime": {
            "provider": "openrouter",
            "api_key_sha256": "sha256:" + key_fingerprint,
            "base_url": module.EXPECTED_OPENROUTER_BASE_URL,
            "base_url_from_env": False,
            "proxy": "",
            "provider_routing_strict": True,
            "stream_error_frames": True,
            "router_metadata_required": True,
            "require_parameters": True,
            "response_cache_disabled": True,
            "cache_namespace_enabled": False,
            "cache_namespace_required": False,
            "cache_namespace_sha256": "",
            "trust_env": False,
            "ambient_proxies": {},
            "provider_routing": dict(module.EXPECTED_PROVIDER_ROUTING),
        }
    }
    fingerprint = module.canonical_json_sha256(contract)

    def router_evidence(model: str) -> dict[str, object]:
        slug = module.EXPECTED_PROVIDER_ROUTING[model]
        provider_name = module.EXPECTED_PROVIDER_DISPLAY_NAMES[slug]
        actual_model = module.EXPECTED_MODEL_REVISIONS[model]
        return {
            "is_byok": False,
            "cost": 0.01,
            "response_ids": [f"gen-{slug}"],
            "router_metadata": {
                "requested": model,
                "strategy": "direct",
                "attempt": 1,
                "is_byok": False,
                "endpoints": {
                    "total": 1,
                    "available": [
                        {
                            "provider": provider_name,
                            "model": actual_model,
                            "selected": True,
                        }
                    ],
                },
                "attempts": [
                    {
                        "provider": provider_name,
                        "model": actual_model,
                        "status": 200,
                    }
                ],
                "pipeline": [],
            },
        }

    generation_calls = [
        {
            "role": role,
            "provider": "openrouter",
            "model": module.EXPECTED_MODEL_REVISIONS[model],
            "agent_call_index": 1,
            "input_tokens": 1,
            "output_tokens": 1,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": router_evidence(model),
        }
        for role, model in module.EXPECTED_ROLE_MODEL_PAIRS
    ]
    judge_usage = {
        "model": module.EXPECTED_MODEL_REVISIONS[module.EXPECTED_JUDGE],
        "input_tokens": 1,
        "output_tokens": 1,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": router_evidence(module.EXPECTED_JUDGE),
    }
    judge_run = {
        "llm_request_count": 1,
        "usage": judge_usage,
    }
    final_text_sha256 = hashlib.sha256(b"done").hexdigest()
    selected_trace_events = [
        {
            "seq": 0,
            "elapsed_ms": 1,
            "kind": "routing_setup",
            "routing": {"routing_source": "b2_quality_first_profile"},
            "usage": [],
        }
    ]
    row = {
        "row_index": 1,
        "group": "B2",
        "task_id": "task-1",
        "prompt": task_prompt,
        "prompt_sha256": hashlib.sha256(task_prompt.encode()).hexdigest(),
        "task_input_sha256": task_hash,
        "run_compatibility_fingerprint": fingerprint,
        "final_text": "done",
        "final_text_sha256": final_text_sha256,
        "quality_total": 100.0,
        "error": None,
        "usage_unknown_count": 0,
        "total_tool_call_count": 0,
        "execution": {
            "selected_generation_attempt": 1,
            "generation_attempts": [
                {
                    "attempt": 1,
                    "run": {
                        "llm_request_count": 5,
                        "usage": {"model_usage_breakdown": generation_calls},
                        "trace_events": selected_trace_events,
                        "error": None,
                        "final_text_sha256": final_text_sha256,
                    },
                }
            ]
        },
        "judge": {
            "mode": "draco_criterion_judgments",
            "rubric_id": "rubric-1",
            "judge_model": module.EXPECTED_JUDGE,
            "judge_repeats": 1,
            "rubric_criteria_count": 1,
            "criteria_count": 1,
            "valid_criteria_count": 1,
            "invalid_criteria_count": 0,
            "score_status": "complete",
            "judge_error_count": 0,
            "criterion_judgments": [
                {
                    "id": "criterion-1",
                    "section_id": "section-1",
                    "section_title": "Evidence",
                    "weight": 1,
                    "requirement": "The answer contains supported evidence.",
                    "repeat_index": 0,
                    "verdict": "MET",
                    "met": True,
                    "judge_run": judge_run,
                    "judge_attempt_count": 1,
                    "judge_attempts": [
                        {
                            "attempt": 1,
                            "verdict": "MET",
                            "met": True,
                            "run": judge_run,
                        }
                    ]
                }
            ],
        },
        "candidate_judges": [],
        "run_trace": {
            "event_count": 1,
            "events": selected_trace_events,
        },
        "cost_accounting": {
            "llm_total": {
                "request_count": 6,
                "exact_request_count": 6,
                "recorded_cost_usd": 0.06,
                "cost_exact": True,
            },
            "external_tools": {
                "potentially_unpriced_tool_call_count_upper_bound": 0,
                "cost_exact": True,
            },
        },
        "openrouter_non_byok_audit": {"pass": True},
    }
    row = seal_result_row(row)
    result = tmp_path / "result.jsonl"
    trace = tmp_path / "trace.jsonl"
    manifest = tmp_path / "manifest.json"
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    output = tmp_path / "audit.json"
    result.write_text(json.dumps(row) + "\n", encoding="utf-8")
    trace.write_text(json.dumps(trace_row_from_result(row)) + "\n", encoding="utf-8")
    run_start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC).timestamp()
    manifest.write_text(
        json.dumps(
            {
                "status": "complete",
                "groups": ["B2"],
                "rows_written": 1,
                "task_count": 1,
                "started_at": run_start,
                "finished_at": run_start + 10,
                "source_provenance": {"git_head": source_head},
                "run_compatibility": {
                    "fingerprints": {"B2": fingerprint},
                    "contracts": {"B2": contract},
                },
                "tool_policy": tool_policy,
            }
        ),
        encoding="utf-8",
    )
    snapshot = {
        "api_key_sha256": key_fingerprint,
        "benchmark_environment_key_verified": True,
        "byok_usage": "0",
    }
    before.write_text(
        json.dumps(
            {
                **snapshot,
                "usage": "1.00",
                "captured_at": datetime.fromtimestamp(run_start - 5, UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    after.write_text(
        json.dumps(
            {
                **snapshot,
                "usage": "1.06",
                "captured_at": datetime.fromtimestamp(run_start + 15, UTC).isoformat(),
                "settlement": {
                    "attempts": 1,
                    "expected_recorded_cost_usd": "0.06",
                    "observed_usage_delta_usd": "0.06",
                    "tolerance_usd": "0.000001",
                },
            }
        ),
        encoding="utf-8",
    )

    def fake_git_run(command, **_kwargs):
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            stdout = source_head + "\n"
        else:
            stdout = ""
        return module.subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_git_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(COST_AUDIT_SCRIPT),
            str(result),
            "--expected-tasks",
            "1",
            "--expected-input-jsonl",
            str(expected_input),
            "--manifest",
            str(manifest),
            "--trace-jsonl",
            str(trace),
            "--account-before",
            str(before),
            "--account-after",
            str(after),
            "--require-account-reconciliation",
            "--require-clean-source-now",
            "--external-preflight-call-count",
            "1",
            "--output-json",
            str(output),
            "--output-md",
            str(tmp_path / "audit.md"),
        ],
    )

    assert module.main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["pass"] is True
    assert report["openrouter_llm_judge_cost_exact"] is True
    assert report["all_provider_cost_exact"] is False
    assert report["all_provider_total_cost_usd"] is None
    assert report["generation_response_models"] == sorted(
        module.EXPECTED_MODEL_REVISIONS[model] for model in module.EXPECTED_MODELS
    )
    assert report["judge_response_models"] == [
        module.EXPECTED_MODEL_REVISIONS[module.EXPECTED_JUDGE]
    ]

    def successful_web_trace(elapsed_ms: int) -> list[dict[str, object]]:
        return [
            {
                "seq": 0,
                "elapsed_ms": elapsed_ms,
                "kind": "routing_setup",
                "routing": {"routing_source": "b2_quality_first_profile"},
                "usage": [],
            },
            {
                "seq": 1,
                "elapsed_ms": elapsed_ms + 1,
                "kind": "tool_use_start",
                "tool_use_id": "reused-across-attempts",
                "tool_name": "web_search",
            },
            {
                "seq": 2,
                "elapsed_ms": elapsed_ms + 2,
                "kind": "tool_result",
                "tool_use_id": "reused-across-attempts",
                "tool_name": "web_search",
                "is_error": False,
                "execution_status": {"status": "success", "reason": ""},
                "diagnostic": {
                    "ok": True,
                    "error_present": False,
                    "http_status": 200,
                },
            },
        ]

    first_attempt_trace = successful_web_trace(10)
    selected_retry_trace = successful_web_trace(20)
    retry_row = json.loads(json.dumps(row))
    retry_row["total_tool_call_count"] = 2
    retry_row["execution"]["selected_generation_attempt"] = 2
    retry_row["execution"]["generation_attempts"] = [
        {
            "attempt": 1,
            "run": {
                "llm_request_count": 5,
                "usage": {"model_usage_breakdown": generation_calls},
                "trace_events": first_attempt_trace,
                "error": "temporary_failure",
                "final_text_sha256": hashlib.sha256(b"").hexdigest(),
            },
        },
        {
            "attempt": 2,
            "run": {
                "llm_request_count": 5,
                "usage": {"model_usage_breakdown": generation_calls},
                "trace_events": selected_retry_trace,
                "error": None,
                "final_text_sha256": final_text_sha256,
            },
        },
    ]
    retry_row["run_trace"] = {
        "event_count": len(selected_retry_trace),
        "events": selected_retry_trace,
    }
    retry_row["cost_accounting"]["llm_total"] = {
        "request_count": 11,
        "exact_request_count": 11,
        "recorded_cost_usd": 0.11,
        "cost_exact": True,
    }
    retry_row["cost_accounting"]["external_tools"] = {
        "potentially_unpriced_tool_call_count_upper_bound": 2,
        "cost_exact": False,
    }
    retry_row = seal_result_row(retry_row)
    result.write_text(json.dumps(retry_row) + "\n", encoding="utf-8")
    trace.write_text(
        json.dumps(trace_row_from_result(retry_row)) + "\n", encoding="utf-8"
    )
    after.write_text(
        json.dumps(
            {
                **snapshot,
                "usage": "1.11",
                "captured_at": datetime.fromtimestamp(run_start + 15, UTC).isoformat(),
                "settlement": {
                    "attempts": 1,
                    "expected_recorded_cost_usd": "0.11",
                    "observed_usage_delta_usd": "0.11",
                    "tolerance_usd": "0.000001",
                },
            }
        ),
        encoding="utf-8",
    )
    sys.argv.extend(["--required-observed-tools", "web_search"])

    assert module.main() == 0
    retry_report = json.loads(output.read_text(encoding="utf-8"))
    assert retry_report["successful_tool_names"] == {"web_search": 2}
    assert retry_report["selected_successful_tool_names"] == {"web_search": 1}

    for diagnostic_field in ("http_status", "status"):
        failed_tool_row = json.loads(json.dumps(retry_row))
        failed_selected_events = failed_tool_row["execution"][
            "generation_attempts"
        ][1]["run"]["trace_events"]
        failed_selected_events[-1]["diagnostic"][diagnostic_field] = 404
        failed_tool_row["run_trace"] = {
            "event_count": len(failed_selected_events),
            "events": failed_selected_events,
        }
        failed_tool_row = seal_result_row(failed_tool_row)
        result.write_text(json.dumps(failed_tool_row) + "\n", encoding="utf-8")
        trace.write_text(
            json.dumps(trace_row_from_result(failed_tool_row)) + "\n",
            encoding="utf-8",
        )

        assert module.main() == 1
        failed_tool_report = json.loads(output.read_text(encoding="utf-8"))
        assert failed_tool_report["selected_successful_tool_names"] == {}
        assert failed_tool_report["failed_tool_names"] == {"web_search": 1}

    result.write_text(json.dumps(retry_row) + "\n", encoding="utf-8")
    trace.write_text(
        json.dumps(trace_row_from_result(retry_row)) + "\n", encoding="utf-8"
    )

    changed_input = dict(input_row)
    changed_input["answer"] = json.dumps(
        {
            "id": "rubric-1",
            "sections": [
                {
                    "id": "section-1",
                    "title": "Evidence",
                    "criteria": [
                        {
                            "id": "criterion-1",
                            "weight": 1,
                            "requirement": "A deliberately changed requirement.",
                        }
                    ],
                }
            ],
        }
    )
    expected_input.write_text(json.dumps(changed_input) + "\n", encoding="utf-8")

    assert module.main() == 1
    changed_report = json.loads(output.read_text(encoding="utf-8"))
    assert changed_report["task_input_mismatch_task_ids"] == ["task-1"]
