from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_V4_TFIDF_DIMS = 102


class V3FeatureRuntime:
    """Minimal runtime surface for the Phase 3 online feature builder."""

    def __init__(self, tfidf_vectorizer, svd_model):
        self._tfidf = tfidf_vectorizer
        self._svd = svd_model

    def extract_handcrafted(self, text: str) -> np.ndarray:
        from src.router.features import extract_handcrafted

        return extract_handcrafted(text).astype(np.float32)

    def extract_tfidf(self, text: str) -> np.ndarray:
        tfidf_raw = self._tfidf.transform([text])
        tfidf_svd = self._svd.transform(tfidf_raw)[0]
        tfidf = np.zeros(_V4_TFIDF_DIMS, dtype=np.float32)
        width = min(tfidf_svd.shape[0], _V4_TFIDF_DIMS)
        tfidf[:width] = tfidf_svd[:width]
        return tfidf

    def extract_context(self, metadata: dict) -> np.ndarray:
        from src.router.features import ContextMetadata, extract_context_features

        if not metadata:
            return extract_context_features(None)
        allowed = ContextMetadata.__dataclass_fields__.keys()
        context = ContextMetadata(**{k: v for k, v in metadata.items() if k in allowed})
        return extract_context_features(context)

    def extract_hist(self, prev_route_decisions: list) -> np.ndarray:
        from src.router.features import extract_hist_features

        return extract_hist_features(prev_route_decisions or None)


@dataclass
class InferenceArtifacts:
    model_dir: Path
    manifest: dict

    @classmethod
    def load(cls, model_dir: str) -> InferenceArtifacts:
        root = Path(model_dir)
        manifest_path = root / "inference_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        if manifest.get("feature_dim") != 390:
            raise ValueError(
                f"feature_dim mismatch: {manifest.get('feature_dim')}"
            )
        if manifest.get("mlp_input_dim") != 1536:
            raise ValueError(
                f"mlp_input_dim mismatch: {manifest.get('mlp_input_dim')}"
            )
        if "temperature" not in manifest:
            raise ValueError("temperature missing from inference manifest")

        alpha = manifest.get("per_class_alpha")
        if not isinstance(alpha, list) or len(alpha) != 4:
            raise ValueError("per_class_alpha must be a list of length 4")

        return cls(model_dir=root, manifest=manifest)

    def required_paths(self) -> dict[str, Path]:
        return {
            "main_model": self.model_dir / "lgbm_main.bin",
            "aux_model": self.model_dir / "lgbm_aux.bin",
            "tfidf": self.model_dir / "features" / "tfidf.pkl",
            "svd": self.model_dir / "features" / "svd.pkl",
            "config": self.model_dir / "features" / "config.pkl",
            "meta": self.model_dir / "features" / "meta.json",
            "bge_pca": self.model_dir / "features" / "bge_pca.joblib",
            "mlp_onnx": self.model_dir / "mlp" / "model.onnx",
            "mlp_scaler": self.model_dir / "mlp" / "scaler.joblib",
        }

    def _resolve_bge_onnx_dir(self, candidate: object) -> str | None:
        bundled = self.model_dir / "bge_onnx"
        candidate_path: Path | None = None
        if candidate:
            candidate_path = Path(str(candidate))
            if not candidate_path.is_absolute():
                candidate_path = self.model_dir / candidate_path
            if candidate_path.is_dir():
                return str(candidate_path.resolve())

        if bundled.is_dir():
            return str(bundled.resolve())

        if candidate_path is not None:
            return str(candidate_path)
        return None

    def load_runtime_objects(self, config: dict, *, use_aux_head: bool) -> dict[str, object]:
        import joblib
        import lightgbm as lgb
        import onnxruntime as ort

        from src.router.v4_features import BGEChannelExtractor

        paths = self.required_paths()
        main_model = lgb.Booster(model_file=str(paths["main_model"]))
        aux_model = None
        if use_aux_head and paths["aux_model"].exists():
            aux_model = lgb.Booster(model_file=str(paths["aux_model"]))

        tfidf_vectorizer = joblib.load(paths["tfidf"])
        svd_model = joblib.load(paths["svd"])
        v3_extractor = V3FeatureRuntime(tfidf_vectorizer, svd_model)

        bge_extractor = BGEChannelExtractor.load(paths["bge_pca"])
        v4_cfg = (config or {}).get("v4", {})
        bge_backend = (
            v4_cfg.get("bge_backend")
            or self.manifest.get("bge_backend")
            or bge_extractor.backend
        )
        bge_onnx_dir = (
            v4_cfg.get("bge_onnx_dir")
            or self.manifest.get("bge_onnx_dir")
            or bge_extractor.onnx_model_dir
        )
        bge_onnx_dir = self._resolve_bge_onnx_dir(bge_onnx_dir)
        bge_extractor.backend = bge_backend
        bge_extractor.onnx_model_dir = bge_onnx_dir
        bge_extractor._bge = None
        if bge_backend == "onnx" and not bge_onnx_dir:
            raise ValueError("ONNX BGE runtime requires bge_onnx_dir")

        mlp_scaler = joblib.load(paths["mlp_scaler"])
        mlp_session = ort.InferenceSession(
            str(paths["mlp_onnx"]),
            providers=["CPUExecutionProvider"],
        )

        return {
            "main_model": main_model,
            "aux_model": aux_model,
            "mlp_session": mlp_session,
            "mlp_scaler": mlp_scaler,
            "v3_extractor": v3_extractor,
            "bge_extractor": bge_extractor,
        }
