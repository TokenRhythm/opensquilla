from __future__ import annotations

import importlib.util
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


def test_accepts_exactly_one_artifact_within_budget(tmp_path: Path) -> None:
    asset = tmp_path / "OpenSquilla-0.5.4.zip"
    asset.write_bytes(b"12345678")

    assert MODULE.verify_artifact_sizes(tmp_path, "test", budgets=_budgets()) == [
        (asset, 8, 8)
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


def test_release_budgets_are_pinned_to_v053_reduction_targets() -> None:
    assert [item.maximum_bytes for item in MODULE.ASSET_BUDGETS["macos"]] == [
        360_291_400,
        364_706_552,
    ]
    assert [item.maximum_bytes for item in MODULE.ASSET_BUDGETS["windows"]] == [
        423_062_346
    ]
