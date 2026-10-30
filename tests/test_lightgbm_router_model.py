"""Validate the migrated pair against frozen independent source outputs."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from opensquilla.squilla_router.lightgbm_model import (
    MANIFEST_HASH,
    MODEL_SET_ID,
    LightGBMInferenceError,
    LightGBMModelError,
    LightGBMRouterModel,
    default_model_root,
    validate_artifacts,
)


@pytest.fixture(scope="module")
def model():
    pytest.importorskip("lightgbm")
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    pytest.importorskip("sklearn")
    with patch(
        "socket.socket.connect", side_effect=AssertionError("inference must remain offline")
    ):
        runtime = LightGBMRouterModel()
        yield runtime
        runtime.close()


def test_manifest_and_boot_probe_do_not_import_ml():
    identity = validate_artifacts()
    assert identity["model_set_id"] == MODEL_SET_ID
    assert identity["model_manifest_hash"] == MANIFEST_HASH
    assert identity["artifact_count"] == 18
    code = (
        "import sys; from opensquilla.squilla_router.lightgbm_model import validate_artifacts; "
        "validate_artifacts(); "
        "assert not {'numpy','lightgbm','onnxruntime','sklearn','router_training'} "
        "& set(sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_original_classifier_golden_vectors(model):
    import numpy as np

    for target in ("intent", "tier"):
        golden = json.loads((default_model_root() / target / "golden_vectors.json").read_text())
        for case in golden["cases"]:
            prediction = model.predict_features(case["features"])
            values = getattr(prediction, target + "_probabilities")
            np.testing.assert_allclose(
                [values[label] for label in golden["classes"]],
                case["expected"]["calibrated_probabilities"],
                rtol=1e-10,
                atol=1e-12,
            )


def test_runtime_identity_does_not_require_distribution_metadata(model):
    from importlib.metadata import PackageNotFoundError

    with patch("importlib.metadata.version", side_effect=PackageNotFoundError("frozen package")):
        runtime = LightGBMRouterModel()
        try:
            assert runtime.identity["runtime_versions"] == model.identity["runtime_versions"]
        finally:
            runtime.close()


def test_full_features_and_outputs_match_frozen_source(model):
    import numpy as np

    reference = json.loads((default_model_root() / "source_parity_cases.json").read_text())
    for case in reference["cases"]:
        features = model.build_features(case["input"])
        assert features.dtype == np.float32
        assert features.shape == (380,)
        np.testing.assert_allclose(
            features, case["features"], rtol=2e-5, atol=2e-6, err_msg=case["name"]
        )
        prediction = model.predict(case["input"])
        for target in ("intent", "tier"):
            values = getattr(prediction, target + "_probabilities")
            expected = case[target + "_probabilities"]
            np.testing.assert_allclose(
                list(values.values()),
                [expected[label] for label in values],
                rtol=1e-10,
                atol=1e-12,
                err_msg=case["name"],
            )
            assert math.isclose(sum(values.values()), 1.0, abs_tol=1e-12)
        assert prediction.identity["model_manifest_hash"] == MANIFEST_HASH
        # Hashes provide strict per-machine evidence even where CPU kernels vary.
        assert prediction.feature_vector_hash.startswith("sha256:")


def test_empty_semantic_segments_are_zero_and_inputs_are_validated(model):
    import numpy as np

    features = model.build_features({"current_request": "hello"})
    np.testing.assert_array_equal(features[212:276], 0.0)
    np.testing.assert_array_equal(features[292:356], 0.0)
    with pytest.raises(LightGBMInferenceError) as caught:
        model.predict({"current_request": "hello", "surprise": "extra"})
    assert not getattr(caught.value, "fail_closed", False)
    with pytest.raises(LightGBMInferenceError):
        model.predict({"current_request": "  "})


def test_tampered_manifest_fails_closed(tmp_path: Path):
    (tmp_path / "model_manifest.json").write_text("{}")
    with pytest.raises(LightGBMModelError, match="manifest hash mismatch") as caught:
        validate_artifacts(tmp_path)
    assert caught.value.fail_closed


def test_tampered_calibration_is_rejected_before_deserialization(tmp_path: Path):
    root = default_model_root()
    for child in root.iterdir():
        if child.name not in {"intent", "inference_manifest.json"}:
            (tmp_path / child.name).symlink_to(child, target_is_directory=child.is_dir())
    index = json.loads((root / "inference_manifest.json").read_text())
    index["artifacts"].sort(key=lambda item: item["path"] != "intent/calibration.json")
    (tmp_path / "inference_manifest.json").write_text(json.dumps(index))
    (tmp_path / "intent").mkdir()
    for child in (root / "intent").iterdir():
        if child.name == "calibration.json":
            (tmp_path / "intent" / child.name).write_text("{}")
        else:
            (tmp_path / "intent" / child.name).symlink_to(child)
    with patch("opensquilla.squilla_router.lightgbm_model.model._load_transformer") as loader:
        with pytest.raises(
            LightGBMModelError,
            match="artifact hash mismatch: intent/calibration.json",
        ):
            LightGBMRouterModel(tmp_path)
        loader.assert_not_called()


def test_artifact_index_cannot_relocate_verified_files(tmp_path: Path):
    root = default_model_root()
    (tmp_path / "model_manifest.json").write_bytes((root / "model_manifest.json").read_bytes())
    index = json.loads((root / "inference_manifest.json").read_text())
    index["artifacts"][0]["path"] = "elsewhere.json"
    (tmp_path / "inference_manifest.json").write_text(json.dumps(index))
    with pytest.raises(LightGBMModelError, match="artifact layout differs"):
        validate_artifacts(tmp_path)


def test_truncated_compressed_model_fails_closed(tmp_path: Path):
    root = default_model_root()
    (tmp_path / "model_manifest.json").write_bytes((root / "model_manifest.json").read_bytes())
    index = json.loads((root / "inference_manifest.json").read_text())
    index["artifacts"].sort(key=lambda item: item["path"] != "intent/model.txt.gz")
    (tmp_path / "inference_manifest.json").write_text(json.dumps(index))
    (tmp_path / "intent").mkdir()
    (tmp_path / "intent/model.txt.gz").write_bytes(b"\x1f\x8b\x08")
    with pytest.raises(LightGBMModelError) as caught:
        validate_artifacts(tmp_path)
    assert caught.value.fail_closed
