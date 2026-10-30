"""Verify packaged router preset data ships in the wheel.

Gateway config validation resolves ``squilla_router.tier_profile`` through the
preset registry's packaged TOML files; if they were dropped from the wheel,
every fresh install would reject the nine legacy profiles at boot.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

LEGACY_PRESET_IDS = (
    "byteplus",
    "dashscope",
    "deepseek",
    "gemini",
    "moonshot",
    "openai",
    "openrouter",
    "volcengine",
    "zhipu",
)
# Curated-inline presets also ship packaged TOML, but never persist as a
# tier_profile id (applied as inline tiers instead).
CURATED_INLINE_PRESET_IDS = ("qwen_token_plan", "tokenrhythm")
PACKAGED_PRESET_IDS = LEGACY_PRESET_IDS + CURATED_INLINE_PRESET_IDS


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_wheel_contains_all_packaged_router_presets(
    isolated_core_wheel: Path,
) -> None:
    """`uv build --wheel` packages opensquilla/provider/presets/<id>.toml."""
    with zipfile.ZipFile(isolated_core_wheel) as wheel:
        names = set(wheel.namelist())

    missing = [
        preset_id
        for preset_id in PACKAGED_PRESET_IDS
        if f"opensquilla/provider/presets/{preset_id}.toml" not in names
    ]
    assert not missing, (
        f"router presets missing from wheel: {missing}; "
        f"found: {sorted(n for n in names if 'provider/presets' in n)}"
    )


def test_source_tree_ships_all_packaged_router_presets() -> None:
    """Cheap guard for the default test path: the preset files exist in-tree."""
    presets_dir = REPO_ROOT / "src" / "opensquilla" / "provider" / "presets"
    present = {p.stem for p in presets_dir.glob("*.toml")}
    assert present == set(PACKAGED_PRESET_IDS), present


def test_wheel_contains_complete_verified_lightgbm_bundle(isolated_core_wheel: Path) -> None:
    import gzip
    import hashlib
    import json

    prefix = "opensquilla/squilla_router/lightgbm_model"
    with zipfile.ZipFile(isolated_core_wheel) as wheel:
        names = set(wheel.namelist())
        assert {
            f"{prefix}/model.py",
            f"{prefix}/input_contract.py",
            f"{prefix}/calibration.py",
            f"{prefix}/_vendor/features/runtime.py",
            f"{prefix}/_vendor/features/numeric.py",
            f"{prefix}/PROVENANCE.md",
        } <= names
        asset_prefix = f"{prefix}/assets"
        manifest = json.loads(wheel.read(f"{asset_prefix}/artifact_manifest.json"))
        from pathlib import PureWindowsPath

        # The real NSIS Gateway prefix previously made hashed encoder paths 289
        # characters and the smoke probe failed to read them. Keep the packaged
        # layout below MAX_PATH without relying on installer or runtime opt-in.
        installed = (
            PureWindowsPath(
                "D:/a/opensquilla/opensquilla/dist/desktop-electron/win-unpacked/resources"
                "/runtime/gateway/opensquilla-gateway/_internal"
            )
            / asset_prefix
        )
        assert {
            "preprocess/feature/artifacts/bge/model.onnx",
            "preprocess/feature/artifacts/bge/tokenizer.json",
        } <= {record["path"] for record in manifest["files"]}
        assert all(len(str(installed / record["path"])) < 260 for record in manifest["files"])
        for record in manifest["files"]:
            path = f"{asset_prefix}/{record['path']}"
            assert path in names, f"missing model artifact: {path}"
            payload = wheel.read(path)
            assert len(payload) == record["size_bytes"], path
            assert hashlib.sha256(payload).hexdigest() == record["sha256"], path
        original = wheel.read(f"{asset_prefix}/model_manifest.json")
        assert hashlib.sha256(original).hexdigest() == (
            "29fb79337458c4f35d3e0d1148fbdb1175c20069a5fa8ee6441633b0e2eee357"
        )
        source = json.loads(original)
        for target in ("intent", "tier"):
            reference = next(
                ref
                for ref in source["models"]
                if (ref["artifact_type"] == "lightgbm_model" and f"/{target}/" in ref["path"])
            )
            decoded = gzip.decompress(wheel.read(f"{asset_prefix}/{target}/model.txt.gz"))
            assert len(decoded) == reference["size"]
            assert "sha256:" + hashlib.sha256(decoded).hexdigest() == reference["sha256"]
