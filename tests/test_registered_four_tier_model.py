from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.engine.routing.fixed_four_tier_v2 import (
    FixedFourTierV2Router,
    RoutingRequest,
)
from opensquilla.engine.routing.registered_model import (
    RegisteredModelClassifier,
    RegisteredModelRuntimeError,
)

_MANIFEST_HASH = "sha256:" + "a" * 64


class _RouterInput:
    def __init__(self, value: dict[str, object]) -> None:
        self._value = dict(value)

    @classmethod
    def model_validate(cls, value: object) -> _RouterInput:
        if not isinstance(value, dict) or not str(value.get("current_request") or ""):
            raise ValueError("invalid RouterInput")
        return cls(value)

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return dict(self._value)


class _Registry:
    record: object | None = None
    instances: list[_Registry] = []
    error: Exception | None = None

    def __init__(self, path: Path) -> None:
        self.path = path
        self.close_calls = 0
        self.instances.append(self)

    def __enter__(self) -> _Registry:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get(self, model_set_id: str) -> object | None:
        assert model_set_id == "router-test-a1"
        if self.error is not None:
            raise self.error
        return self.record

    def close(self) -> None:
        self.close_calls += 1


class _Runner:
    loads: list[tuple[object, object, bool]] = []

    def __init__(self) -> None:
        self.calls: list[object] = []
        self.close_calls = 0
        self.close_error: Exception | None = None
        self.prediction_schema_version = "local_routing_prediction.v1"
        self.intent_decision = "continue"
        self.tier_decision = "C2"
        self.intent_probabilities = {"new_task": 0.05, "continue": 0.9, "redo": 0.05}
        self.tier_probabilities = {"C0": 0.1, "C1": 0.2, "C2": 0.6, "C3": 0.1}
        self.identity = SimpleNamespace(
            model_dump=lambda **_: {
                "schema_version": "local_runner_identity.v2",
                "model_set_id": "router-test-a1",
                "model_manifest_hash": _MANIFEST_HASH,
                "artifact_closure_hash": "sha256:" + "b" * 64,
                "runner_digest": "sha256:" + "c" * 64,
                "environment_digest": "sha256:" + "d" * 64,
                "model_type": "lightgbm",
                "execution_mode": "native_embedded",
            }
        )

    @classmethod
    def load_for_online_inference(
        cls,
        store: object,
        manifest: object,
        *,
        bert_load_fp32: bool,
    ) -> _Runner:
        cls.loads.append((store, manifest, bert_load_fp32))
        return cls()

    def predict(self, row: object) -> object:
        self.calls.append(row)
        return SimpleNamespace(
            schema_version=self.prediction_schema_version,
            identity=self.identity,
            intent_probabilities=self.intent_probabilities,
            tier_probabilities=self.tier_probabilities,
            intent_decision=self.intent_decision,
            tier_decision=self.tier_decision,
        )

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def _dependencies(monkeypatch: pytest.MonkeyPatch, *, status: str = "VALIDATED") -> object:
    manifest = SimpleNamespace(
        model_set_id="router-test-a1",
        input_schema_version="lightgbm_380.v1",
        outputs={
            "intent": ["new_task", "continue", "redo"],
            "tier": ["C0", "C1", "C2", "C3"],
        },
        model_type="lightgbm",
        runtime={"feature_dimension": 380},
    )
    _Registry.record = SimpleNamespace(
        manifest=manifest,
        manifest_hash=_MANIFEST_HASH,
        status=SimpleNamespace(value=status),
    )
    _Registry.instances.clear()
    _Registry.error = None
    _Runner.loads.clear()
    dependencies = SimpleNamespace(
        LocalArtifactStore=lambda root: SimpleNamespace(root=root),
        LocalModelSetRunner=_Runner,
        RouterInput=_RouterInput,
        SQLiteModelRegistryReader=_Registry,
        model_manifest_identity_hash=lambda value: (
            _MANIFEST_HASH if value is manifest else "sha256:" + "0" * 64
        ),
    )
    monkeypatch.setattr(
        "opensquilla.engine.routing.registered_model._load_runtime_dependencies",
        lambda: dependencies,
    )
    return manifest


def _paths(tmp_path: Path) -> tuple[str, str]:
    root = tmp_path / "artifacts"
    root.mkdir()
    metadata = tmp_path / "metadata.sqlite3"
    metadata.write_bytes(b"")
    return str(root), str(metadata)


def _load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **overrides: object,
) -> RegisteredModelClassifier:
    _dependencies(monkeypatch, status=str(overrides.pop("status", "VALIDATED")))
    artifact_root, metadata_db = _paths(tmp_path)
    return RegisteredModelClassifier(
        artifact_root=artifact_root,
        metadata_db=metadata_db,
        model_set_id="router-test-a1",
        expected_manifest_hash=str(overrides.pop("expected_manifest_hash", _MANIFEST_HASH)),
        allow_candidate=bool(overrides.pop("allow_candidate", False)),
    )


def test_registered_model_reuses_one_joint_prediction_for_both_facades(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    classifier = _load(monkeypatch, tmp_path)
    snapshot = {"router_input": {"current_request": "route me", "history_user": []}}

    intent = classifier.predict(snapshot)
    tier = classifier.predict(snapshot, ("c0", "c1", "c2", "c3"))

    assert intent.label == "continue"
    assert intent.confidence == pytest.approx(0.9)
    assert tier.label == "c2"
    assert tier.probabilities == {"c0": 0.1, "c1": 0.2, "c2": 0.6, "c3": 0.1}
    assert len(classifier._runner.calls) == 1
    assert _Runner.loads[0][2] is False
    assert classifier.feature_schema_version == "lightgbm_380.v1"
    assert classifier.feature_vector_dim == 380
    assert classifier.identity["execution_mode"] == "native_embedded"

    classifier.close()
    classifier.close()
    assert classifier._runner.close_calls == 1
    assert _Registry.instances[0].close_calls == 1


def test_registered_model_candidate_requires_explicit_diagnostic_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(RegisteredModelRuntimeError, match="status"):
        _load(monkeypatch, tmp_path, status="CANDIDATE")

    other = tmp_path / "allowed"
    other.mkdir()
    classifier = _load(
        monkeypatch,
        other,
        status="CANDIDATE",
        allow_candidate=True,
    )
    assert classifier.identity["registry_status"] == "CANDIDATE"
    classifier.close()


def test_registered_model_rejects_manifest_hash_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(RegisteredModelRuntimeError, match="manifest hash"):
        _load(
            monkeypatch,
            tmp_path,
            expected_manifest_hash="sha256:" + "c" * 64,
        )


def test_registered_model_rejects_missing_or_invalid_router_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    classifier = _load(monkeypatch, tmp_path)
    with pytest.raises(RegisteredModelRuntimeError, match="canonical RouterInput"):
        classifier.predict({})
    with pytest.raises(RegisteredModelRuntimeError, match="RouterInput contract"):
        classifier.predict({"router_input": {"current_request": ""}})
    classifier.close()


def test_registered_model_reauthorizes_before_returning_cached_prediction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    classifier = _load(monkeypatch, tmp_path)
    snapshot = {"router_input": {"current_request": "route me", "history_user": []}}
    classifier.predict(snapshot)
    assert len(classifier._runner.calls) == 1

    assert _Registry.record is not None
    _Registry.record.status = SimpleNamespace(value="DEPRECATED")
    with pytest.raises(RegisteredModelRuntimeError, match="status") as exc_info:
        classifier.predict(snapshot)

    assert getattr(exc_info.value, "fail_closed", False) is True
    assert len(classifier._runner.calls) == 1
    classifier.close()


def test_registered_model_registry_read_failure_is_fail_closed_on_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    classifier = _load(monkeypatch, tmp_path)
    snapshot = {"router_input": {"current_request": "route me", "history_user": []}}
    classifier.predict(snapshot)
    _Registry.error = OSError("registry unavailable")

    with pytest.raises(RegisteredModelRuntimeError, match="re-authorized") as exc_info:
        classifier.predict(snapshot)

    assert getattr(exc_info.value, "fail_closed", False) is True
    assert len(classifier._runner.calls) == 1
    classifier.close()


@pytest.mark.parametrize("corruption", ["schema", "identity"])
def test_registered_model_rejects_prediction_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    corruption: str,
) -> None:
    classifier = _load(monkeypatch, tmp_path)
    if corruption == "schema":
        classifier._runner.prediction_schema_version = "local_routing_prediction.v0"
    else:
        classifier._runner.identity = SimpleNamespace(
            model_dump=lambda **_: {
                **classifier._runner_identity,
                "model_set_id": "router-other-a1",
            }
        )

    with pytest.raises(RegisteredModelRuntimeError, match="runtime identity"):
        classifier.predict({"router_input": {"current_request": "route me"}})
    classifier.close()


@pytest.mark.parametrize(
    ("head", "decision"),
    [("intent", "new_task"), ("tier", "C3")],
)
def test_registered_model_rejects_decision_that_disagrees_with_argmax(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    head: str,
    decision: str,
) -> None:
    classifier = _load(monkeypatch, tmp_path)
    setattr(classifier._runner, f"{head}_decision", decision)
    snapshot = {"router_input": {"current_request": "route me"}}

    with pytest.raises(RegisteredModelRuntimeError, match="probability argmax"):
        classifier.predict(snapshot, None if head == "intent" else ("c0", "c1", "c2", "c3"))
    classifier.close()


def test_registered_model_refreshes_candidate_status_on_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    classifier = _load(
        monkeypatch,
        tmp_path,
        status="CANDIDATE",
        allow_candidate=True,
    )
    snapshot = {"router_input": {"current_request": "route me", "history_user": []}}
    classifier.predict(snapshot)
    assert classifier.identity["registry_status"] == "CANDIDATE"

    assert _Registry.record is not None
    _Registry.record.status = SimpleNamespace(value="VALIDATED")
    classifier.predict(snapshot)

    assert classifier.identity["registry_status"] == "VALIDATED"
    assert len(classifier._runner.calls) == 1
    classifier.close()


def test_registered_model_refreshes_status_in_decision_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    classifier = _load(
        monkeypatch,
        tmp_path,
        status="CANDIDATE",
        allow_candidate=True,
    )
    router = FixedFourTierV2Router(
        intent_classifier=classifier,
        tier_classifier=classifier,
    )
    request = RoutingRequest(
        session_id="session-a",
        request_id="request-a",
        message="route me",
    )
    classifier.predict(router._snapshot(request, None, include_control_event=False))
    assert len(classifier._runner.calls) == 1
    assert _Registry.record is not None
    _Registry.record.status = SimpleNamespace(value="VALIDATED")

    decision = router.route(request)

    assert decision.classifier_identity is not None
    assert decision.classifier_identity["registry_status"] == "VALIDATED"
    assert len(classifier._runner.calls) == 1
    router.close()


def test_registered_model_close_closes_reader_when_runner_close_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    classifier = _load(monkeypatch, tmp_path)
    classifier._runner.close_error = RuntimeError("runner cleanup failed")

    with pytest.raises(RuntimeError, match="runner cleanup failed"):
        classifier.close()

    assert classifier._closed is True
    assert classifier._cached_input_hash is None
    assert classifier._cached_prediction is None
    assert classifier._runner.close_calls == 1
    assert _Registry.instances[0].close_calls == 1
    classifier.close()
    assert classifier._runner.close_calls == 1
    assert _Registry.instances[0].close_calls == 1


@pytest.mark.parametrize(
    ("head", "probabilities", "decision", "expected"),
    [
        (
            "intent",
            {"new_task": 0.34, "continue": 0.33, "redo": 0.33},
            "new_task",
            "new_task",
        ),
        (
            "tier",
            {"C0": 0.25, "C1": 0.25, "C2": 0.26, "C3": 0.24},
            "C2",
            "c2",
        ),
        (
            "intent",
            {"redo": 1 / 3, "new_task": 1 / 3, "continue": 1 / 3},
            "continue",
            "continue",
        ),
        (
            "intent",
            {"redo": 1 / 3, "new_task": 1 / 3, "continue": 1 / 3},
            "new_task",
            "continue",
        ),
        (
            "tier",
            {"C3": 0.25, "C2": 0.25, "C1": 0.25, "C0": 0.25},
            "C0",
            "c0",
        ),
    ],
)
def test_registered_model_accepts_low_probability_and_fixed_order_tied_argmax(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    head: str,
    probabilities: dict[str, float],
    decision: str,
    expected: str,
) -> None:
    classifier = _load(monkeypatch, tmp_path)
    setattr(classifier._runner, f"{head}_probabilities", probabilities)
    setattr(classifier._runner, f"{head}_decision", decision)
    snapshot = {"router_input": {"current_request": "route me"}}

    result = classifier.predict(snapshot, None if head == "intent" else ("c0", "c1", "c2", "c3"))
    assert result.label == expected
    assert result.confidence == pytest.approx(max(probabilities.values()))
    classifier.close()
