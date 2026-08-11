from __future__ import annotations

import argparse
import contextlib
import copy
import gc
import importlib.util
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
FINALIZER_TEST = ROOT / "tests/test_scripts/test_finalize_draco_campaign.py"
SCALE_TEST_ENV = "OPENSQUILLA_RUN_FINALIZER_SCALE_TESTS"
SCALE_PAIR_COUNT = 50
MAX_RSS_DELTA_MIB = 192.0


def _load_finalizer_helpers() -> Any:
    spec = importlib.util.spec_from_file_location(
        "finalizer_streaming_scale_helpers",
        FINALIZER_TEST,
    )
    assert spec is not None and spec.loader is not None
    helpers = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helpers
    spec.loader.exec_module(helpers)
    return helpers


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    path.chmod(0o600)


def _remap_attempt_ids(
    row: dict[str, object],
    *,
    task_index: int,
    group_index: int,
) -> None:
    execution = row["execution"]
    assert isinstance(execution, dict)
    attempts = execution["generation_attempts"]
    assert isinstance(attempts, list)
    assert len(attempts) == 1 and isinstance(attempts[0], dict)
    attempts[0]["attempt_id"] = (
        f"{1_000_000 + task_index * 100 + group_index:032x}"
    )
    judge = row["judge"]
    assert isinstance(judge, dict)
    judgments = judge["criterion_judgments"]
    assert isinstance(judgments, list)
    for judgment_index, judgment in enumerate(judgments):
        assert isinstance(judgment, dict)
        judge_attempts = judgment["judge_attempts"]
        assert isinstance(judge_attempts, list)
        assert len(judge_attempts) == 1 and isinstance(judge_attempts[0], dict)
        judge_attempts[0]["attempt_id"] = (
            f"{2_000_000 + task_index * 10_000 + group_index * 100 + judgment_index:032x}"
        )


def _repair_row(module: Any, row: dict[str, object]) -> dict[str, object]:
    value = copy.deepcopy(row)
    value["completed_at"] = 1_020.0
    execution = value["execution"]
    assert isinstance(execution, dict)
    execution.update(
        {
            "prior_generation_attempts_used": 1,
            "resume_action": "metadata_only",
            "generation_reused": True,
            "metadata_repaired": True,
            "judge_reran": False,
        }
    )
    value["resume_completion"] = {
        "action": "metadata_only",
        "generation_reused": True,
        "metadata_repaired": True,
        "judge_reran": False,
        "post_repair_action": "complete",
        "status": "complete",
        "incomplete_reasons": [],
    }
    judge = value["judge"]
    assert isinstance(judge, dict)
    judgments = judge["criterion_judgments"]
    assert isinstance(judgments, list)
    for judgment in judgments:
        assert isinstance(judgment, dict)
        judgment["prior_judge_attempts_used"] = 1
        judgment["judge_new_attempt_count"] = 0
    judge["judge_new_attempt_count"] = 0
    return module.seal_result_row(value)


def _build_scale_campaign(
    module: Any,
    helpers: Any,
    root: Path,
    *,
    target_mib: int,
) -> tuple[argparse.Namespace, int, dict[str, int]]:
    args, _, lock_fd = helpers._campaign(module, root, with_repair=True)
    base_task = json.loads(args.input.read_text(encoding="utf-8"))
    tasks: list[dict[str, object]] = []
    for task_index in range(10):
        task = copy.deepcopy(base_task)
        task["id"] = f"task-{task_index + 1:02d}"
        rubric = task["rubric"]
        assert isinstance(rubric, dict)
        rubric["id"] = f"rubric-{task_index + 1:02d}"
        tasks.append(task)
    _write_jsonl(args.input, tasks)
    module.FROZEN_DRACO_MINI_TASK_COUNT = len(tasks)
    module.FROZEN_DRACO_MINI_SHA256 = module.file_sha256(args.input)

    first_manifest = json.loads(args.manifest[0].read_text(encoding="utf-8"))
    compatibility = first_manifest["run_compatibility"]
    assert isinstance(compatibility, dict)
    contracts = compatibility["contracts"]
    assert isinstance(contracts, dict)
    for contract in contracts.values():
        assert isinstance(contract, dict)
        profile = contract["global_experiment_profile"]
        assert isinstance(profile, dict)
        benchmark_input = profile["benchmark_input"]
        assert isinstance(benchmark_input, dict)
        benchmark_input.update(
            {
                "sha256": module.FROZEN_DRACO_MINI_SHA256,
                "task_count": len(tasks),
            }
        )
    fingerprints = {
        group: module.canonical_sha256(contract, prefix=True)
        for group, contract in contracts.items()
    }

    wave1_rows: list[dict[str, object]] = []
    for task_index, task in enumerate(tasks):
        for group_index, group in enumerate(module.GROUPS):
            row = helpers._row(
                module,
                group=group,
                task=task,
                fingerprint=fingerprints[group],
                response_prefix=f"scale-{task_index:02d}-{group.lower()}",
            )
            _remap_attempt_ids(
                row,
                task_index=task_index,
                group_index=group_index,
            )
            wave1_rows.append(module.seal_result_row(row))
    assert len(wave1_rows) == SCALE_PAIR_COUNT
    _write_jsonl(args.result[0], wave1_rows)
    repair_rows = [_repair_row(module, row) for row in wave1_rows]

    target_bytes = target_mib * 1024 * 1024
    pair_bytes = {
        (str(row["group"]), str(row["task_id"])): 0
        for row in wave1_rows
    }
    padded_rows = 0
    wave2 = args.result[1]
    with wave2.open("w", encoding="utf-8", newline="") as handle:
        source_bytes = args.result[0].stat().st_size
        while source_bytes < target_bytes:
            for base in repair_rows:
                if source_bytes >= target_bytes:
                    break
                value = copy.deepcopy(base)
                value["scale_padding"] = "x" * (1024 * 1024)
                line = json.dumps(
                    module.seal_result_row(value),
                    ensure_ascii=False,
                ) + "\n"
                handle.write(line)
                line_bytes = len(line.encode("utf-8"))
                source_bytes += line_bytes
                pair_bytes[(str(base["group"]), str(base["task_id"]))] += (
                    line_bytes
                )
                padded_rows += 1
        for base in repair_rows:
            line = json.dumps(base, ensure_ascii=False) + "\n"
            handle.write(line)
            line_bytes = len(line.encode("utf-8"))
            source_bytes += line_bytes
            pair_bytes[(str(base["group"]), str(base["task_id"]))] += line_bytes
    wave2.chmod(0o600)
    peak_pair_bytes = max(pair_bytes.values())
    assert peak_pair_bytes < module.FINALIZER_MAX_PAIR_SOURCE_BYTES

    for manifest_index, manifest_path in enumerate(args.manifest):
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["task_ids"] = [str(task["id"]) for task in tasks]
        payload["rows_written"] = (
            len(wave1_rows)
            if manifest_index == 0
            else padded_rows + len(repair_rows)
        )
        payload["command"]["parsed_args"]["max_tasks"] = len(tasks)
        payload["run_compatibility"] = {
            "fingerprints": fingerprints,
            "contracts": contracts,
        }
        helpers._owner_json(manifest_path, payload)

    usage_delta = 3.9 * len(tasks)
    usage_after = str(10 + usage_delta)
    after = json.loads(args.account_after.read_text(encoding="utf-8"))
    after["usage"] = usage_after
    helpers._owner_json(args.account_after, after)
    reconciliation = json.loads(
        args.account_reconciliation.read_text(encoding="utf-8")
    )
    reconciliation["usage_after_usd"] = usage_after
    reconciliation["usage_delta_usd"] = str(usage_delta)
    for observation in reconciliation["stable_observations"]:
        observation["usage"] = usage_after
    helpers._owner_json(args.account_reconciliation, reconciliation)
    return args, lock_fd, {
        "source_bytes": source_bytes,
        "padded_rows": padded_rows,
        "expected_history_rows": len(wave1_rows) + padded_rows + len(repair_rows),
        "peak_pair_bytes": peak_pair_bytes,
    }


@contextlib.contextmanager
def _silence_process_output() -> Iterator[None]:
    sys.stdout.flush()
    sys.stderr.flush()
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    sink = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(sink, 1)
        os.dup2(sink, 2)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
        os.close(saved_stdout)
        os.close(saved_stderr)
        os.close(sink)


def _rss_mib() -> float:
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / divisor


def _run_scale_probe(target_mib: int) -> dict[str, object]:
    helpers = _load_finalizer_helpers()
    module = helpers._load()
    archives: list[Any] = []
    original_archive = module.FinalizerSourceArchive

    class TrackedArchive(original_archive):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            archives.append(self)

    module.FinalizerSourceArchive = TrackedArchive
    with tempfile.TemporaryDirectory(prefix="finalizer-streaming-scale-") as raw:
        with _silence_process_output():
            args, lock_fd, fixture = _build_scale_campaign(
                module,
                helpers,
                Path(raw),
                target_mib=target_mib,
            )
        gc.collect()
        baseline_rss = _rss_mib()
        started = time.monotonic()
        try:
            manifest = module.run_finalization(args)
        finally:
            os.close(lock_fd)
        elapsed = time.monotonic() - started
        peak_rss = _rss_mib()
        assert len(archives) == 1
        archive = archives[0]
        return {
            "status": manifest["status"],
            "result_count": manifest["result_count"],
            "source_mib": fixture["source_bytes"] / 1024.0 / 1024.0,
            "padded_rows": fixture["padded_rows"],
            "expected_history_rows": fixture["expected_history_rows"],
            "peak_pair_mib": fixture["peak_pair_bytes"] / 1024.0 / 1024.0,
            "materialized_history_rows": archive.materialized_row_count,
            "peeked_selected_rows": archive.peeked_row_count,
            "archive_closed": archive.closed,
            "baseline_rss_mib": baseline_rss,
            "peak_rss_mib": peak_rss,
            "delta_rss_mib": peak_rss - baseline_rss,
            "elapsed_s": elapsed,
        }


@pytest.mark.skipif(
    os.environ.get(SCALE_TEST_ENV) != "1" or not sys.platform.startswith("linux"),
    reason=(
        f"set {SCALE_TEST_ENV}=1 on Linux to run 500 MiB/1 GiB RSS gates"
    ),
)
@pytest.mark.parametrize("target_mib", [500, 1024])
def test_finalizer_streaming_scale_rss_is_bounded(target_mib: int) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", str(target_mib)],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert completed.returncode == 0, completed.stderr[-20_000:]
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["status"] == "complete"
    assert payload["result_count"] == SCALE_PAIR_COUNT
    assert payload["source_mib"] >= target_mib
    assert payload["materialized_history_rows"] == payload["expected_history_rows"]
    assert payload["peeked_selected_rows"] == SCALE_PAIR_COUNT
    assert payload["archive_closed"] is True
    assert payload["peak_pair_mib"] < 32.0
    assert payload["delta_rss_mib"] <= MAX_RSS_DELTA_MIB
    assert payload["elapsed_s"] <= target_mib * 0.25 + 60.0


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("target_mib", type=int)
    args = parser.parse_args()
    if not args.probe:
        parser.error("only --probe subprocess execution is supported")
    print(json.dumps(_run_scale_probe(args.target_mib), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
