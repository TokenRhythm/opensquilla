"""Settings policy imports remain independent of process runtime implementations."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_settings_import_does_not_initialize_runtime_packages(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "\n".join(
                (
                    "import json, sys",
                    "sys.dont_write_bytecode = True",
                    "sys.path.insert(0, sys.argv[1])",
                    "import opensquilla.application.app_settings",
                    "print(json.dumps(sorted(sys.modules)))",
                )
            ),
            str(ROOT / "src"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    loaded = json.loads(completed.stdout)
    forbidden = (
        "opensquilla.provider",
        "opensquilla.gateway",
        "opensquilla.engine",
        "opensquilla.tools",
        "httpx",
    )
    assert not [
        name for name in loaded
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in forbidden)
    ]
