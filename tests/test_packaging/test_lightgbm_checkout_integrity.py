"""A Windows-style checkout must retain the frozen model's byte identities."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_autocrlf_checkout_preserves_frozen_router_assets(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("Git is required to exercise checkout conversion")
    root = Path(__file__).resolve().parents[2]
    package = root / "src/opensquilla/squilla_router/lightgbm_model"
    paths = [
        path
        for path in package.rglob("*")
        if path.is_file() and path.suffix in {".json", ".py", ".toml", ".md"}
    ]
    assert paths
    repository = tmp_path / "repository"
    repository.mkdir()
    environment = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

    def git(*arguments: str) -> None:
        subprocess.run(
            ["git", *arguments], cwd=repository, env=environment, check=True, capture_output=True
        )

    git("init", "--quiet")
    git("config", "core.autocrlf", "true")
    shutil.copyfile(root / ".gitattributes", repository / ".gitattributes")
    expected = {}
    for path in paths:
        relative = path.relative_to(root)
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = path.read_bytes()
        destination.write_bytes(payload)
        expected[relative] = payload
    # Small binary fixtures exercise the production paths without copying
    # the 60 MB ONNX into this checkout-conversion test.
    for filename in ("model.int8.onnx", "standard_scaler88.joblib"):
        relative = package.relative_to(root) / "assets/intent" / filename
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = b"\x00binary\r\nmodel\n\xff"
        destination.write_bytes(payload)
        expected[relative] = payload
    git("add", ".")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git("checkout-index", "--all", f"--prefix={checkout}{os.sep}")
    changed = [
        str(path)
        for path, original in expected.items()
        if (checkout / path).read_bytes() != original
    ]
    assert changed == [], f"Checkout changed frozen source/model bytes: {changed}"
