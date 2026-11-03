"""Offline BERT intent and LightGBM tier inference with frozen preprocessing.

The tier feature pipeline and trained transformers retain their frozen bytes
and arithmetic. Intent uses the selected quantized BERT training contract.
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

MODEL_SET_ID = "router-bert60-int8-intent-lightgbm-tier-v1"
MANIFEST_HASH = "sha256:fd12bd784933733f6cc8bf82b414d88d07a6b0bb1a4cb79b9de251c794664fb4"
_LEGACY_MANIFEST_HASH = "sha256:29fb79337458c4f35d3e0d1148fbdb1175c20069a5fa8ee6441633b0e2eee357"
FEATURE_SCHEMA_VERSION = "lightgbm_380.v1"
INTENT_LABELS = ("new_task", "continue", "redo")
TIER_LABELS = ("C0", "C1", "C2", "C3")
_EXCLUDED_TYPES = frozenset({"run_manifest", "training_metrics"})
# The packaged classifiers share the byte-identical legacy BGE assets. External
# bundles keep their self-contained short paths; both use the frozen digests.
_ENCODER_ASSETS = {"onnx_model": "model.onnx", "tokenizer": "tokenizer.json"}


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
        manifest_hash = _hash(manifest_bytes)
        if manifest_hash != MANIFEST_HASH:
            if root != default_model_root() and manifest_hash == _LEGACY_MANIFEST_HASH:
                raise LightGBMModelError(
                    "squilla_router.lightgbm_model_dir contains the retired LightGBM intent "
                    "bundle. Remove squilla_router.lightgbm_model_dir to use the packaged "
                    "BERT intent model, or replace the entire directory with the current "
                    "verified BERT intent + LightGBM tier bundle."
                )
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
            artifact_root = root
            if local.startswith("preprocess/feature/artifacts/bge/"):
                filename = _ENCODER_ASSETS[source.parent.parent.name]
                local = f"preprocess/feature/artifacts/bge/{filename}"
                if root == default_model_root():
                    artifact_root = root.parents[1] / "models/v4.2_phase3_inference"
                    local = f"bge_onnx/{filename}"
            compressed = ref["artifact_type"] == "lightgbm_model"
            if compressed:
                local += ".gz"
            path = _safe_path(artifact_root, local)
            if compressed:
                with gzip.open(path, "rb") as handle:
                    payload = handle.read(ref["size"] + 1)
            else:
                payload = path.read_bytes()
            if len(payload) != ref["size"] or _hash(payload) != ref["sha256"]:
                raise LightGBMModelError(f"bundled LightGBM artifact hash mismatch: {local}")
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


def _runtime_version(name: str, module: Any) -> str:
    """Keep optional version diagnostics from blocking a loaded frozen runtime."""
    module_version = getattr(module, "__version__", None)
    if isinstance(module_version, str) and module_version:
        return module_version

    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


@dataclass(frozen=True, slots=True)
class LightGBMPrediction:
    intent_probabilities: dict[str, float]
    tier_probabilities: dict[str, float]
    intent_decision: str
    tier_decision: str
    feature_vector_hash: str
    identity: dict[str, Any]


class LightGBMRouterModel:
    """Single-model policy: BERT intent plus the unchanged LightGBM tier head."""

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
        self._encoder = None
        self._intent_session = None
        try:
            import joblib
            import lightgbm as lgb
            import numpy as np
            import onnxruntime
            import sklearn
            import tokenizers

            from ._vendor.features.lightgbm380 import LightGBM380Featurizer
            from ._vendor.features.runtime import BGEOnnxEncoder

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
            encoder_model = one("feature_artifact", "bge/onnx_model")
            encoder = BGEOnnxEncoder(
                encoder_model.parent,
                model_path=encoder_model,
                tokenizer_path=one("feature_artifact", "bge/tokenizer"),
            )
            try:
                self._featurizer = LightGBM380Featurizer(
                    embedder=encoder,
                    semantic_projector=_load_transformer(feature_root / "pca64.joblib"),
                    text_vectorizer=_load_transformer(feature_root / "tfidf_char_wb_2_4.joblib"),
                    character_projector=_load_transformer(feature_root / "svd100.joblib"),
                )
                self._featurizer._fitted = True
            except BaseException:
                encoder.close()
                raise
            self._encoder = encoder
            self._models = {}
            self._temperatures = {}
            with gzip.open(one("lightgbm_model", "tier"), "rt", encoding="utf-8") as handle:
                self._models["tier"] = lgb.Booster(model_str=handle.read())
            self._intent_scaler = _load_transformer(one("numeric_scaler", "intent"))
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
            options.add_session_config_entry("session.x64quantprecision", "1")
            self._intent_session = onnxruntime.InferenceSession(
                str(one("onnx_model", "intent")), sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            for target in ("intent", "tier"):
                calibration = json.loads(
                    one("temperature_calibration", target).read_text(encoding="utf-8")
                )
                self._temperatures[target] = calibration["temperature"]
            self.identity = {
                "model_set_id": MODEL_SET_ID,
                "model_manifest_hash": MANIFEST_HASH,
                "input_schema_version": FEATURE_SCHEMA_VERSION,
                "feature_vector_dim": 380,
                "execution_mode": "native_embedded",
                "model_type": "bert_intent_lightgbm_tier",
                "runtime_versions": {
                    name: _runtime_version(name, module)
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
            if self._encoder is not None:
                self._encoder.close()
            raise LightGBMModelError(
                "cannot load bundled LightGBM runtime; install recommended dependencies"
            ) from exc

    def _build_features(self, canonical: Mapping[str, Any]) -> Any:
        if self._closed:
            raise LightGBMInferenceError("LightGBM router model is closed")
        features = self._featurizer.transform_row({"input": canonical})
        if features.shape != (380,) or not self._np.isfinite(features).all():
            raise LightGBMInferenceError("LightGBM feature contract was violated")
        return features

    def build_features(self, router_input: Mapping[str, Any]) -> Any:
        """Return the exact float32[380] feature vector for parity inspection."""
        from .input_contract import RouterInput

        with self._lock:
            try:
                canonical = RouterInput.model_validate(router_input).model_dump(mode="json")
                return self._build_features(canonical).copy()
            except (LightGBMModelError, LightGBMInferenceError):
                raise
            except Exception as exc:
                raise LightGBMInferenceError("LightGBM feature extraction failed") from exc

    def _intent_features(
        self, canonical: Mapping[str, Any], tier_features: Any,
    ) -> dict[str, Any]:
        """Reuse tier numeric88 and reproduce the frozen BERT text contract."""
        from ._vendor.features.runtime import _tokenizer_content_ids
        from ._vendor.features.text import (
            SEGMENT_KINDS,
            extract_text_segments,
            truncate_content_tokens,
        )

        np = self._np
        encoder = self._encoder
        assert encoder is not None
        texts = extract_text_segments(canonical).as_tuple()
        ids = np.full((1, 3, 128), encoder.pad_token_id, dtype=np.int64)
        mask = np.zeros_like(ids)
        for index, (kind, text, length) in enumerate(
            zip(SEGMENT_KINDS, texts, (128, 80, 48), strict=True)
        ):
            content = truncate_content_tokens(
                kind, _tokenizer_content_ids(encoder.tokenizer, text or "[EMPTY]"),
                max_content_tokens=length - 2,
            )
            sequence = [encoder.cls_token_id, *content, encoder.sep_token_id]
            ids[0, index, :len(sequence)] = sequence
            mask[0, index, :len(sequence)] = 1
        # The frozen 380-vector embeds rules48, route-history16, then
        # previous-signals19 + context5 in the original numeric88 order.
        numeric = np.concatenate((
            tier_features[164:212], tier_features[276:292], tier_features[356:380],
        )).reshape(1, 88)
        numeric = np.asarray(self._intent_scaler.transform(numeric), dtype=np.float32)
        numeric = np.clip(numeric, -5.0, 5.0).astype(np.float32, copy=False)
        if numeric.shape != (1, 88) or not np.isfinite(numeric).all():
            raise LightGBMInferenceError("BERT numeric feature contract was violated")
        return {"input_ids": ids, "attention_mask": mask, "numeric88": numeric}

    def predict_tier_features(self, features: Any) -> dict[str, float]:
        """Run the unchanged calibrated tier head on a frozen 380-vector."""
        from .calibration import apply_temperature, probabilities_to_logits

        with self._lock:
            if self._closed:
                raise LightGBMInferenceError("LightGBM router model is closed")
            matrix = self._np.asarray(features, dtype=self._np.float32).reshape(1, -1)
            if matrix.shape != (1, 380) or not self._np.isfinite(matrix).all():
                raise LightGBMInferenceError("LightGBM requires a finite float32[380] vector")
            values = self._np.asarray(
                self._models["tier"].predict(matrix, num_threads=1), dtype=self._np.float64,
            )
            if (values.shape != (1, 4) or not self._np.isfinite(values).all()
                    or (values < 0).any() or values.sum() <= 0):
                raise LightGBMInferenceError("LightGBM returned an invalid probability vector")
            calibrated = apply_temperature(
                probabilities_to_logits(values / values.sum(axis=1, keepdims=True)),
                self._temperatures["tier"],
            )[0]
            return dict(zip(TIER_LABELS, map(float, calibrated), strict=True))

    def predict_tier(self, router_input: Mapping[str, Any]) -> dict[str, Any]:
        """Classify a known new task without executing the intent model."""
        from .input_contract import RouterInput

        with self._lock:
            try:
                canonical = RouterInput.model_validate(router_input).model_dump(mode="json")
                features = self._build_features(canonical)
                tier = self.predict_tier_features(features)
                return {
                    "tier_probabilities": tier,
                    "tier_decision": max(TIER_LABELS, key=tier.__getitem__),
                    "feature_vector_hash": _hash(
                        features.astype("<f4", copy=False).tobytes(order="C"),
                    ),
                    "identity": dict(self.identity),
                }
            except (LightGBMModelError, LightGBMInferenceError):
                raise
            except Exception as exc:
                raise LightGBMInferenceError("Model-policy tier inference failed") from exc

    def predict(self, router_input: Mapping[str, Any]) -> LightGBMPrediction:
        from .calibration import apply_temperature
        from .input_contract import RouterInput

        with self._lock:
            try:
                canonical = RouterInput.model_validate(router_input).model_dump(mode="json")
                features = self._build_features(canonical)
                assert self._intent_session is not None
                logits = self._np.asarray(
                    self._intent_session.run(
                        ["logits"], self._intent_features(canonical, features),
                    )[0],
                    dtype=self._np.float64,
                )
                if logits.shape != (1, 3) or not self._np.isfinite(logits).all():
                    raise LightGBMInferenceError("BERT returned invalid intent logits")
                intent = dict(zip(
                    INTENT_LABELS,
                    map(float, apply_temperature(logits, self._temperatures["intent"])[0]),
                    strict=True,
                ))
                tier = self.predict_tier_features(features)
                return LightGBMPrediction(
                    intent_probabilities=intent, tier_probabilities=tier,
                    intent_decision=max(("continue", "redo", "new_task"), key=intent.__getitem__),
                    tier_decision=max(TIER_LABELS, key=tier.__getitem__),
                    feature_vector_hash=_hash(
                        features.astype("<f4", copy=False).tobytes(order="C"),
                    ),
                    identity=dict(self.identity),
                )
            except (LightGBMModelError, LightGBMInferenceError):
                raise
            except Exception as exc:
                raise LightGBMInferenceError("Model-policy routing inference failed") from exc

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                assert self._encoder is not None
                self._encoder.close()
                self._intent_session = None
                self._closed = True
