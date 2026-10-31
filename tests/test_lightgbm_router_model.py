"""Validate the migrated pair against frozen independent source outputs."""

from __future__ import annotations

import json
import math
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
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


def _copy_standalone_bundle(root: Path) -> None:
    shutil.copytree(default_model_root(), root, dirs_exist_ok=True)
    encoder = root / "preprocess/feature/artifacts/bge"
    encoder.mkdir(parents=True, exist_ok=True)
    shared = default_model_root().parents[1] / "models/v4.2_phase3_inference/bge_onnx"
    for filename in ("model.onnx", "tokenizer.json"):
        shutil.copyfile(shared / filename, encoder / filename)


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
            expected = dict(
                zip(golden["classes"], case["expected"]["calibrated_probabilities"], strict=True)
            )
            labels = ("continue", "redo", "new_task") if target == "intent" else tuple(values)
            assert getattr(prediction, target + "_decision") == max(
                labels, key=expected.__getitem__
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

    from opensquilla.engine.routing.model_policy import ModelPolicyState, decide_model_tier

    # Inspect the actual native session; VNNI hosts otherwise hide U8S8 overflow.
    options = model._encoder.session.get_session_options()
    assert options.get_session_config_entry("session.x64quantprecision") == "1"
    arm64 = platform.machine().lower() in {"arm64", "aarch64"}
    if arm64:
        assert options.get_session_config_entry(
            "optimization.disable_specified_optimizers"
        ) == "MatMulIntegerToFloatFusion;GeluFusionL2"
    # Bounds apply only to measured CPU/input pairs; all labels and policy state
    # still require exact agreement. Evidence lives in runtime_compatibility.json.
    quantization_bounds = {
        "long_three_segments": {
            "feature": 0.0062 if arm64 else 0.0035,
            "intent": 0.003,
            "tier": 0.007,
        },
    }
    if arm64:
        quantization_bounds["code_debug"] = {
            "feature": 0.0065, "intent": 0.00065, "tier": 0.0018,
        }

    reference = json.loads(
        (default_model_root() / "source_parity_cases.json").read_text(encoding="utf-8")
    )
    for case in reference["cases"]:
        features = model.build_features(case["input"])
        assert features.dtype == np.float32
        assert features.shape == (380,)
        expected_features = np.asarray(case["features"], dtype=np.float32)
        bounds = quantization_bounds.get(case["name"])
        semantic = np.r_[0:64, 212:276, 292:356]
        other = np.r_[64:212, 276:292, 356:380]
        np.testing.assert_allclose(
            features[semantic],
            expected_features[semantic],
            rtol=0 if bounds else 2e-5,
            atol=bounds["feature"] if bounds else 2e-6,
            err_msg=case["name"],
        )
        np.testing.assert_allclose(
            features[other],
            expected_features[other],
            rtol=2e-5,
            atol=2e-6,
            err_msg=case["name"],
        )
        prediction = model.predict(case["input"])
        for target in ("intent", "tier"):
            values = getattr(prediction, target + "_probabilities")
            expected = case[target + "_probabilities"]
            np.testing.assert_allclose(
                list(values.values()),
                [expected[label] for label in values],
                rtol=0 if bounds else 1e-10,
                atol=bounds[target] if bounds else 1e-12,
                err_msg=case["name"],
            )
            labels = ("continue", "redo", "new_task") if target == "intent" else tuple(values)
            assert getattr(prediction, target + "_decision") == max(
                labels, key=expected.__getitem__
            ), case["name"]
            assert math.isclose(sum(values.values()), 1.0, abs_tol=1e-12)
        # Numeric tolerance must never excuse a different route or control head.
        prior = case["input"].get("active_route_tier")
        state = ModelPolicyState(prior.lower(), task_id="reference-task") if prior else None
        reference_prediction = SimpleNamespace(
            intent_probabilities=case["intent_probabilities"],
            tier_probabilities=case["tier_probabilities"],
        )
        decisions = [
            decide_model_tier(
                case["input"],
                state,
                SimpleNamespace(predict=lambda _, result=result: result),
                task_id_factory=lambda: "reference-new-task",
            )
            for result in (prediction, reference_prediction)
        ]
        for field in (
            "intent",
            "tier",
            "raw_tier",
            "thinking_mode",
            "prompt_policy",
            "context_reset",
        ):
            assert getattr(decisions[0], field) == getattr(decisions[1], field), case["name"]
        assert decisions[0].next_state == decisions[1].next_state, case["name"]
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


@pytest.mark.parametrize(
    ("relative", "payload", "message"),
    [
        ("intent/calibration.json", b"{}", "artifact hash mismatch: intent/calibration.json"),
        ("preprocess/feature/artifacts/pca64.joblib", None, "assets are missing or invalid"),
        ("intent/model.txt.gz", b"\x1f\x8b\x08", "assets are missing or invalid"),
        ("preprocess/feature/artifacts/bge/tokenizer.json", None, "assets are missing or invalid"),
    ],
)
def test_invalid_bundle_is_rejected_before_deserialization(tmp_path, relative, payload, message):
    _copy_standalone_bundle(tmp_path)
    path = tmp_path / relative
    if payload is None:
        path.unlink()
    else:
        path.write_bytes(payload)
    with patch("opensquilla.squilla_router.lightgbm_model.model._load_transformer") as loader:
        with pytest.raises(LightGBMModelError, match=message) as caught:
            LightGBMRouterModel(tmp_path)
        assert caught.value.fail_closed
        loader.assert_not_called()


@pytest.mark.parametrize("filename", ["model.onnx", "tokenizer.json"])
@pytest.mark.parametrize("missing", [False, True], ids=["tampered", "missing"])
def test_invalid_shared_encoder_is_rejected_before_deserialization(
    tmp_path, monkeypatch, filename, missing,
):
    from opensquilla.squilla_router.lightgbm_model import model as module

    root = tmp_path / "lightgbm_model/assets"
    _copy_standalone_bundle(root)
    shared = tmp_path / "models/v4.2_phase3_inference/bge_onnx"
    shared.parent.mkdir(parents=True)
    (root / "preprocess/feature/artifacts/bge").rename(shared)
    path = shared / filename
    if missing:
        path.unlink()
    else:
        path.write_bytes(b"invalid")
    monkeypatch.setattr(module, "default_model_root", lambda: root)
    message = (
        "assets are missing or invalid" if missing
        else f"artifact hash mismatch: bge_onnx/{filename}"
    )
    with patch.object(module, "_load_transformer") as loader:
        with pytest.raises(LightGBMModelError, match=message):
            LightGBMRouterModel()
        loader.assert_not_called()


def test_native_model_loads_from_deep_install_path_with_non_utf8_locale(tmp_path, monkeypatch):
    # Match the installed Gateway's long prefix while keeping resource names short.
    root = tmp_path / ("installed-gateway-" + "x" * max(1, 145 - len(str(tmp_path)))) / "assets"
    _copy_standalone_bundle(root)
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


def test_encoder_cache_is_bounded_lru_and_releases_owned_rows(monkeypatch):
    import numpy as np

    from opensquilla.squilla_router.lightgbm_model._vendor.features import runtime

    monkeypatch.setattr(runtime, "_BGE_MEMORY_CACHE_MAX_ENTRIES", 2)
    calls = []

    class Tokenizer:
        def token_to_id(self, token):
            return {"[PAD]": 0, "[CLS]": 1, "[SEP]": 2}[token]

        def encode(self, text, *, add_special_tokens):
            return SimpleNamespace(ids=[int(text) + 10])

    class Session:
        def get_inputs(self):
            return [SimpleNamespace(name="input_ids", shape=[1, 512])]

        def run(self, _, feed):
            value = int(feed["input_ids"][0, 1])
            calls.append(value)
            return [np.asarray([[value, 1, 2, 3]], dtype=np.float32)]

    encoder = runtime.BGEOnnxEncoder(
        default_model_root().parents[1] / "models/v4.2_phase3_inference/bge_onnx",
        tokenizer=Tokenizer(),
        session=Session(),
    )

    def encode(*texts):
        return encoder.encode(texts, segment_kinds=["current_request"] * len(texts))

    first = encode("1", "2", "1")
    assert calls == [11, 12]  # Duplicate rows in one batch share an inference.
    np.testing.assert_array_equal(first[0], first[2])
    encode("1")  # Refresh the first entry before inserting a third.
    encode("3")
    encode("1")
    assert calls == [11, 12, 13]
    np.testing.assert_array_equal(encode("2")[0], first[1])
    assert calls == [11, 12, 13, 12]  # The least recently used row was recomputed.
    assert encode("4", "5", "6").shape == (3, 4)
    assert len(encoder._memory_cache) == 2
    assert all(value.flags.owndata for value in encoder._memory_cache.values())
    encoder.close()
    assert not encoder._memory_cache
    with pytest.raises(RuntimeError, match="closed"):
        encode("1")
