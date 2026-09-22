from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.engine.routing import legacy_benchmark_worker as worker
from opensquilla.engine.routing.benchmark_worker import BenchmarkRouteOnlyError


def _input(item: str = "item-a", **overrides):
    value = {
        "current_request": "Explain why sorting n numbers often takes n log n comparisons.",
        "task_anchor": "",
        "history_user": [],
        "previous_answer": "",
        "previous_usage": {},
        "previous_outcome": "unknown",
        "active_route_tier": None,
        "route_history": [],
        "context": {},
        "tool_state": {},
        "attachments": [],
    }
    value.update(overrides)
    return {"item_id": item, "input": value}


def _pool():
    return {
        tier: {
            "model_id": worker._MODELS[index],
            "revision": worker._REVISIONS[index],
            "definition_hash": worker._sha256(
                worker.canonical_json_bytes(
                    {
                        "provider": "openrouter",
                        "model": worker._MODELS[index],
                        "reasoning": "thinking" if index == 0 else "max",
                        "deployment_version": worker._REVISIONS[index],
                    }
                )
            ),
        }
        for index, tier in enumerate(worker._TIERS)
    }


def _request(tmp_path, *, rows=None, pool=None):
    input_path = tmp_path / "input.jsonl"
    input_payload = b"".join(
        worker.canonical_json_bytes(row) + b"\n" for row in (rows or [_input()])
    )
    input_path.write_bytes(input_payload)
    pool_path = tmp_path / "pool.json"
    pool_payload = worker.canonical_json_bytes(pool or _pool())
    pool_path.write_bytes(pool_payload)
    request = {
        "schema_version": worker.REQUEST_SCHEMA,
        "input_path": str(input_path),
        "input_sha256": worker._sha256(input_payload),
        "model_pool_path": str(pool_path),
        "model_pool_sha256": worker._sha256(pool_payload),
        "output_dir": str(tmp_path / "output"),
    }
    request_path = tmp_path / "request.json"
    request_path.write_bytes(worker.canonical_json_bytes(request))
    return request_path, request


@pytest.mark.skipif(
    any(
        importlib.util.find_spec(name) is None
        for name in (
            "numpy",
            "lightgbm",
            "onnxruntime",
            "joblib",
            "sklearn",
            "tokenizers",
        )
    ),
    reason="native dependencies unavailable",
)
def test_native_single_item_process(tmp_path):
    request_path, request = _request(tmp_path)
    environment = dict(os.environ)
    environment.update(worker._OFFLINE_ENV)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(worker._repo_root() / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "opensquilla.engine.routing.legacy_benchmark_worker",
            "--request",
            str(request_path),
        ],
        cwd=worker._repo_root(),
        env=environment,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    output = Path(request["output_dir"])
    evidence = json.loads((output / "evidence.json").read_bytes())
    payload = (output / "outputs.jsonl").read_bytes()
    decision = json.loads(payload)
    assert evidence["schema_version"] == worker.EVIDENCE_SCHEMA
    assert evidence["decisions_sha256"] == worker._sha256(payload)
    assert evidence["decision_count"] == 1
    assert evidence["network_attempts"] == []
    assert evidence["downstream_dispatch_count"] == 0
    assert evidence["model_identity"]["aux_head_loaded"] is True
    assert evidence["model_identity"]["model_version"] == "v4"
    assert decision["item_id"] == "item-a"
    assert decision["routing_source"] == "v4_phase3"
    assert decision["tier"] in worker._TIERS
    assert decision["model_id"] == _pool()[decision["tier"]]["model_id"]
    assert decision["trace"]["final_tier"] == decision["tier"]
    assert decision["trace"]["final_model_id"] == decision["model_id"]
    assert decision["trace"]["routing_source"] == decision["routing_source"]
    assert decision["trace"]["thinking_mode"] is not None
    assert decision["trace"]["prompt_policy"] is not None
    assert len(evidence["model_identity"]["asset_hashes"]) == 18


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_anchor", "some anchor"),
        ("history_user", ["earlier turn"]),
        ("previous_answer", "earlier answer"),
        ("previous_usage", {"input_tokens": 1}),
        ("previous_outcome", "success"),
        ("active_route_tier", "C1"),
        ("route_history", [{"tier": "C1"}]),
        ("context", {"surface": "cli"}),
        ("tool_state", {"available": ["shell"]}),
        ("attachments", [{"mime_type": "image/png"}]),
    ],
)
def test_nonempty_context_never_silently_disappears(field, value):
    payload = worker.canonical_json_bytes(_input(**{field: value})) + b"\n"
    with pytest.raises((worker.LegacyReplayError, BenchmarkRouteOnlyError)):
        worker._load_inputs(payload)


@pytest.mark.parametrize("kind", ["duplicate", "unsorted", "oracle", "blank"])
def test_input_alignment_and_result_blindness(kind):
    rows = [_input()]
    if kind == "duplicate":
        rows *= 2
    elif kind == "unsorted":
        rows = [_input("z"), _input("a")]
    elif kind == "oracle":
        rows[0]["input"]["quality"] = 1.0
    else:
        rows[0]["input"]["current_request"] = " "
    payload = b"".join(worker.canonical_json_bytes(row) + b"\n" for row in rows)
    with pytest.raises(BenchmarkRouteOnlyError):
        worker._load_inputs(payload)


@pytest.mark.parametrize("field", ["model_id", "revision", "definition_hash"])
def test_model_pool_identity_is_pinned(field):
    pool = _pool()
    pool["C1"][field] = "changed"
    with pytest.raises(worker.LegacyReplayError, match="fixed replay deployment"):
        worker._load_pool(worker.canonical_json_bytes(pool))


def _fake_bundle(tmp_path):
    bundle = tmp_path / "bundle"
    entries = []
    for name in sorted(worker._ASSETS):
        artifact = bundle / name
        artifact.parent.mkdir(parents=True, exist_ok=True)
        payload = ("fake asset: " + name).encode()
        artifact.write_bytes(payload)
        entries.append(
            {
                "path": name,
                "size_bytes": len(payload),
                "sha256": worker._sha256(payload).removeprefix("sha256:"),
            }
        )
    core = bundle / "runtime_src/src/router/inference/core.py"
    core.parent.mkdir(parents=True)
    core.write_text("# Fake source, never imported.\n")
    manifest = bundle / "artifact_manifest.json"
    manifest.write_bytes(worker.canonical_json_bytes({"schema_version": 1, "files": entries}))
    return bundle


@pytest.mark.parametrize("kind", ["hash", "size", "missing", "manifest-duplicate", "lfs"])
def test_bundle_all_assets_are_checked_before_native_loading(tmp_path, kind):
    bundle = _fake_bundle(tmp_path)
    identity = worker._bundle_identity(bundle)
    assert len(identity["asset_hashes"]) == 18
    target = bundle / "lgbm_main.bin"
    if kind == "hash":
        target.write_bytes(b"X" * target.stat().st_size)
    elif kind == "size":
        target.write_bytes(b"short")
    elif kind == "missing":
        target.unlink()
    else:
        manifest = bundle / "artifact_manifest.json"
        data = json.loads(manifest.read_bytes())
        if kind == "manifest-duplicate":
            data["files"][1] = dict(data["files"][0])
        else:
            target.write_bytes(b"version https://git-lfs.github.com/spec/v1\n")
            entry = next(row for row in data["files"] if row["path"] == "lgbm_main.bin")
            entry["size_bytes"] = target.stat().st_size
            entry["sha256"] = worker._sha256(target.read_bytes()).removeprefix("sha256:")
        manifest.write_bytes(worker.canonical_json_bytes(data))
    with pytest.raises(worker.LegacyReplayError):
        worker._bundle_identity(bundle)


@pytest.fixture
def fake_native(monkeypatch, tmp_path):
    bundle = _fake_bundle(tmp_path)
    calls = []

    async def fake_route(rows, pool, selected_bundle):
        assert selected_bundle == bundle
        assert os.environ["OPENSQUILLA_OPENROUTER_LIVE_PRICING"] == "0"
        calls.append([row.item_id for row in rows])
        decisions = [
            {
                "item_id": row.item_id,
                "tier": "C1",
                "model_id": pool["C1"]["model_id"],
                "routing_source": "v4_phase3",
                "confidence": 0.9,
                "trace": {"thinking_mode": "T1", "prompt_policy": "P1"},
            }
            for row in rows
        ]
        return decisions, {
            "model_version": "v4",
            "feature_schema_version": "test-schema",
            "aux_head_loaded": True,
            "config": {"fixture": True},
        }

    monkeypatch.setattr(worker, "_route", fake_route)
    monkeypatch.setattr(worker.metadata, "version", lambda _name: "test-version")
    monkeypatch.setattr(worker, "_source_hashes", lambda: {"test.py": "sha256:" + "a" * 64})
    return bundle, calls


def _bundle_request(tmp_path, bundle):
    request_path, request = _request(tmp_path)
    request["bundle_dir"] = str(bundle)
    request_path.write_bytes(worker.canonical_json_bytes(request))
    return request_path, request


def test_report_is_hash_bound_atomic_and_immutable(tmp_path, monkeypatch, fake_native):
    bundle, calls = fake_native
    request_path, request = _bundle_request(tmp_path, bundle)
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "1")
    evidence = worker.run_request(request_path)
    output = Path(request["output_dir"])
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    assert set(before) == {"outputs.jsonl", "evidence.json"}
    assert evidence["input_sha256"] == request["input_sha256"]
    assert evidence["model_pool_sha256"] == request["model_pool_sha256"]
    assert evidence["decisions_sha256"] == worker._sha256(before["outputs.jsonl"])
    assert evidence["network_attempts"] == []
    assert evidence["diagnostic_only"] is True
    assert evidence["controller_effects_replayed"] is False
    assert os.environ["OPENSQUILLA_OPENROUTER_LIVE_PRICING"] == "1"
    with pytest.raises(worker.LegacyReplayError, match="immutable"):
        worker.run_request(request_path)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before
    assert calls == [["item-a"]]


@pytest.mark.parametrize("field", ["input_sha256", "model_pool_sha256"])
def test_hash_mismatch_fails_before_native(tmp_path, fake_native, field):
    bundle, calls = fake_native
    request_path, request = _bundle_request(tmp_path, bundle)
    request[field] = "sha256:" + "f" * 64
    request_path.write_bytes(worker.canonical_json_bytes(request))
    with pytest.raises(worker.LegacyReplayError, match="mismatch"):
        worker.run_request(request_path)
    assert calls == []
    assert not (Path(request["output_dir"]) / "evidence.json").exists()


def test_swallowed_network_attempt_still_prevents_success(tmp_path, monkeypatch, fake_native):
    bundle, _calls = fake_native
    request_path, request = _bundle_request(tmp_path, bundle)
    original_route = worker._route

    async def attempts_network(*args):
        with pytest.raises(worker.LegacyReplayError, match="forbids network"):
            socket.getaddrinfo("invalid.example", 443)
        return await original_route(*args)

    monkeypatch.setattr(worker, "_route", attempts_network)
    with pytest.raises(worker.LegacyReplayError, match="attempted a forbidden"):
        worker.run_request(request_path)
    assert not (Path(request["output_dir"]) / "evidence.json").exists()
    assert not (Path(request["output_dir"]) / "outputs.jsonl").exists()


def test_bundle_change_during_native_prevents_publication(tmp_path, monkeypatch, fake_native):
    bundle, _calls = fake_native
    request_path, request = _bundle_request(tmp_path, bundle)
    original_route = worker._route

    async def changes_bundle(*args):
        result = await original_route(*args)
        (bundle / "runtime_src/src/router/inference/core.py").write_text("# changed\n")
        return result

    monkeypatch.setattr(worker, "_route", changes_bundle)
    with pytest.raises(worker.LegacyReplayError, match="changed during replay"):
        worker.run_request(request_path)
    assert not (Path(request["output_dir"]) / "evidence.json").exists()


def test_atomic_publish_never_overwrites_existing_file(tmp_path):
    target = tmp_path / "evidence.json"
    target.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        worker._publish(target, b"replacement")
    assert target.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("source", ["heuristic", "v4_unavailable"])
def test_production_fallback_cannot_be_reported_as_native(tmp_path, monkeypatch, source):
    from opensquilla.engine.steps import squilla_router as step

    monkeypatch.setattr(
        step,
        "preload_strategy",
        lambda _config: SimpleNamespace(
            source=source,
            _available=False,
        ),
    )
    with pytest.raises(worker.LegacyReplayError, match="heuristic fallback is forbidden"):
        import asyncio

        asyncio.run(worker._route([], _pool(), tmp_path))
