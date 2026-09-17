#!/usr/bin/env python3
"""Install current and frozen generators into separate environments from one lock."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    subprocess.run(["uv", "sync", "--locked", "--only-group", "dev"], cwd=ROOT, check=True)
    env = dict(os.environ)
    env["UV_PROJECT_ENVIRONMENT"] = str(ROOT / ".venv-contract-legacy")
    subprocess.run(
        ["uv", "sync", "--locked", "--only-group", "legacy-contract-codegen", "--python", "3.12"],
        cwd=ROOT,
        env=env,
        check=True,
    )


if __name__ == "__main__":
    main()
