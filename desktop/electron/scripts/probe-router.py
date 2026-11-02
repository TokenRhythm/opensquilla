"""Offline packaged router inference gate, never run by application startup."""

from __future__ import annotations

import contextlib
import json
import math
import sys
from pathlib import Path

_INTENT_LABELS = {"new_task", "continue", "redo"}
_TIER_LABELS = {"C0", "C1", "C2", "C3"}
_RUNTIME_NAMES = {"numpy", "lightgbm", "onnxruntime", "tokenizers", "scikit-learn", "joblib"}


def _probe() -> dict[str, object]:
    import lightgbm

    from opensquilla.squilla_router.lightgbm_model import (
        MANIFEST_HASH,
        MODEL_SET_ID,
        LightGBMRouterModel,
    )

    # Runtime identity has a metadata fallback, but a release must still carry
    # the version resource used by LightGBM's own package initialization.
    version = Path(lightgbm.__file__).with_name("VERSION.txt").read_text(encoding="utf-8").strip()
    if not version or getattr(lightgbm, "__version__", None) != version:
        raise RuntimeError("packaged LightGBM version resource is missing or inconsistent")

    model = LightGBMRouterModel()
    try:
        identity = model.identity
        expected = {
            "model_set_id": MODEL_SET_ID,
            "model_manifest_hash": MANIFEST_HASH,
            "input_schema_version": "lightgbm_380.v1",
            "feature_vector_dim": 380,
            "execution_mode": "native_embedded",
            "model_type": "lightgbm",
        }
        if any(identity.get(key) != value for key, value in expected.items()):
            raise RuntimeError("packaged router runtime identity is inconsistent")
        versions = identity.get("runtime_versions", {})
        if (
            set(versions) != _RUNTIME_NAMES
            or any(
                not isinstance(value, str) or not value.strip() or value == "unknown"
                for value in versions.values()
            )
            or versions["lightgbm"] != version
        ):
            raise RuntimeError("packaged router dependency versions are missing or inconsistent")

        prediction = model.predict({"current_request": "Explain binary search."})
        if prediction.identity != identity:
            raise RuntimeError("packaged router prediction changed runtime identity")
        for target, labels in (("intent", _INTENT_LABELS), ("tier", _TIER_LABELS)):
            probabilities = getattr(prediction, f"{target}_probabilities")
            decision = getattr(prediction, f"{target}_decision")
            if (
                set(probabilities) != labels
                or not all(math.isfinite(value) and value >= 0 for value in probabilities.values())
                or not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-8)
                or decision not in labels
                or probabilities[decision] != max(probabilities.values())
            ):
                raise RuntimeError(f"packaged router returned invalid {target} predictions")
        return {
            "probe": "opensquilla-desktop-router",
            "frozen": bool(getattr(sys, "frozen", False)),
            "modelType": identity["model_type"],
            "executionMode": identity["execution_mode"],
            "featureVectorDim": identity["feature_vector_dim"],
            "runtimeVersions": sorted(versions),
            "lightgbmVersionResource": True,
            "intentLabels": sorted(_INTENT_LABELS),
            "tierLabels": sorted(_TIER_LABELS),
        }
    finally:
        model.close()


def main() -> None:
    with contextlib.redirect_stdout(sys.stderr):
        result = _probe()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
