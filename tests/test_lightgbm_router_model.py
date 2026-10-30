"""Validate the migrated pair against frozen independent source outputs."""

from __future__ import annotations

import json
import math
import shutil
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
        golden = json.loads(
            (default_model_root() / target / "golden_vectors.json").read_text(encoding="utf-8")
        )
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

    reference = json.loads(
        (default_model_root() / "source_parity_cases.json").read_text(encoding="utf-8")
    )
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
    shutil.copytree(default_model_root(), tmp_path, dirs_exist_ok=True)
    (tmp_path / "intent/calibration.json").write_text("{}")
    with patch("opensquilla.squilla_router.lightgbm_model.model._load_transformer") as loader:
        with pytest.raises(
            LightGBMModelError,
            match="artifact hash mismatch: intent/calibration.json",
        ):
            LightGBMRouterModel(tmp_path)
        loader.assert_not_called()


def test_missing_preprocess_asset_is_rejected_before_deserialization(tmp_path: Path):
    shutil.copytree(default_model_root(), tmp_path, dirs_exist_ok=True)
    (tmp_path / "preprocess/feature/artifacts/pca64.joblib").unlink()
    with patch("opensquilla.squilla_router.lightgbm_model.model._load_transformer") as loader:
        with pytest.raises(LightGBMModelError, match="assets are missing or invalid"):
            LightGBMRouterModel(tmp_path)
        loader.assert_not_called()


def test_truncated_compressed_model_fails_closed(tmp_path: Path):
    shutil.copytree(default_model_root(), tmp_path, dirs_exist_ok=True)
    (tmp_path / "intent/model.txt.gz").write_bytes(b"\x1f\x8b\x08")
    with pytest.raises(LightGBMModelError) as caught:
        validate_artifacts(tmp_path)
    assert caught.value.fail_closed


def test_native_model_loads_from_deep_install_path_with_non_utf8_locale(tmp_path, monkeypatch):
    # Match the installed Gateway's long prefix while keeping resource names short.
    root = tmp_path / ("installed-gateway-" + "x" * max(1, 145 - len(str(tmp_path)))) / "assets"
    shutil.copytree(default_model_root(), root)
    original_read_text = Path.read_text
    reads = []

    def legacy_read_text(path, encoding=None, errors=None):
        if path.is_relative_to(root):
            reads.append((path.name, encoding))
        return original_read_text(path, encoding=encoding or "cp1252", errors=errors)

    monkeypatch.setattr(Path, "read_text", legacy_read_text)
    runtime = LightGBMRouterModel(root)
    try:
        prediction = runtime.predict({"current_request": "你好，请解释一下什么是二分查找。"})
        assert prediction.tier_decision == "C0"
        assert reads and all(encoding == "utf-8" for _, encoding in reads)
        assert len(str(root / "preprocess/feature/artifacts/bge/model.onnx")) > 200
    finally:
        runtime.close()
