#!/usr/bin/env python3
"""Fail release builds whose slim Desktop artifacts exceed fixed byte budgets."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AssetBudget:
    pattern: str
    maximum_bytes: int


ASSET_BUDGETS: dict[str, tuple[AssetBudget, ...]] = {
    "macos": (
        AssetBudget("OpenSquilla-*-mac-arm64.dmg", 360_291_400),
        AssetBudget("OpenSquilla-*-mac-arm64.zip", 364_706_552),
    ),
    "windows": (
        AssetBudget("OpenSquilla-*-win-x64.exe", 423_062_346),
    ),
}


def verify_artifact_sizes(
    root: Path,
    platform: str,
    *,
    budgets: Mapping[str, Sequence[AssetBudget]] = ASSET_BUDGETS,
) -> list[tuple[Path, int, int]]:
    if platform not in budgets:
        raise ValueError(f"unsupported Desktop artifact platform: {platform}")
    if not root.is_dir():
        raise ValueError(f"Desktop artifact directory does not exist: {root}")

    verified: list[tuple[Path, int, int]] = []
    for budget in budgets[platform]:
        matches = sorted(path for path in root.glob(budget.pattern) if path.is_file())
        if len(matches) != 1:
            raise ValueError(
                f"expected exactly one {budget.pattern} below {root}, found {len(matches)}"
            )
        asset = matches[0]
        actual_bytes = asset.stat().st_size
        if actual_bytes > budget.maximum_bytes:
            raise ValueError(
                f"{asset.name} is {actual_bytes} bytes; slim-package budget is "
                f"{budget.maximum_bytes} bytes"
            )
        verified.append((asset, actual_bytes, budget.maximum_bytes))
    return verified


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--platform", choices=sorted(ASSET_BUDGETS), required=True)
    args = parser.parse_args()

    try:
        verified = verify_artifact_sizes(args.root, args.platform)
    except ValueError as exc:
        parser.error(str(exc))

    for asset, actual_bytes, maximum_bytes in verified:
        print(f"{asset.name}: {actual_bytes} <= {maximum_bytes} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
