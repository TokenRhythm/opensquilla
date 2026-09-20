"""Shared references retain identity without importing implementation packages."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from opensquilla.resource_references import session_reference_v1


def test_session_reference_v1_has_stable_identity_scope_and_capabilities() -> None:
    assert session_reference_v1(
        "webchat:default",
        title="  Default   chat ",
        run_status="running",
    ) == {
        "version": 1,
        "kind": "session",
        "id": "agent:main:webchat:default",
        "label": "Default chat",
        "scope": {"sessionKey": "agent:main:webchat:default"},
        "state": {"available": True, "runStatus": "running"},
        "capabilities": {"open": True, "copy": True},
    }


def test_reference_contract_imports_without_loading_runtime_implementations() -> None:
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "src")
    completed = subprocess.run(
        [sys.executable, "-c", """
import sys
from opensquilla.resource_references import session_reference_v1
assert session_reference_v1("webchat:default")["id"] == "agent:main:webchat:default"
for package in ("application", "gateway", "session", "tools"):
    prefix = "opensquilla." + package
    assert not any(name == prefix or name.startswith(prefix + ".") for name in sys.modules)
"""],
        cwd=root, env=environment, capture_output=True, text=True, check=False, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
