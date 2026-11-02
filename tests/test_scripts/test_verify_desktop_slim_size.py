from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(".github/scripts/verify_desktop_slim_size.py")
SPEC = importlib.util.spec_from_file_location("verify_desktop_slim_size", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _budgets(maximum_bytes: int = 8):
    return {"test": (MODULE.AssetBudget("OpenSquilla-*.zip", maximum_bytes),)}


@pytest.mark.parametrize("size", [7, 8])
def test_accepts_exactly_one_artifact_within_or_at_budget(
    tmp_path: Path, size: int
) -> None:
    asset = tmp_path / "OpenSquilla-0.5.4.zip"
    asset.write_bytes(b"x" * size)

    assert MODULE.verify_artifact_sizes(tmp_path, "test", budgets=_budgets()) == [
        (asset, size, 8)
    ]


@pytest.mark.parametrize("count", [0, 2])
def test_requires_exactly_one_matching_artifact(tmp_path: Path, count: int) -> None:
    for index in range(count):
        (tmp_path / f"OpenSquilla-{index}.zip").write_bytes(b"ok")

    with pytest.raises(ValueError, match="expected exactly one"):
        MODULE.verify_artifact_sizes(tmp_path, "test", budgets=_budgets())


def test_rejects_an_artifact_over_budget(tmp_path: Path) -> None:
    (tmp_path / "OpenSquilla-0.5.4.zip").write_bytes(b"123456789")

    with pytest.raises(ValueError, match="slim-package budget"):
        MODULE.verify_artifact_sizes(tmp_path, "test", budgets=_budgets())


def test_release_budgets_include_fixed_font_and_router_asset_allowances() -> None:
    manifest_path = Path(
        "src/opensquilla/squilla_router/lightgbm_model/assets/artifact_manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    resource_bytes = sum(item["size_bytes"] for item in manifest["files"])
    allowance = resource_bytes + manifest_path.stat().st_size
    assert allowance == MODULE.LIGHTGBM_ASSET_ALLOWANCE == 24_212_758
    assert [item.maximum_bytes for item in MODULE.ASSET_BUDGETS["macos"]] == [
        400_886_040 + allowance,
        405_301_192 + allowance,
    ]
    assert [item.maximum_bytes for item in MODULE.ASSET_BUDGETS["windows"]] == [
        423_062_346 + allowance
    ]
