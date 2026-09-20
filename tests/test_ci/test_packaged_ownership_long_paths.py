from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def ownership_probe():
    spec = importlib.util.spec_from_file_location(
        "packaged_ownership_probe",
        ROOT / ".github/scripts/verify-packaged-ownership-long-paths.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("fault", [
    None, "exited-before-ready", "missing-record", "wrong-pid", "forged-identity",
    "nonzero-exit", "retained-record",
])
def test_ownership_probe_requires_identity_and_clean_exit(
    ownership_probe, tmp_path, monkeypatch, request, fault,
):
    """Healthy HTTP alone, a forged owner or forced cleanup must never pass."""
    module = ownership_probe
    root = tmp_path / "owned"
    request.addfinalizer(lambda: shutil.rmtree(module.native_path(root), ignore_errors=True))
    state = {}
    monkeypatch.setenv("OPENSQUILLA_RECOVERY_OFFLINE", "1")
    monkeypatch.setenv("SYNTHETIC_API_TOKEN", "must-not-reach-frozen-child")

    class Child:
        pid = 12345
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            assert timeout > 0
            return self.returncode

        def kill(self):
            self.returncode = -9

    def spawn(_command, *, env, **_kwargs):
        assert _kwargs["cwd"] == root / "empty-cwd"
        assert Path(env["TEMP"]) == root / "temp"
        assert env["TMP"] == env["TEMP"]
        assert "OPENSQUILLA_RECOVERY_OFFLINE" not in env
        assert "SYNTHETIC_API_TOKEN" not in env
        child = Child()
        child.pid += state.get("launches", 0)
        state["launches"] = state.get("launches", 0) + 1
        state["child"] = child
        child.returncode = 0 if fault == "exited-before-ready" else None
        control = Path(env["OPENSQUILLA_DESKTOP_GATEWAY_OWNERSHIP_DIR"])
        os.makedirs(module.native_path(control), exist_ok=True)
        with open(module.native_path(control / module.LOCK), "ab") as stream:
            stream.write(b"\0")
        record = {
            "pid": child.pid + (1 if fault == "wrong-pid" else 0),
            "start_identity": f"synthetic-process-{child.pid}",
            "instance_nonce": env["OPENSQUILLA_DESKTOP_GATEWAY_INSTANCE_NONCE"],
            "profile_fingerprint": control.name,
            "port": int(_command[_command.index("--port") + 1]),
            "version": "0.5.5", "schema_version": 1,
            "protocol": "opensquilla-desktop-gateway-ownership-v1",
        }
        state.update(record=record, record_path=control / module.RECORD)
        if fault != "missing-record":
            with open(module.native_path(state["record_path"]), "w", encoding="utf-8") as stream:
                json.dump(record, stream)
        return child

    def respond(_port, path, payload=None):
        if path == "/api/desktop/identity":
            record = state["record"]
            identity = {key: value for key, value in record.items() if key != "instance_nonce"}
            identity["challenge"] = payload["challenge"]
            signed = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("ascii")
            identity["proof"] = hmac.new(
                record["instance_nonce"].encode("ascii"), signed, hashlib.sha256,
            ).hexdigest()
            if fault == "forged-identity":
                identity["proof"] = "0" * 64
            return 200, json.dumps(identity).encode()
        if path == "/api/desktop/shutdown":
            state["child"].returncode = 7 if fault == "nonzero-exit" else 0
            if fault != "retained-record":
                os.unlink(module.native_path(state["record_path"]))
            return 202, b"{}"
        return 200, b"{}"

    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(module.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(module, "request", respond)
    result = {"ok": False, "boots": []}
    if fault is None:
        module.run_case(tmp_path / "gateway.exe", root, 310, result)
        assert result["ok"] is True
        assert len(result["boots"]) == 2
        assert all(boot["ok"] and boot["exit_code"] == 0 for boot in result["boots"])
    else:
        with pytest.raises((AssertionError, FileNotFoundError)):
            module.run_case(tmp_path / "gateway.exe", root, 310, result)
        assert result["ok"] is False
        assert not any(boot["ok"] for boot in result["boots"])


def test_ownership_probe_boundary_lengths(ownership_probe, tmp_path):
    for length in (245, 310):
        control = ownership_probe.control_directory(tmp_path, "f" * 64, length)
        assert len(str(control / "desktop-gateway.json")) == length
        assert len(str(control / ".desktop-gateway.json.1.0123456789abcdef.tmp")) > 260
        assert (len(str(control / "desktop-gateway.lock")) > 260) == (length == 310)
