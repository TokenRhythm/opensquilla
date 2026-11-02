from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[2] / "desktop" / "electron" / "scripts"
_PROBE = _SCRIPTS / "probe-router.py"


@pytest.fixture
def router_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from opensquilla.squilla_router import lightgbm_model

    package = tmp_path / "lightgbm"
    package.mkdir()
    version_path = package / "VERSION.txt"
    version_path.write_text("1.2.3\n", encoding="utf-8")
    module = ModuleType("lightgbm")
    module.__file__ = str(package / "__init__.py")
    module.__version__ = "1.2.3"
    monkeypatch.setitem(sys.modules, "lightgbm", module)
    identity = {
        "model_set_id": lightgbm_model.MODEL_SET_ID,
        "model_manifest_hash": lightgbm_model.MANIFEST_HASH,
        "input_schema_version": "lightgbm_380.v1",
        "feature_vector_dim": 380,
        "execution_mode": "native_embedded",
        "model_type": "lightgbm",
        "runtime_versions": dict.fromkeys(
            ("numpy", "lightgbm", "onnxruntime", "tokenizers", "scikit-learn", "joblib"), "1.2.3",
        ),
    }
    prediction = SimpleNamespace(
        identity=identity,
        intent_probabilities={"new_task": 0.5, "continue": 0.25, "redo": 0.25},
        intent_decision="new_task",
        tier_probabilities={"C0": 0.1, "C1": 0.4, "C2": 0.3, "C3": 0.2},
        tier_decision="C1",
    )
    closed = []
    model = SimpleNamespace(identity=identity, predict=lambda _: prediction,
                            close=lambda: closed.append(True))
    monkeypatch.setattr(lightgbm_model, "LightGBMRouterModel", lambda: model)
    probe = runpy.run_path(str(_PROBE))["_probe"]
    return SimpleNamespace(run=probe, model=model, prediction=prediction, closed=closed,
                           module=module, version_path=version_path)


def test_router_probe_checks_runtime_and_closes_model(router_probe) -> None:
    result = router_probe.run()

    assert result == {
        "probe": "opensquilla-desktop-router", "frozen": False,
        "modelType": "lightgbm", "executionMode": "native_embedded", "featureVectorDim": 380,
        "runtimeVersions": [
            "joblib", "lightgbm", "numpy", "onnxruntime", "scikit-learn", "tokenizers",
        ],
        "lightgbmVersionResource": True,
        "intentLabels": ["continue", "new_task", "redo"], "tierLabels": ["C0", "C1", "C2", "C3"],
    }
    assert router_probe.closed == [True]


def test_router_probe_requires_version_resource_despite_available_identity(router_probe) -> None:
    router_probe.version_path.unlink()

    with pytest.raises(FileNotFoundError):
        router_probe.run()


def test_router_probe_rejects_missing_module_version(router_probe) -> None:
    del router_probe.module.__version__

    with pytest.raises(RuntimeError, match="version resource"):
        router_probe.run()


@pytest.mark.parametrize("failure", ["identity", "version", "nan", "mass", "labels", "decision"])
def test_router_probe_rejects_invalid_runtime_or_predictions_and_closes_model(
    router_probe, failure: str,
) -> None:
    if failure == "identity":
        router_probe.model.identity["execution_mode"] = "unavailable"
    elif failure == "version":
        router_probe.model.identity["runtime_versions"]["lightgbm"] = "unknown"
    elif failure == "nan":
        router_probe.prediction.intent_probabilities["continue"] = float("nan")
    elif failure == "mass":
        router_probe.prediction.tier_probabilities["C0"] = 0.5
    elif failure == "labels":
        del router_probe.prediction.intent_probabilities["redo"]
    else:
        router_probe.prediction.tier_decision = "C0"

    with pytest.raises(RuntimeError, match="packaged router"):
        router_probe.run()
    assert router_probe.closed == [True]


def test_packaged_smoke_requires_frozen_router_inference_before_startup() -> None:
    smoke = (_SCRIPTS / "smoke-gateway.mjs").read_text(encoding="utf-8")
    router = smoke[smoke.index("async function verifyGatewayRouter"):
                   smoke.index("async function verifyGatewayCodeExecution")]
    assert "readFile(join(scriptDir, 'probe-router.py'), 'utf8')" in router
    assert "functionalProbe(gatewayBinary, isolatedProbeEnv(env)" in router
    assert "'--internal-child', 'python-code', code" in router
    assert "probe: 'opensquilla-desktop-router', frozen: true" in router
    assert "lightgbmVersionResource: true" in router
    main = smoke[smoke.index("async function main"):]
    assert main.index("await verifyGatewayRouter(gatewayBinary, env)") < main.index(
        "child = spawn(gatewayBinary",
    )


@pytest.mark.parametrize(
    ("platform", "library", "destination"),
    [
        ("darwin", "lib/lib_lightgbm.dylib", "lightgbm/lib"),
        ("linux", "lib/lib_lightgbm.so", "lightgbm/lib"),
        ("win32", "bin/lib_lightgbm.dll", "lightgbm/bin"),
    ],
)
def test_frozen_router_verifier_collects_production_native_resources(
    tmp_path: Path, platform: str, library: str, destination: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = runpy.run_path(str(_SCRIPTS / "verify-router-frozen.py"))
    build = verifier["build_command"]
    monkeypatch.setitem(build.__globals__, "sys", SimpleNamespace(
        executable=sys.executable, platform=platform,
    ))
    monkeypatch.setitem(build.__globals__, "package_file", lambda package, path: (
        tmp_path / "site-packages" / package / path
    ))
    root = _SCRIPTS.parents[2]
    command = build(root, tmp_path / "output")
    paired_flags = list(zip(command, command[1:]))
    assert ("--paths", str(root / "src")) in paired_flags
    assert ("--collect-binaries", "sklearn") in paired_flags
    for module in verifier["ROUTER_HIDDEN_IMPORTS"]:
        assert ("--hidden-import", module) in paired_flags
    for distribution in ("lightgbm", "scikit-learn"):
        assert ("--copy-metadata", distribution) in paired_flags
    for source, target in (
        (tmp_path / "site-packages/lightgbm/VERSION.txt", "lightgbm"),
        (root / "src/opensquilla/squilla_router/lightgbm_model/assets",
         "opensquilla/squilla_router/lightgbm_model/assets"),
        (root / "src/opensquilla/squilla_router/models/v4.2_phase3_inference/bge_onnx",
         "opensquilla/squilla_router/models/v4.2_phase3_inference/bge_onnx"),
    ):
        assert ("--add-data", f"{source}{os.pathsep}{target}") in paired_flags
    assert ("--add-binary", (
        f"{tmp_path / 'site-packages/lightgbm' / library}{os.pathsep}{destination}"
    )) in paired_flags
    mac_openmp = f"{tmp_path / 'site-packages/sklearn/.dylibs/libomp.dylib'}{os.pathsep}."
    assert (("--add-binary", mac_openmp) in paired_flags) is (platform == "darwin")
    assert command[-1] == str(_PROBE)


@pytest.mark.parametrize("missing_rejected", [True, False])
def test_frozen_router_verifier_restores_version_resource_after_negative_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_rejected: bool,
) -> None:
    verifier = runpy.run_path(str(_SCRIPTS / "verify-router-frozen.py"))
    verify = verifier["verify_frozen_probe"]
    binary = tmp_path / "dist/opensquilla-router-probe/opensquilla-router-probe"
    resource = binary.parent / "_internal/lightgbm/VERSION.txt"
    resource.parent.mkdir(parents=True)
    resource.write_bytes(b"1.2.3\n")
    calls = []

    def run(command, **kwargs):
        assert command == [str(binary)]
        assert kwargs["cwd"] == tmp_path
        calls.append(resource.exists())
        if len(calls) == 1:
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "probe": "opensquilla-desktop-router", "frozen": True,
                "lightgbmVersionResource": True,
            }), stderr="")
        return SimpleNamespace(
            returncode=1 if missing_rejected else 0, stdout="",
            stderr="FileNotFoundError: VERSION.txt",
        )

    monkeypatch.setattr(verify.__globals__["subprocess"], "run", run)
    if missing_rejected:
        verify(binary, tmp_path, {})
    else:
        with pytest.raises(RuntimeError, match="did not reject"):
            verify(binary, tmp_path, {})
    assert calls == [True, False]
    assert resource.read_bytes() == b"1.2.3\n"


def test_frozen_router_verifier_rejects_source_python_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = runpy.run_path(str(_SCRIPTS / "verify-router-frozen.py"))
    verify = verifier["verify_frozen_probe"]
    monkeypatch.setattr(verify.__globals__["subprocess"], "run", lambda *_args, **_kwargs: (
        SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "probe": "opensquilla-desktop-router", "frozen": False,
            "lightgbmVersionResource": True,
        }))
    ))

    with pytest.raises(RuntimeError, match="frozen production runtime"):
        verify(tmp_path / "binary", tmp_path, {})


@pytest.mark.parametrize("stage", ["probe", "missing-version"])
def test_frozen_router_verifier_preserves_timeout_logs_and_version_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    verifier = runpy.run_path(str(_SCRIPTS / "verify-router-frozen.py"))
    verify = verifier["verify_frozen_probe"]
    binary = tmp_path / "dist/opensquilla-router-probe/opensquilla-router-probe"
    resource = binary.parent / "_internal/lightgbm/VERSION.txt"
    resource.parent.mkdir(parents=True)
    resource.write_bytes(b"1.2.3\n")
    calls = []

    def run(command, **kwargs):
        assert kwargs["timeout"] == 60
        calls.append(resource.exists())
        if stage == "missing-version" and len(calls) == 1:
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
                "probe": "opensquilla-desktop-router", "frozen": True,
                "lightgbmVersionResource": True,
            }))
        raise subprocess.TimeoutExpired(
            command, 60, output="partial inference 输出".encode(), stderr=b"native timeout\xff",
        )

    monkeypatch.setattr(verify.__globals__["subprocess"], "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        verify(binary, tmp_path, {})

    assert calls == ([True] if stage == "probe" else [True, False])
    assert resource.read_bytes() == b"1.2.3\n"
    assert (tmp_path / f"{stage}.stdout.log").read_text(encoding="utf-8") == (
        "partial inference 输出"
    )
    assert (tmp_path / f"{stage}.stderr.log").read_text(encoding="utf-8") == "native timeout\ufffd"


def test_native_frozen_cells_verify_router_on_all_supported_platforms() -> None:
    root = _SCRIPTS.parents[2]
    workflow = yaml.safe_load(
        (root / ".github/workflows/desktop-native-probes.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["native"]
    cells = [cell["os"] for cell in job["strategy"]["matrix"]["include"]
             if cell["probe"] == "frozen"]
    assert cells == ["ubuntu-latest", "macos-latest", "windows-latest"]
    step, = [step for step in job["steps"]
             if "verify-router-frozen.py" in step.get("run", "")]
    assert step["if"] == "${{ matrix.probe == 'frozen' }}"
    assert '--output-dir "${CI_REPORT_DIR}/router-frozen"' in step["run"]
    assert "set -euo pipefail" in step["run"]
