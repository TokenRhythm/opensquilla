"""Headless four-tier Benchmark routing boundary tests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from opensquilla.engine.routing import benchmark_worker as worker
from opensquilla.engine.routing.fixed_four_tier_v2 import (
    ClassifierPrediction,
    fixed_four_tier_semantic_policy_config,
)
from opensquilla.gateway.config import FixedFourTierV2Config


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


_RUNTIME_IDENTITY = {
    "schema_version": "local_runner_identity.v2",
    "model_set_id": "benchmark-router",
    "model_manifest_hash": _hash("manifest"),
    "artifact_closure_hash": _hash("artifact-closure"),
    "runner_digest": _hash("runner"),
    "environment_digest": _hash("environment"),
    "model_type": "lightgbm",
    "execution_mode": "native_embedded",
    "registry_status": "VALIDATED",
    "input_schema_version": "lightgbm_380.v1",
}


class _FakeRegisteredModelClassifier:
    backend = "registered_model"
    feature_vector_status = "materialized"
    feature_schema_version = "lightgbm_380.v1"
    feature_vector_dim = 380
    version = "benchmark-router@test"
    instances: list[_FakeRegisteredModelClassifier] = []

    def __init__(self, **kwargs: Any) -> None:
        self.constructor = dict(kwargs)
        self.identity = dict(_RUNTIME_IDENTITY)
        self.calls: list[tuple[str, bool]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.closed = False
        self.instances.append(self)

    def predict(
        self,
        snapshot: dict[str, Any],
        allowed_tiers: tuple[str, ...] | None = None,
    ) -> ClassifierPrediction:
        route_input = snapshot["router_input"]
        request = str(route_input["current_request"])
        self.snapshots.append(snapshot)
        self.calls.append((request, allowed_tiers is not None))
        if allowed_tiers is None:
            label = "redo" if "redo" in request else "continue"
            labels = ("continue", "redo", "new_task")
        else:
            label = "c3" if "upgrade" in request else "c2" if "hard" in request else "c1"
            labels = ("c0", "c1", "c2", "c3")
        probabilities = {value: float(value == label) for value in labels}
        return ClassifierPrediction(
            label=label,
            probabilities=probabilities,
            confidence=1.0,
            version=self.version,
        )

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _registered_classifier(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeRegisteredModelClassifier.instances = []
    monkeypatch.setattr(
        worker,
        "RegisteredModelClassifier",
        _FakeRegisteredModelClassifier,
    )


def _config(tmp_path: Path) -> FixedFourTierV2Config:
    artifact_root = tmp_path / "router-artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    metadata_db = tmp_path / "models.sqlite"
    metadata_db.touch(exist_ok=True)
    return FixedFourTierV2Config(
        classifier={
            "backend": "registered_model",
            "artifact_root": str(artifact_root.resolve()),
            "metadata_db": str(metadata_db.resolve()),
            "model_set_id": "benchmark-router",
            "expected_manifest_hash": _RUNTIME_IDENTITY["model_manifest_hash"],
        }
    )


def _route_input(
    request: str,
    *,
    active_route_tier: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "current_request": request,
        "task_anchor": "",
        "history_user": [],
        "previous_answer": "",
        "previous_usage": {},
        "previous_outcome": "unknown",
        "active_route_tier": active_route_tier,
        "route_history": [],
        "context": context or {},
        "tool_state": {},
        "attachments": [],
    }


def _artifacts(
    tmp_path: Path,
    *,
    rows: list[dict[str, Any]],
    routing_session_mode: str,
) -> Path:
    config = _config(tmp_path)
    config_path = tmp_path / "router-config.json"
    config_path.write_bytes(worker.canonical_json_bytes(config))
    pool = {
        tier.upper(): {
            "model_id": deployment.model,
            "revision": deployment.deployment_version,
            "definition_hash": _hash(f"definition-{tier}"),
        }
        for tier, deployment in config.tiers.items()
    }
    pool_path = tmp_path / "model-pool.json"
    pool_path.write_bytes(worker.canonical_json_bytes(pool))
    input_path = tmp_path / "router-input.jsonl"
    input_path.write_bytes(b"".join(worker.canonical_json_bytes(row) + b"\n" for row in rows))
    request = {
        "schema_version": worker.REQUEST_SCHEMA_VERSION,
        "inference_run_id": "benchmark-run-1",
        "input_jsonl": str(input_path.resolve()),
        "router_config": str(config_path.resolve()),
        "model_pool": str(pool_path.resolve()),
        "routing_session_mode": routing_session_mode,
    }
    request_path = tmp_path / "request.json"
    request_path.write_bytes(worker.canonical_json_bytes(request))
    return request_path


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def test_independent_batch_is_result_blind_deterministic_and_never_dispatches(
    tmp_path: Path,
) -> None:
    request_path = _artifacts(
        tmp_path,
        routing_session_mode="independent",
        rows=[
            {"item_id": "a", "input": _route_input("ordinary")},
            {"item_id": "b", "input": _route_input("hard problem")},
        ],
    )

    first = worker.run_route_only(request_path, tmp_path / "first")
    first_output = (tmp_path / "first" / worker.DECISIONS_FILENAME).read_bytes()
    second = worker.run_route_only(request_path, tmp_path / "second")
    second_output = (tmp_path / "second" / worker.DECISIONS_FILENAME).read_bytes()

    # A platform worker normally snapshots the same assets into a new temp
    # directory for each launch.  Those launch-only paths must not perturb any
    # route/task identity or the decisions artifact.
    alternate = tmp_path / "alternate-launch"
    alternate.mkdir()
    raw_request = json.loads(request_path.read_bytes())
    for field, filename in (
        ("input_jsonl", "router-input.jsonl"),
        ("router_config", "router-config.json"),
        ("model_pool", "model-pool.json"),
    ):
        destination = alternate / filename
        destination.write_bytes(Path(raw_request[field]).read_bytes())
        raw_request[field] = str(destination.resolve())
    alternate_artifact_root = alternate / "router-artifacts"
    alternate_artifact_root.mkdir()
    alternate_metadata_db = alternate / "models.sqlite"
    alternate_metadata_db.touch()
    alternate_config_path = Path(raw_request["router_config"])
    alternate_config = json.loads(alternate_config_path.read_bytes())
    alternate_config["classifier"]["artifact_root"] = str(alternate_artifact_root.resolve())
    alternate_config["classifier"]["metadata_db"] = str(alternate_metadata_db.resolve())
    alternate_config_path.write_bytes(worker.canonical_json_bytes(alternate_config))
    alternate_request = alternate / "request.json"
    alternate_request.write_bytes(worker.canonical_json_bytes(raw_request))
    third = worker.run_route_only(alternate_request, tmp_path / "third")
    third_output = (tmp_path / "third" / worker.DECISIONS_FILENAME).read_bytes()

    assert first == second
    assert first == third
    assert first_output == second_output
    assert first_output == third_output
    assert first["semantic_execution_hash"] == third["semantic_execution_hash"]
    assert first["semantic_router_config_hash"] == third["semantic_router_config_hash"]
    assert "launch_router_config_hash" not in first
    assert "request_hash" not in first
    assert first["execution_mode"] == "system_replay"
    assert first["routing_session_mode"] == "independent"
    assert first["replay_scope"] == "independent_items"
    assert first["clock_mode"] == "deterministic_logical_ms"
    assert first["no_dispatch"] is True
    assert first["result_blind"] is True
    assert first["item_count"] == first["decision_count"] == 2
    assert first["output_hash"] == worker._sha256_bytes(first_output)
    assert first["model_manifest_hash"] == _RUNTIME_IDENTITY["model_manifest_hash"]
    assert first["artifact_closure_hash"] == _RUNTIME_IDENTITY["artifact_closure_hash"]
    assert set(first["opensquilla_code_files"]) == {
        "opensquilla/__init__.py",
        "opensquilla/engine/__init__.py",
        "opensquilla/engine/routing/__init__.py",
        "opensquilla/engine/routing/benchmark_worker.py",
        "opensquilla/engine/routing/fixed_four_tier_v2.py",
        "opensquilla/engine/routing/registered_model.py",
        "opensquilla/gateway/config.py",
    }
    assert str(tmp_path) not in json.dumps(first, sort_keys=True)

    decisions = _read_jsonl(tmp_path / "first" / worker.DECISIONS_FILENAME)
    assert [value["item_id"] for value in decisions] == ["a", "b"]
    assert [value["final_intent"] for value in decisions] == ["new_task", "new_task"]
    assert [value["final_tier"] for value in decisions] == ["C1", "C2"]
    assert decisions[0]["model_id"] == "deepseek/deepseek-v4-flash"
    assert decisions[1]["model_id"] == "deepseek/deepseek-v4-pro"
    assert all(value["deployment_definition_hash"].startswith("sha256:") for value in decisions)
    assert all(value["episode_id"] is None for value in decisions)
    assert all(value["turn_index"] is None for value in decisions)
    assert all(value["state_version_after"] == 1 for value in decisions)
    assert all(value["policy_hash"].startswith("sha256:") for value in decisions)
    assert all(value["input_snapshot_hash"].startswith("sha256:") for value in decisions)
    assert all(instance.closed for instance in _FakeRegisteredModelClassifier.instances)
    # New-task intent is decided by the production no-active-task rule.  The
    # single local model call per row is therefore the tier inference only.
    assert [instance.calls for instance in _FakeRegisteredModelClassifier.instances] == [
        [("ordinary", True), ("hard problem", True)]
    ] * 3


def test_episode_mode_runs_production_intent_and_continuity_rules(tmp_path: Path) -> None:
    request_path = _artifacts(
        tmp_path,
        routing_session_mode="episode",
        rows=[
            {
                "item_id": "z-first",
                "input": _route_input("initial"),
                "episode_id": "episode-a",
                "turn_index": 0,
            },
            {
                "item_id": "a-continue",
                "input": _route_input("continue this", active_route_tier="C1"),
                "episode_id": "episode-a",
                "turn_index": 1,
            },
            {
                "item_id": "m-redo",
                "input": _route_input("upgrade on redo", active_route_tier="C1"),
                "episode_id": "episode-a",
                "turn_index": 2,
                "control_event": "redo",
            },
        ],
    )

    attestation = worker.run_route_only(request_path, tmp_path / "output")
    decisions = {
        value["item_id"]: value
        for value in _read_jsonl(tmp_path / "output" / worker.DECISIONS_FILENAME)
    }

    assert attestation["replay_scope"] == "synthetic_ordered_fixed_context_episodes"
    assert decisions["z-first"]["final_intent"] == "new_task"
    assert decisions["z-first"]["final_tier"] == "C1"
    assert decisions["a-continue"]["final_intent"] == "continue"
    assert decisions["a-continue"]["final_tier"] == "C1"
    assert decisions["a-continue"]["previous_tier"] == "C1"
    assert decisions["a-continue"]["route_trace"]["tier"]["run_status"] == "ran"
    assert decisions["m-redo"]["final_intent"] == "redo"
    assert decisions["m-redo"]["final_tier"] == "C3"
    assert decisions["m-redo"]["switch_reason"] == "redo_upgrade"
    ordered_items = ("z-first", "a-continue", "m-redo")
    assert [decisions[item]["state_version_after"] for item in ordered_items] == [1, 2, 3]
    assert _FakeRegisteredModelClassifier.instances[0].calls == [
        ("initial", True),
        ("continue this", False),
        ("continue this", True),
        ("upgrade on redo", True),
    ]


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", active_route_tier="C1"),
                }
            ],
            "active_route_tier=null",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"oracle_quality": 1.0}),
                }
            ],
            "result-derived data",
        ),
        (
            [{"item_id": "a", "input": _route_input("x", context={"official_cost_usd": 1})}],
            "result-derived data",
        ),
        (
            [{"item_id": "a", "input": _route_input("x", context={"best_model_id": "m"})}],
            "result-derived data",
        ),
        (
            [{"item_id": "a", "input": _route_input("x", context={"input_tokens_actual": 1})}],
            "result-derived data",
        ),
        (
            [{"item_id": "a", "input": _route_input("x", context={"input_token_count": 1})}],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"cache_read_token_count": 1}),
                }
            ],
            "result-derived data",
        ),
        (
            [{"item_id": "a", "input": _route_input("x", context={"actual_cost": 1})}],
            "result-derived data",
        ),
        (
            [{"item_id": "a", "input": _route_input("x", context={"benchmark_cost": 1})}],
            "result-derived data",
        ),
        (
            [{"item_id": "a", "input": _route_input("x", context={"note": "quality=1"})}],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "quality_score=1"}),
                }
            ],
            "result-derived data",
        ),
        (
            [{"item_id": "a", "input": _route_input("x", context={"note": "score: 0.9"})}],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "actual_cost -> 1"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "input_tokens=12"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "cache write token count: 4"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": '{"best_tier":"C3"}'}),
                }
            ],
            "result-derived data",
        ),
        (
            [{"item_id": "a", "input": _route_input("x", context={"note": "[quality=1]"})}],
            "result-derived data",
        ),
        (
            [{"item_id": "a", "input": _route_input("x", context={"note": "(score:0.9)"})}],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "/actual_cost=1"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "oracle_quality_score=1"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "selected_model_id=C3"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "bestModelId=C3"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "oracleQualityScore=1"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "quality_a_b_c_d_e=1"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "quality/foo=1"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "oracle@value=1"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "payload={official_cost:1}"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "json:{quality:1}"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "official:cost=1"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "ground:truth:model=C3"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "selected:model=C3"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "expected:tier=C3"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "input:tokens=2"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "cache:read:tokens=4"}),
                }
            ],
            "result-derived data",
        ),
        (
            [
                {
                    "item_id": "a",
                    "input": _route_input("x", context={"note": "cache:write:token:count=4"}),
                }
            ],
            "result-derived data",
        ),
    ],
)
def test_independent_mode_rejects_state_and_result_leakage_before_model_load(
    tmp_path: Path,
    rows: list[dict[str, Any]],
    expected: str,
) -> None:
    request_path = _artifacts(
        tmp_path,
        routing_session_mode="independent",
        rows=rows,
    )

    with pytest.raises(worker.BenchmarkRouteOnlyError, match=expected):
        worker.run_route_only(request_path, tmp_path / "output")

    assert _FakeRegisteredModelClassifier.instances == []
    assert list((tmp_path / "output").iterdir()) == []


def test_result_blind_filter_keeps_route_before_usage_and_token_budget(
    tmp_path: Path,
) -> None:
    route_input = _route_input(
        "x",
        context={
            "input_token_budget": 8_000,
            "note": "input_token_budget=4096",
        },
    )
    previous_usage = {
        "input_tokens": 123,
        "output_tokens": 45,
        "reasoning_tokens": 6,
        "cached_tokens": 7,
        "cache_write_tokens": 8,
        "duration_ms": 900,
        "route_id": "prior-route",
        "execution_status": "succeeded",
        "error_code": None,
        "response_id": "prior-response",
        "attempt_ids": ["attempt-1"],
        "retry_count": 0,
    }
    route_input["previous_usage"] = previous_usage
    request_path = _artifacts(
        tmp_path,
        routing_session_mode="independent",
        rows=[{"item_id": "a", "input": route_input}],
    )

    attestation = worker.run_route_only(request_path, tmp_path / "output")

    assert attestation["decision_count"] == 1
    observed = _FakeRegisteredModelClassifier.instances[0].snapshots[0]["router_input"]
    assert observed["previous_usage"] == {
        key: previous_usage[key]
        for key in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "cache_write_tokens",
            "duration_ms",
        )
    }


def test_feature_ready_boundary_accepts_largest_supported_integer(
    tmp_path: Path,
) -> None:
    route_input = _route_input(
        "x",
        context={"context_tokens_est": worker._MAX_FEATURE_INTEGER},  # noqa: SLF001
    )
    route_input["route_history"] = [
        {
            "route_class": "r1",
            "difficulty": worker._MAX_FEATURE_INTEGER,  # noqa: SLF001
            "margin": 0.5,
        }
    ]
    route_input["previous_usage"] = {
        "input_tokens": worker._MAX_FEATURE_INTEGER,  # noqa: SLF001
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
    }
    request_path = _artifacts(
        tmp_path,
        routing_session_mode="independent",
        rows=[{"item_id": "a", "input": route_input}],
    )

    attestation = worker.run_route_only(request_path, tmp_path / "output")

    assert attestation["decision_count"] == 1


@pytest.mark.parametrize(
    ("input_update", "expected"),
    [
        ({"current_request": ""}, "current_request"),
        ({"current_request": " \t "}, "current_request"),
        ({"route_history": [{}]}, "route_history"),
        ({"route_history": [{"tier_id": "C4"}]}, "route_history"),
        (
            {"route_history": [{"tier_id": "C1", "difficulty": 10**1_000}]},
            "feature numeric range",
        ),
        (
            {"context": {"context_tokens_est": 10**1_000}},
            "feature numeric range",
        ),
    ],
)
def test_feature_ready_boundary_rejects_runtime_invalid_rows_before_model_load(
    tmp_path: Path,
    input_update: dict[str, Any],
    expected: str,
) -> None:
    route_input = _route_input("x")
    route_input.update(input_update)
    request_path = _artifacts(
        tmp_path,
        routing_session_mode="independent",
        rows=[{"item_id": "a", "input": route_input}],
    )

    with pytest.raises(worker.BenchmarkRouteOnlyError, match=expected):
        worker.run_route_only(request_path, tmp_path / "output")

    assert _FakeRegisteredModelClassifier.instances == []
    assert list((tmp_path / "output").iterdir()) == []


def test_result_assignment_scanner_is_linear_for_long_unstructured_text() -> None:
    value = "a" * 100_000

    assert worker._contains_result_assignment(value) is False
    assert worker._contains_result_assignment(f"{value}=1") is True


@pytest.mark.parametrize(
    "previous_usage",
    [
        {"official_cost": 999},
        {"oracle_quality": 1.0},
        {"best_tier": "C3"},
        {"unknown": {"nested": "value"}},
        {"input_tokens": 1, "output_tokens": 2},
        {
            "input_tokens": True,
            "output_tokens": 2,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
        },
        {
            "input_tokens": -1,
            "output_tokens": 2,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
        },
        {
            "input_tokens": worker._MAX_FEATURE_INTEGER + 1,  # noqa: SLF001
            "output_tokens": 2,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
        },
        {"route_id": "route-only"},
        {
            "route_id": "route",
            "execution_status": "succeeded",
            "error_code": None,
            "response_id": None,
            "attempt_ids": [""],
            "retry_count": 0,
        },
    ],
)
def test_result_blind_filter_rejects_unbounded_or_invalid_previous_usage_before_model_load(
    tmp_path: Path,
    previous_usage: dict[str, Any],
) -> None:
    route_input = _route_input("x")
    route_input["previous_usage"] = previous_usage
    request_path = _artifacts(
        tmp_path,
        routing_session_mode="independent",
        rows=[{"item_id": "a", "input": route_input}],
    )

    with pytest.raises(worker.BenchmarkRouteOnlyError, match="previous_usage"):
        worker.run_route_only(request_path, tmp_path / "output")

    assert _FakeRegisteredModelClassifier.instances == []


def test_episode_mode_requires_canonical_group_order_and_contiguous_turns(
    tmp_path: Path,
) -> None:
    request_path = _artifacts(
        tmp_path,
        routing_session_mode="episode",
        rows=[
            {
                "item_id": "a",
                "input": _route_input("x"),
                "episode_id": "episode-a",
                "turn_index": 1,
            }
        ],
    )

    with pytest.raises(worker.BenchmarkRouteOnlyError, match="contiguous from zero"):
        worker.run_route_only(request_path, tmp_path / "output")

    assert _FakeRegisteredModelClassifier.instances == []


def test_worker_routes_one_verified_snapshot_when_source_path_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_row = {"item_id": "a", "input": _route_input("alpha")}
    request_path = _artifacts(
        tmp_path,
        routing_session_mode="independent",
        rows=[original_row],
    )
    request = json.loads(request_path.read_bytes())
    input_path = Path(request["input_jsonl"])
    original_payload = input_path.read_bytes()
    replacement_payload = (
        worker.canonical_json_bytes({"item_id": "a", "input": _route_input("omega")}) + b"\n"
    )
    assert len(replacement_payload) == len(original_payload)

    original_builder = worker._build_router

    def replace_after_snapshot(*args: Any, **kwargs: Any) -> Any:
        input_path.write_bytes(replacement_payload)
        return original_builder(*args, **kwargs)

    monkeypatch.setattr(worker, "_build_router", replace_after_snapshot)
    attestation = worker.run_route_only(request_path, tmp_path / "output")

    assert input_path.read_bytes() == replacement_payload
    assert attestation["input_hash"] == worker._sha256_bytes(original_payload)
    assert _FakeRegisteredModelClassifier.instances[0].calls == [("alpha", True)]


def test_input_line_reader_stops_at_the_line_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingStream(io.BytesIO):
        readline_sizes: list[int]

        def __init__(self, value: bytes) -> None:
            super().__init__(value)
            self.readline_sizes = []

        def readline(self, size: int = -1) -> bytes:
            self.readline_sizes.append(size)
            return super().readline(size)

    monkeypatch.setattr(worker, "_MAX_JSONL_LINE_BYTES", 8)
    stream = RecordingStream(b"x" * 100)

    with pytest.raises(worker.BenchmarkRouteOnlyError, match="line size limit"):
        list(worker._iter_input_rows(stream, routing_session_mode="independent"))  # noqa: SLF001

    assert stream.readline_sizes == [10]
    assert stream.tell() == 10


@pytest.mark.parametrize(
    ("limit_name", "expected"),
    [
        ("_MAX_DECISION_LINE_BYTES", "decision exceeds the line size limit"),
        ("_MAX_DECISIONS_JSONL_BYTES", "decisions exceed the output size limit"),
    ],
)
def test_output_size_limit_fails_before_bundle_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    expected: str,
) -> None:
    request_path = _artifacts(
        tmp_path,
        routing_session_mode="independent",
        rows=[{"item_id": "a", "input": _route_input("x")}],
    )
    monkeypatch.setattr(worker, limit_name, 1)

    with pytest.raises(worker.BenchmarkRouteOnlyError, match=expected):
        worker.run_route_only(request_path, tmp_path / "output")

    assert _FakeRegisteredModelClassifier.instances[0].closed is True
    assert list((tmp_path / "output").iterdir()) == []


@pytest.mark.parametrize(
    "failure_point",
    ["second_replace", "directory_open", "directory_fsync"],
)
def test_output_bundle_failure_never_leaves_commit_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    output_dir = tmp_path / failure_point
    output_dir.mkdir()
    original_replace = worker.os.replace
    original_open = worker.os.open
    original_fsync = worker.os.fsync
    replace_calls = 0
    fsync_calls = 0

    def failing_replace(source: str | bytes | Path, target: str | bytes | Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if failure_point == "second_replace" and replace_calls == 2:
            raise OSError("injected second rename failure")
        original_replace(source, target)

    def failing_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if failure_point == "directory_fsync" and fsync_calls == 3:
            raise OSError("injected directory fsync failure")
        original_fsync(descriptor)

    def failing_open(path: str | bytes | Path, flags: int, *args: Any) -> int:
        if failure_point == "directory_open" and Path(path) == output_dir:
            raise OSError("injected directory open failure")
        return original_open(path, flags, *args)

    monkeypatch.setattr(worker.os, "replace", failing_replace)
    monkeypatch.setattr(worker.os, "open", failing_open)
    monkeypatch.setattr(worker.os, "fsync", failing_fsync)

    with pytest.raises(worker.BenchmarkRouteOnlyError, match="could not be committed"):
        worker._write_bundle(
            output_dir,
            decisions_payload=b"{}\n",
            attestation_payload=b"{}",
        )

    assert not (output_dir / worker.ATTESTATION_FILENAME).exists()
    assert not (output_dir / worker.DECISIONS_FILENAME).exists()
    assert list(output_dir.iterdir()) == []


def test_semantic_policy_identity_excludes_registered_model_host_paths(tmp_path: Path) -> None:
    left = _config(tmp_path / "left")
    right = _config(tmp_path / "right")

    left_policy = fixed_four_tier_semantic_policy_config(left.model_dump(mode="json"))
    right_policy = fixed_four_tier_semantic_policy_config(right.model_dump(mode="json"))

    assert left_policy == right_policy
    assert "artifact_root" not in left_policy["classifier"]
    assert "metadata_db" not in left_policy["classifier"]
    assert left_policy["classifier"]["model_set_id"] == "benchmark-router"
    assert left_policy["classifier"]["expected_manifest_hash"] == _hash("manifest")


def test_source_identity_rejects_symlinked_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "opensquilla"
    relative_files = (
        "__init__.py",
        "engine/__init__.py",
        "engine/routing/__init__.py",
        "engine/routing/benchmark_worker.py",
        "engine/routing/fixed_four_tier_v2.py",
        "engine/routing/registered_model.py",
        "gateway/config.py",
    )
    for relative in relative_files:
        path = package_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    target = tmp_path / "replacement.py"
    target.write_text("replacement", encoding="utf-8")
    config_path = package_root / "gateway/config.py"
    config_path.unlink()
    config_path.symlink_to(target)
    monkeypatch.setattr(
        worker,
        "__file__",
        str(package_root / "engine/routing/benchmark_worker.py"),
    )

    with pytest.raises(worker.BenchmarkRouteOnlyError, match="source identity"):
        worker._source_identity()  # noqa: SLF001


def test_primary_module_import_does_not_load_provider_or_runtime() -> None:
    repository = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repository / "src")
    script = """
import sys
import opensquilla.engine.routing.benchmark_worker
assert 'opensquilla.engine.runtime' not in sys.modules
assert not any(
    name == prefix or name.startswith(prefix + '.')
    for name in sys.modules
    for prefix in (
        'opensquilla.engine.agent',
        'opensquilla.engine.turn_runner',
        'opensquilla.provider',
    )
)
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr

    help_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "opensquilla.engine.routing.benchmark_worker",
            "--help",
        ],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "--request" in help_result.stdout
    assert "--output-dir" in help_result.stdout


def test_routing_package_dir_keeps_lazy_public_surface() -> None:
    repository = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repository / "src")
    script = """
import sys
import opensquilla.engine.routing as routing
assert set(routing.__all__).issubset(dir(routing))
assert 'RoutingPolicyEngine' in dir(routing)
assert 'opensquilla.engine.routing.policy' not in sys.modules
assert not any(
    name == prefix or name.startswith(prefix + '.')
    for name in sys.modules
    for prefix in (
        'opensquilla.engine.agent',
        'opensquilla.engine.turn_runner',
        'opensquilla.provider',
    )
)
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("schema_version", ["fixed-four-tier-v2-v3", "fixed-four-tier-v2-v4"])
def test_worker_normalizes_complete_canonical_router_config(
    tmp_path: Path, schema_version: str
) -> None:
    original = _config(tmp_path).model_dump(mode="json")
    original["schema_version"] = schema_version
    path = tmp_path / "config.json"
    payload = worker.canonical_json_bytes(original)
    path.write_bytes(payload)
    config, input_bytes = worker._load_router_config(path)
    assert input_bytes == payload
    assert config.schema_version == "fixed-four-tier-v2-v4"
    assert config.classifier.model_dump(mode="json") == original["classifier"]
    assert path.read_bytes() == payload


@pytest.mark.parametrize("schema_version", ["fixed-four-tier-v2-v3", "fixed-four-tier-v2-v4"])
def test_worker_legacy_config_still_requires_complete_canonical_input(
    tmp_path: Path, schema_version: str
) -> None:
    original = _config(tmp_path).model_dump(mode="json")
    original["schema_version"] = schema_version
    path = tmp_path / "config.json"
    incomplete = json.loads(json.dumps(original))
    incomplete["classifier"].pop("allow_candidate")
    path.write_bytes(worker.canonical_json_bytes(incomplete))
    with pytest.raises(worker.BenchmarkRouteOnlyError, match="complete canonical"):
        worker._load_router_config(path)
    path.write_text(json.dumps(original, indent=2))
    with pytest.raises(worker.BenchmarkRouteOnlyError, match="canonical"):
        worker._load_router_config(path)


def test_worker_v3_and_v4_configs_execute_identical_policy(tmp_path: Path) -> None:
    request_path = _artifacts(
        tmp_path,
        routing_session_mode="independent",
        rows=[{"item_id": "a", "input": _route_input("hard problem")}],
    )
    config_path = Path(json.loads(request_path.read_bytes())["router_config"])
    config = json.loads(config_path.read_bytes())
    config["schema_version"] = "fixed-four-tier-v2-v3"
    config_path.write_bytes(worker.canonical_json_bytes(config))
    legacy = worker.run_route_only(request_path, tmp_path / "legacy")
    legacy_decisions = (tmp_path / "legacy" / worker.DECISIONS_FILENAME).read_bytes()
    config["schema_version"] = "fixed-four-tier-v2-v4"
    config_path.write_bytes(worker.canonical_json_bytes(config))
    current = worker.run_route_only(request_path, tmp_path / "current")
    assert legacy == current
    assert legacy_decisions == (tmp_path / "current" / worker.DECISIONS_FILENAME).read_bytes()
