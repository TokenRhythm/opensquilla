from __future__ import annotations

import numpy as np

from src.router.inference.artifacts import InferenceArtifacts
from src.router.inference.ensemble import fuse_probabilities
from src.router.inference.features import build_feature_bundle
from src.router.inference.heads import run_heads
from src.router.inference.postprocess import apply_postprocess
from src.router.inference.types import InferenceRequest, InferenceResult
from src.router.predictor import ROUTE_CLASSES


class InferenceCore:
    def __init__(
        self,
        *,
        config: dict,
        alpha: np.ndarray,
        temperature: float,
        main_model,
        aux_model,
        mlp_session,
        mlp_scaler,
        v3_extractor,
        bge_extractor,
    ):
        self.config = config
        self.alpha = np.asarray(alpha, dtype=np.float64)
        self.temperature = float(temperature)
        self.main_model = main_model
        self.aux_model = aux_model
        self.mlp_session = mlp_session
        self.mlp_scaler = mlp_scaler
        self.v3_extractor = v3_extractor
        self.bge_extractor = bge_extractor

    @classmethod
    def from_model_dir(
        cls,
        model_dir: str,
        config: dict,
        *,
        use_aux_head: bool,
    ) -> InferenceCore:
        artifacts = InferenceArtifacts.load(model_dir)
        loaded = artifacts.load_runtime_objects(
            config=config,
            use_aux_head=use_aux_head,
        )
        return cls(
            config=config,
            alpha=np.asarray(artifacts.manifest["per_class_alpha"], dtype=np.float64),
            temperature=float(artifacts.manifest["temperature"]),
            main_model=loaded["main_model"],
            aux_model=loaded["aux_model"],
            mlp_session=loaded["mlp_session"],
            mlp_scaler=loaded["mlp_scaler"],
            v3_extractor=loaded["v3_extractor"],
            bge_extractor=loaded["bge_extractor"],
        )

    def predict(self, request: InferenceRequest) -> InferenceResult:
        bundle = self._build_features(request)
        outputs = self._run_heads(bundle)
        fused = self._fuse(outputs)
        decision = self._postprocess(fused, outputs.p_aux_lgbm, request)
        intermediates = {
            "bge_channels_used": bundle.bge_channels_used,
            "asst_signal_present": bundle.asst_signal_present,
        }
        # Self-learning capture (opt-in): surface the feature vectors the model
        # actually consumed so an offline trainer reuses them verbatim (no
        # re-extraction, no train/serve skew). Gated so default runs pay nothing.
        if self.config.get("emit_train_features"):
            intermediates["features_390"] = bundle.features_390
            if self.config.get("emit_raw_bge"):
                intermediates["raw_bge_1536"] = bundle.raw_bge_1536
        return InferenceResult(
            decision=decision,
            probabilities={
                route_class: float(fused[idx])
                for idx, route_class in enumerate(ROUTE_CLASSES)
            },
            aux_decision_probs=self._aux_probs_dict(outputs.p_aux_lgbm),
            intermediates=intermediates,
        )

    def _build_features(self, request: InferenceRequest):
        return build_feature_bundle(
            request=request,
            v3_extractor=self.v3_extractor,
            bge_extractor=self.bge_extractor,
        )

    def _run_heads(self, bundle):
        return run_heads(
            bundle=bundle,
            main_model=self.main_model,
            aux_model=self.aux_model,
            mlp_session=self.mlp_session,
            mlp_scaler=self.mlp_scaler,
            temperature=self.temperature,
        )

    def _fuse(self, outputs) -> np.ndarray:
        return fuse_probabilities(
            outputs.p_main_lgbm,
            outputs.p_mlp_calibrated,
            self.alpha,
        )

    def _postprocess(self, fused_probs: np.ndarray, aux_probs: np.ndarray | None,
                     request: InferenceRequest):
        return apply_postprocess(
            fused_probs=fused_probs,
            aux_probs=self._aux_probs_dict(aux_probs),
            request=request,
            config=self.config,
        )

    @staticmethod
    def _aux_probs_dict(aux_probs: np.ndarray | None) -> dict[str, float] | None:
        if aux_probs is None:
            return None
        aux_probs = np.asarray(aux_probs, dtype=np.float64)
        return {
            "initial": float(aux_probs[0]),
            "maintain": float(aux_probs[1]),
            "upgrade": float(aux_probs[2]),
            "downgrade": float(aux_probs[3]),
        }
