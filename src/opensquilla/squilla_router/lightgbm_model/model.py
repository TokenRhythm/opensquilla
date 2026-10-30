"""Offline loading of the original classifiers and frozen 380-feature runtime.

Only the artifact registry adapter is replaced. Feature extraction and trained
transformers retain the frozen training implementation and original bytes.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODEL_SET_ID = "router-lightgbm-99d6ff75-d444135-a1"
MANIFEST_HASH = "sha256:29fb79337458c4f35d3e0d1148fbdb1175c20069a5fa8ee6441633b0e2eee357"
FEATURE_SCHEMA_VERSION = "lightgbm_380.v1"
INTENT_LABELS = ("new_task", "continue", "redo")
TIER_LABELS = ("C0", "C1", "C2", "C3")
_EXCLUDED_TYPES = frozenset({"run_manifest", "training_metrics"})


class LightGBMModelError(RuntimeError):
    """The configured classifier cannot safely execute."""

    fail_closed = True


class LightGBMInferenceError(RuntimeError):
    """A request failed; the model policy owns the source-compatible fallback."""


def default_model_root() -> Path:
    return Path(__file__).resolve().parent / "assets"


def _hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _safe_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if path == root or root not in path.parents:
        raise LightGBMModelError("bundled model path escapes its asset directory")
    return path


def _verified_artifacts(model_root: str | Path | None) -> tuple[dict[str, Any], dict[str, Path]]:
    root = (
        Path(model_root).expanduser().resolve() if model_root is not None else default_model_root()
    )
    try:
        manifest_bytes = (root / "model_manifest.json").read_bytes()
        if _hash(manifest_bytes) != MANIFEST_HASH:
            raise LightGBMModelError("bundled LightGBM model manifest hash mismatch")
        manifest = json.loads(manifest_bytes)
        if (
            manifest["model_set_id"] != MODEL_SET_ID
            or manifest["input_schema_version"] != FEATURE_SCHEMA_VERSION
            or manifest["outputs"] != {"intent": list(INTENT_LABELS), "tier": list(TIER_LABELS)}
        ):
            raise LightGBMModelError("bundled LightGBM model contract mismatch")
        references = [
            ref
            for ref in manifest["models"] + manifest["preprocess"]
            if ref["artifact_type"] not in _EXCLUDED_TYPES
        ]
        paths = {}
        for ref in references:
            source = Path(ref["path"])
            if "/preprocess/" in ref["path"]:
                local = "preprocess/" + ref["path"].split("/preprocess/", 1)[1].split("/", 1)[1]
            elif "/bundles/" in ref["path"]:
                local = "bundles/" + source.name
            else:
                local = source.parent.name + "/" + source.name
            compressed = ref["artifact_type"] == "lightgbm_model"
            if compressed:
                local += ".gz"
            path = _safe_path(root, local)
            if compressed:
                with gzip.open(path, "rb") as handle:
                    payload = handle.read(ref["size"] + 1)
            else:
                payload = path.read_bytes()
            if len(payload) != ref["size"] or _hash(payload) != ref["sha256"]:
                raise LightGBMModelError(
                    f"bundled LightGBM artifact hash mismatch: {local}"
                )
            paths[ref["path"]] = path
        return manifest, paths
    except LightGBMModelError:
        raise
    except (OSError, EOFError, ValueError, KeyError, TypeError) as exc:
        raise LightGBMModelError("bundled LightGBM model assets are missing or invalid") from exc


def validate_artifacts(model_root: str | Path | None = None) -> dict[str, Any]:
    """Verify the original manifest and every inference asset without ML imports."""
    manifest, paths = _verified_artifacts(model_root)
    return {
        "model_set_id": MODEL_SET_ID,
        "model_manifest_hash": MANIFEST_HASH,
        "input_schema_version": manifest["input_schema_version"],
        "feature_vector_dim": 380,
        "artifact_count": len(paths),
    }


def _load_transformer(path: Path) -> Any:
    """Remap the three frozen wrapper classes without installing router_training.

    The original joblib bytes refer to their training module. Hash verification
    always precedes this call; all sklearn class resolution remains unchanged.
    """
    from joblib.numpy_pickle import NumpyUnpickler

    from ._vendor.features import runtime

    wrappers = {
        name: getattr(runtime, name)
        for name in (
            "SklearnPCA64Projector",
            "SklearnCharTfidfVectorizer",
            "SklearnSVD100Projector",
        )
    }

    class FrozenUnpickler(NumpyUnpickler):
        def find_class(self, module: str, name: str) -> Any:
            if module == "router_training.features.runtime" and name in wrappers:
                return wrappers[name]
            if module.startswith("router_training"):
                raise LightGBMModelError("unexpected training class in frozen transformer")
            return super().find_class(module, name)

    with path.open("rb") as handle:
        return FrozenUnpickler(str(path), handle, ensure_native_byte_order=True).load()


@dataclass(frozen=True, slots=True)
class LightGBMPrediction:
    intent_probabilities: dict[str, float]
    tier_probabilities: dict[str, float]
    intent_decision: str
    tier_decision: str
    feature_vector_hash: str
    identity: dict[str, Any]


class LightGBMRouterModel:
    """One local feature runtime shared by the intent and tier classifiers."""

    source = "lightgbm_model"
    _available = True
    model_set_id = MODEL_SET_ID
    manifest_hash = MANIFEST_HASH
    feature_schema_version = FEATURE_SCHEMA_VERSION
    feature_vector_dim = 380

    def __init__(self, model_root: str | Path | None = None) -> None:
        manifest, paths = _verified_artifacts(model_root)
        self._lock = threading.RLock()
        self._closed = False
        self._feature_runtime = None
        try:
            import joblib
            import lightgbm as lgb
            import numpy as np
            import onnxruntime
            import sklearn
            import tokenizers

            from ._vendor.features.runtime import BGEOnnxEncoder, LightGBMFeatureRuntime

            self._np = np
            references = manifest["models"] + manifest["preprocess"]

            def one(kind: str, target: str = "") -> Path:
                matches = [
                    paths[ref["path"]]
                    for ref in references
                    if ref["artifact_type"] == kind and (not target or f"/{target}/" in ref["path"])
                ]
                if len(matches) != 1:
                    raise LightGBMModelError(f"model has no unique {kind} artifact")
                return matches[0]

            feature_root = one("transformer_manifest").parent
            runtime_manifest = json.loads((feature_root / "runtime_manifest.json").read_text())
            encoder_paths = {
                item["role"]: feature_root / item["path"]
                for item in runtime_manifest["encoder_assets"]
            }
            encoder = BGEOnnxEncoder(
                feature_root,
                model_path=encoder_paths["onnx_model"],
                tokenizer_path=encoder_paths["tokenizer"],
            )
            try:
                self._feature_runtime = LightGBMFeatureRuntime(
                    embedder=encoder,
                    semantic_projector=_load_transformer(feature_root / "pca64.joblib"),
                    text_vectorizer=_load_transformer(feature_root / "tfidf_char_wb_2_4.joblib"),
                    character_projector=_load_transformer(feature_root / "svd100.joblib"),
                )
                self._feature_runtime._featurizer._fitted = True
            except BaseException:
                encoder.close()
                raise
            self._models = {}
            self._temperatures = {}
            for target in ("intent", "tier"):
                model_file = one("lightgbm_model", target)
                with gzip.open(model_file, "rt", encoding="utf-8") as handle:
                    text = handle.read()
                self._models[target] = lgb.Booster(model_str=text)
                calibration = json.loads(one("temperature_calibration", target).read_text())
                self._temperatures[target] = calibration["temperature"]
            self.identity = {
                "model_set_id": MODEL_SET_ID,
                "model_manifest_hash": MANIFEST_HASH,
                "input_schema_version": FEATURE_SCHEMA_VERSION,
                "feature_vector_dim": 380,
                "execution_mode": "native_embedded",
                "model_type": "lightgbm",
                "runtime_versions": {
                    name: module.__version__
                    for name, module in {
                        "numpy": np,
                        "lightgbm": lgb,
                        "onnxruntime": onnxruntime,
                        "tokenizers": tokenizers,
                        "scikit-learn": sklearn,
                        "joblib": joblib,
                    }.items()
                },
            }
        except Exception as exc:
            if self._feature_runtime is not None:
                self._feature_runtime.close()
            raise LightGBMModelError(
                "cannot load bundled LightGBM runtime; install recommended dependencies"
            ) from exc

    def _build_features(self, router_input: Mapping[str, Any]) -> Any:
        from .input_contract import RouterInput

        if self._closed:
            raise LightGBMInferenceError("LightGBM router model is closed")
        canonical = RouterInput.model_validate(router_input).model_dump(mode="json")
        features = self._feature_runtime.transform_row({"input": canonical})
        if features.shape != (380,) or not self._np.isfinite(features).all():
            raise LightGBMInferenceError("LightGBM feature contract was violated")
        return features

    def build_features(self, router_input: Mapping[str, Any]) -> Any:
        """Return the exact float32[380] feature vector for parity inspection."""
        with self._lock:
            try:
                return self._build_features(router_input).copy()
            except (LightGBMModelError, LightGBMInferenceError):
                raise
            except Exception as exc:
                raise LightGBMInferenceError("LightGBM feature extraction failed") from exc

    def predict_features(self, features: Any) -> LightGBMPrediction:
        """Run both calibrated heads on a precomputed vector (golden tests)."""
        from .calibration import apply_temperature, probabilities_to_logits

        with self._lock:
            if self._closed:
                raise LightGBMInferenceError("LightGBM router model is closed")
            matrix = self._np.asarray(features, dtype=self._np.float32).reshape(1, -1)
            if matrix.shape != (1, 380) or not self._np.isfinite(matrix).all():
                raise LightGBMInferenceError("LightGBM requires a finite float32[380] vector")
            outputs = {}
            for target, labels in (("intent", INTENT_LABELS), ("tier", TIER_LABELS)):
                values = self._np.asarray(
                    self._models[target].predict(matrix, num_threads=1), dtype=self._np.float64
                )
                if (
                    values.shape != (1, len(labels))
                    or not self._np.isfinite(values).all()
                    or (values < 0).any()
                ):
                    raise LightGBMInferenceError("LightGBM returned an invalid probability vector")
                mass = values.sum(axis=1, keepdims=True)
                if (mass <= 0).any():
                    raise LightGBMInferenceError("LightGBM returned no probability mass")
                calibrated = apply_temperature(
                    probabilities_to_logits(values / mass),
                    self._temperatures[target],
                )[0]
                outputs[target] = dict(zip(labels, map(float, calibrated), strict=True))
            return LightGBMPrediction(
                intent_probabilities=outputs["intent"],
                tier_probabilities=outputs["tier"],
                intent_decision=max(
                    ("continue", "redo", "new_task"), key=outputs["intent"].__getitem__
                ),
                tier_decision=max(TIER_LABELS, key=outputs["tier"].__getitem__),
                feature_vector_hash=_hash(matrix.astype("<f4", copy=False).tobytes(order="C")),
                identity=dict(self.identity),
            )

    def predict(self, router_input: Mapping[str, Any]) -> LightGBMPrediction:
        with self._lock:
            try:
                return self.predict_features(self._build_features(router_input))
            except (LightGBMModelError, LightGBMInferenceError):
                raise
            except Exception as exc:
                raise LightGBMInferenceError("LightGBM routing inference failed") from exc

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._feature_runtime.close()
                self._closed = True
