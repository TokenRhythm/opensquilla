from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_session_maintenance_adapters_import_in_isolation() -> None:
    environment = dict(os.environ)
    source_root = str(ROOT / "src")
    inherited_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        os.pathsep.join((source_root, inherited_pythonpath))
        if inherited_pythonpath
        else source_root
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from opensquilla.gateway.adapters.session_reset import "
                "GatewaySessionResetAdapter; "
                "from opensquilla.gateway.adapters.session_maintenance import "
                "GatewaySessionMaintenanceAdapter"
            ),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
