"""Adapter for hash-pinned Router model sets produced by routing-training-platform.

The training project owns feature extraction, calibration and native model
loading.  OpenSquilla deliberately treats that package as an optional runtime
dependency instead of copying its feature logic here.  One model-set runner
is shared by the intent and tier facades so both heads observe one identical
route-before input and execute at most once per decision.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from opensquilla.engine.routing.fixed_four_tier_v2 import (
    INTENTS,
    ClassifierPrediction,
    Tier,
)

_INTENT_LABELS = ("new_task", "continue", "redo")
_TIER_LABELS = ("C0", "C1", "C2", "C3")
_SUPPORTED_INPUT_SCHEMAS = frozenset({"lightgbm_380.v1", "bert_text88.v1"})


class RegisteredModelRuntimeError(RuntimeError):
    """A registered routing model could not be loaded or executed safely."""


class RegisteredModelAuthorizationError(RegisteredModelRuntimeError):
    """Registry authorization changed, so routing must fail closed."""

    fail_closed = True


def _load_runtime_dependencies() -> SimpleNamespace:
    """Import the optional training runtime behind one testable boundary."""

    try:
        from router_training.artifacts import LocalArtifactStore  # type: ignore[import-untyped]
        from router_training.contracts import RouterInput  # type: ignore[import-untyped]
        from router_training.model_identity import (  # type: ignore[import-untyped]
            model_manifest_identity_hash,
        )
        from router_training.model_registry import (  # type: ignore[import-untyped]
            SQLiteModelRegistryReader,
        )
        from router_training.serving_runtime import (  # type: ignore[import-untyped]
            LocalModelSetRunner,
        )
    except ImportError as exc:
        raise RegisteredModelRuntimeError(
            "registered_model requires routing-training-platform in the OpenSquilla environment"
        ) from exc
    return SimpleNamespace(
        LocalArtifactStore=LocalArtifactStore,
        LocalModelSetRunner=LocalModelSetRunner,
        RouterInput=RouterInput,
        SQLiteModelRegistryReader=SQLiteModelRegistryReader,
        model_manifest_identity_hash=model_manifest_identity_hash,
    )


def _verified_registry_record(
    dependencies: SimpleNamespace,
    registry: object,
    *,
    model_set_id: str,
    expected_manifest_hash: str,
    allow_candidate: bool,
) -> tuple[object, str]:
    """Re-authorize one explicitly configured model against current metadata."""

    try:
        # Deliberately do not consult the registry's active pointer.  This mode
        # evaluates the exact model_set_id + Manifest Hash frozen in config;
        # the pointer is a production promotion mechanism, not an evaluator.
        record = registry.get(model_set_id)  # type: ignore[attr-defined]
        if record is None:
            raise RegisteredModelAuthorizationError("configured model_set_id is not registered")
        calculated_hash = dependencies.model_manifest_identity_hash(record.manifest)
        if (
            record.manifest_hash != expected_manifest_hash
            or calculated_hash != expected_manifest_hash
            or record.manifest.model_set_id != model_set_id
        ):
            raise RegisteredModelAuthorizationError(
                "configured model manifest hash no longer matches the registered model"
            )
        status = str(getattr(record.status, "value", record.status))
        if status == "CANDIDATE" and allow_candidate:
            pass
        elif status != "VALIDATED":
            raise RegisteredModelAuthorizationError(
                f"registered model status {status!r} is not allowed"
            )
        return record, status
    except RegisteredModelAuthorizationError:
        raise
    except Exception as exc:
        raise RegisteredModelAuthorizationError(
            "registered model metadata could not be re-authorized"
        ) from exc


def _best_effort_close(value: object | None) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _absolute_existing_path(value: str, *, label: str, directory: bool) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise RegisteredModelRuntimeError(f"{label} must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RegisteredModelRuntimeError(f"{label} does not exist") from exc
    valid = resolved.is_dir() if directory else resolved.is_file()
    if not valid:
        expected = "directory" if directory else "file"
        raise RegisteredModelRuntimeError(f"{label} must be an existing {expected}")
    return resolved


def _probabilities(
    value: object,
    *,
    labels: Sequence[str],
    lower_labels: bool,
) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(labels):
        raise RegisteredModelRuntimeError("registered model returned an invalid label space")
    result: dict[str, float] = {}
    for label in labels:
        raw = value[label]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise RegisteredModelRuntimeError("registered model returned a non-numeric probability")
        probability = float(raw)
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise RegisteredModelRuntimeError("registered model returned an invalid probability")
        result[label.lower() if lower_labels else label] = probability
    if not math.isclose(sum(result.values()), 1.0, rel_tol=1e-6, abs_tol=1e-6):
        raise RegisteredModelRuntimeError("registered model probabilities do not sum to one")
    return result


def _winner(probabilities: Mapping[str, float]) -> tuple[str, float]:
    maximum = max(probabilities.values())
    labels = INTENTS if set(probabilities) == set(INTENTS) else tuple(probabilities)
    return max(labels, key=probabilities.__getitem__), maximum


class RegisteredModelClassifier:
    """Intent/tier classifier facades backed by one verified local model set.

    ``predict(snapshot)`` serves the intent facade and
    ``predict(snapshot, allowed_tiers)`` serves the tier facade.  The tier
    argument only selects the output head; policy filtering remains owned by
    the four-tier state machine and the full four-class distribution is kept.
    """

    backend = "registered_model"
    feature_vector_status = "materialized"

    def __init__(
        self,
        *,
        artifact_root: str,
        metadata_db: str,
        model_set_id: str,
        expected_manifest_hash: str,
        allow_candidate: bool = False,
    ) -> None:
        artifact_path = _absolute_existing_path(
            artifact_root,
            label="registered model artifact_root",
            directory=True,
        )
        metadata_path = _absolute_existing_path(
            metadata_db,
            label="registered model metadata_db",
            directory=False,
        )
        dependencies = _load_runtime_dependencies()

        store = dependencies.LocalArtifactStore(artifact_path)
        registry = None
        runner = None
        try:
            registry = dependencies.SQLiteModelRegistryReader(metadata_path)
            record, status = _verified_registry_record(
                dependencies,
                registry,
                model_set_id=model_set_id,
                expected_manifest_hash=expected_manifest_hash,
                allow_candidate=allow_candidate,
            )
            manifest = record.manifest  # type: ignore[attr-defined]

            if manifest.input_schema_version not in _SUPPORTED_INPUT_SCHEMAS:
                raise RegisteredModelRuntimeError(
                    "registered model uses an unsupported route input schema"
                )
            if (
                tuple(manifest.outputs.get("intent", ())) != _INTENT_LABELS
                or tuple(manifest.outputs.get("tier", ())) != _TIER_LABELS
            ):
                raise RegisteredModelRuntimeError(
                    "registered model output contract is incompatible"
                )

            load_online = getattr(
                dependencies.LocalModelSetRunner, "load_for_online_inference", None
            )
            if not callable(load_online):
                raise RegisteredModelRuntimeError(
                    "routing-training-platform lacks the embedded online inference API"
                )
            try:
                runner = load_online(store, manifest, bert_load_fp32=False)
            except Exception as exc:
                raise RegisteredModelRuntimeError(
                    "registered model artifacts could not be loaded"
                ) from exc

            identity_value = getattr(runner, "identity", None)
            identity_dump = getattr(identity_value, "model_dump", None)
            identity = identity_dump(mode="json") if callable(identity_dump) else None
            required_identity = {
                "schema_version",
                "model_set_id",
                "model_manifest_hash",
                "artifact_closure_hash",
                "runner_digest",
                "environment_digest",
                "model_type",
                "execution_mode",
            }
            if (
                not isinstance(identity, Mapping)
                or not required_identity.issubset(identity)
                or identity.get("schema_version") != "local_runner_identity.v2"
                or identity.get("model_set_id") != model_set_id
                or identity.get("model_manifest_hash") != expected_manifest_hash
                or identity.get("model_type") != manifest.model_type
                or identity.get("execution_mode") != "native_embedded"
            ):
                raise RegisteredModelRuntimeError(
                    "registered model runtime identity is unavailable or inconsistent"
                )

            runtime = manifest.runtime
            vector_dim = runtime.get("feature_dimension")
            if vector_dim is None:
                vector_dim = runtime.get("numeric_dimension")
            self.feature_vector_dim = (
                int(vector_dim)
                if isinstance(vector_dim, int)
                and not isinstance(vector_dim, bool)
                and vector_dim > 0
                else None
            )
            self.feature_schema_version = str(manifest.input_schema_version)
            self.version = f"{model_set_id}@{expected_manifest_hash}"
            self._runner_identity = dict(identity)
            self.identity = {
                **self._runner_identity,
                "registry_status": status,
                "input_schema_version": self.feature_schema_version,
            }
        except BaseException:
            _best_effort_close(runner)
            _best_effort_close(registry)
            raise

        self._router_input_model = dependencies.RouterInput
        self._runner = runner
        self._registry = registry
        self._runtime_dependencies = dependencies
        self._model_set_id = model_set_id
        self._expected_manifest_hash = expected_manifest_hash
        self._allow_candidate = allow_candidate
        self._manifest = manifest
        self._lock = threading.RLock()
        self._closed = False
        self._cached_input_hash: str | None = None
        self._cached_prediction: object | None = None

    @staticmethod
    def _canonical_hash(value: Mapping[str, Any]) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _prediction(self, snapshot: Mapping[str, Any]) -> object:
        raw = snapshot.get("router_input")
        if not isinstance(raw, Mapping):
            raise RegisteredModelRuntimeError("route snapshot has no canonical RouterInput")
        try:
            router_input = self._router_input_model.model_validate(raw)
            serialized = router_input.model_dump(mode="json")
        except Exception as exc:
            raise RegisteredModelRuntimeError(
                "route snapshot violates the RouterInput contract"
            ) from exc
        input_hash = self._canonical_hash(serialized)
        with self._lock:
            if self._closed:
                raise RegisteredModelRuntimeError("registered model runner is closed")
            current_record, current_status = _verified_registry_record(
                self._runtime_dependencies,
                self._registry,
                model_set_id=self._model_set_id,
                expected_manifest_hash=self._expected_manifest_hash,
                allow_candidate=self._allow_candidate,
            )
            if current_record.manifest != self._manifest:  # type: ignore[attr-defined]
                raise RegisteredModelAuthorizationError(
                    "registered model manifest changed after the runner was loaded"
                )
            self.identity["registry_status"] = current_status
            if self._cached_input_hash == input_hash and self._cached_prediction is not None:
                return self._cached_prediction
            try:
                prediction = self._runner.predict({"input": serialized})
            except Exception as exc:
                raise RegisteredModelRuntimeError("registered model inference failed") from exc
            prediction_identity_value = getattr(prediction, "identity", None)
            prediction_identity_dump = getattr(prediction_identity_value, "model_dump", None)
            prediction_identity = (
                prediction_identity_dump(mode="json")
                if callable(prediction_identity_dump)
                else None
            )
            if (
                getattr(prediction, "schema_version", None) != "local_routing_prediction.v1"
                or not isinstance(prediction_identity, Mapping)
                or dict(prediction_identity) != self._runner_identity
            ):
                raise RegisteredModelRuntimeError(
                    "registered model returned an inconsistent runtime identity"
                )
            self._cached_input_hash = input_hash
            self._cached_prediction = prediction
            return prediction

    def predict(
        self,
        snapshot: Mapping[str, Any],
        allowed_tiers: Sequence[Tier] | None = None,
    ) -> ClassifierPrediction:
        prediction = self._prediction(snapshot)
        if allowed_tiers is None:
            probabilities = _probabilities(
                getattr(prediction, "intent_probabilities", None),
                labels=_INTENT_LABELS,
                lower_labels=False,
            )
        else:
            probabilities = _probabilities(
                getattr(prediction, "tier_probabilities", None),
                labels=_TIER_LABELS,
                lower_labels=True,
            )
        label, confidence = _winner(probabilities)
        decision_field = "intent_decision" if allowed_tiers is None else "tier_decision"
        expected_decision = label if allowed_tiers is None else label.upper()
        reported_decision = getattr(prediction, decision_field, None)
        reported_label = str(reported_decision or "").lower()
        if (
            reported_decision != expected_decision
            and probabilities.get(reported_label) != confidence
        ):
            raise RegisteredModelRuntimeError(
                "registered model decision does not match its probability argmax"
            )
        return ClassifierPrediction(
            label=label,
            probabilities=probabilities,
            confidence=confidence,
            version=self.version,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cached_prediction = None
            self._cached_input_hash = None
            try:
                self._runner.close()
            finally:
                self._registry.close()
