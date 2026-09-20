"""Boot the actual frozen Windows Gateway with long Desktop ownership paths.

Only new synthetic profiles below --workdir are used. The Windows long-path
policy is recorded, never changed. Forced process termination is failure cleanup.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import ntpath
import os
import platform
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

RECORD = "desktop-gateway.json"
LOCK = "desktop-gateway.lock"
TIMEOUT_SECONDS = 120


def native_path(path: Path) -> str:
    """Use extended I/O spelling without changing profile identity in evidence."""
    if os.name != "nt":
        return str(path)
    value = ntpath.abspath(str(path))
    if value.startswith("\\\\?\\"):
        return value
    return "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value


def profile_fingerprint(profile: Path) -> str:
    # Same logical identity as recovery.locking.profile_lock_key and the
    # production TS ownership reader. Do not import source runtime dependencies
    # into an acceptance probe for a self-contained frozen executable.
    normalized = os.path.normcase(os.path.normpath(str(profile.resolve())))
    return hashlib.sha256(normalized.encode("utf-8", "surrogatepass")).hexdigest()


def long_paths_enabled() -> int:
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem"
    ) as key:
        try:
            return int(winreg.QueryValueEx(key, "LongPathsEnabled")[0])
        except FileNotFoundError:
            return 0


def request(port: int, path: str, payload: dict | None = None) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"},
    )
    with opener.open(req, timeout=2) as response:
        return response.status, response.read()


def proof(nonce: str, payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hmac.new(nonce.encode("ascii"), canonical.encode("ascii"), hashlib.sha256).hexdigest()


def control_directory(root: Path, fingerprint: str, record_length: int) -> Path:
    padding = record_length - len(str(root / fingerprint / RECORD)) - 1
    if not 1 <= padding <= 255:
        raise AssertionError("Use a shorter --workdir for the Windows MAX_PATH boundary probe")
    result = root / ("x" * padding) / fingerprint
    assert len(str(result / RECORD)) == record_length
    return result


def run_case(gateway: Path, root: Path, record_length: int, result: dict) -> None:
    profile = root / "profile"
    profile.mkdir(parents=True)
    cwd = root / "empty-cwd"
    for directory in (cwd, root / "temp", root / "appdata", root / "localappdata"):
        directory.mkdir()
    fingerprint = profile_fingerprint(profile)
    control = control_directory(root / "ownership", fingerprint, record_length)
    record_path, lock_path = control / RECORD, control / LOCK
    config = profile / "config.toml"
    config.write_text(
        f"config_version = 1\nstate_dir = {json.dumps(str(profile / 'state'))}\n"
        f"workspace_dir = {json.dumps(str(profile / 'workspace'))}\n"
        '[llm]\nprovider = "ollama"\nmodel = "ownership-native-probe"\n'
        'base_url = "http://127.0.0.1:9"\ncontext_window_tokens = 131072\n'
        '[squilla_router]\nenabled = false\n[llm_ensemble]\nenabled = false\n'
        '[privacy]\ndisable_network_observability = true\n',
        encoding="utf-8",
    )
    base_env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith(("OPENSQUILLA_", "UV_"))
        and not any(
            part in key.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
        )
        and key.upper() not in {"PYTHONPATH", "PYTHONHOME"}
    }
    base_env.update({
        "HOME": str(profile), "USERPROFILE": str(profile),
        "APPDATA": str(root / "appdata"), "LOCALAPPDATA": str(root / "localappdata"),
        "TEMP": str(root / "temp"), "TMP": str(root / "temp"),
        "OPENSQUILLA_DESKTOP": "1", "OPENSQUILLA_INSTALL_METHOD": "desktop",
        "OPENSQUILLA_STATE_DIR": str(profile),
        "OPENSQUILLA_USER_STATE_DIR": str(root / "user-state"),
        "OPENSQUILLA_TEST_PROFILE_LOCK_ROOT": "1",
        "OPENSQUILLA_GATEWAY_CONFIG_PATH": str(config),
        "OPENSQUILLA_CONTROL_UI_DIST": str(gateway.parent.parent / "control-ui-dist"),
        "OPENSQUILLA_DESKTOP_GATEWAY_OWNERSHIP_DIR": str(control),
        "OPENSQUILLA_TESTING": "0", "GITHUB_ACTIONS": "0", "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8:replace",
        "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
        "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": "127.0.0.1,localhost,::1",
    })
    result.update(record_path_length=len(str(record_path)), lock_path_length=len(str(lock_path)))
    lock_identity = None
    previous_identity = None
    for attempt in (1, 2):
        nonce = secrets.token_urlsafe(32)
        env = {**base_env, "OPENSQUILLA_DESKTOP_GATEWAY_INSTANCE_NONCE": nonce}
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        boot = {"attempt": attempt, "ok": False}
        result["boots"].append(boot)
        log = root / f"boot-{attempt}.log"
        with log.open("wb") as stream:
            started = time.monotonic()
            child = subprocess.Popen(
                [str(gateway), "gateway", "run", "--bind", "127.0.0.1", "--port", str(port),
                 "--config", str(config)],
                cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            temporary_length = len(str(control / f".{RECORD}.{child.pid}.{'0' * 16}.tmp"))
            boot.update(pid=child.pid, temporary_path_length=temporary_length)
            try:
                assert temporary_length > 260
                while True:
                    if child.poll() is not None:
                        raise AssertionError(
                            f"Gateway exited before ready ({child.returncode}); {log}"
                        )
                    try:
                        if all(
                            request(port, path)[0] == 200
                            for path in ("/health", "/ready", "/control/")
                        ):
                            break
                    except (OSError, urllib.error.URLError):
                        pass
                    if time.monotonic() - started > TIMEOUT_SECONDS:
                        raise AssertionError(f"Gateway readiness deadline exceeded; {log}")
                    time.sleep(0.2)
                with open(native_path(record_path), encoding="utf-8") as handle:
                    record = json.load(handle)
                assert record["pid"] == child.pid
                assert record["instance_nonce"] == nonce
                assert record["profile_fingerprint"] == fingerprint
                assert record["port"] == port
                public = {key: value for key, value in record.items() if key != "instance_nonce"}
                challenge = secrets.token_urlsafe(32)
                status, body = request(port, "/api/desktop/identity", {"challenge": challenge})
                identity = json.loads(body)
                assert status == 200
                assert identity == {**public, "challenge": challenge,
                                    "proof": proof(nonce, {**public, "challenge": challenge})}
                current_identity = (record["pid"], record["start_identity"])
                assert current_identity != previous_identity
                previous_identity = current_identity
                current_lock = os.stat(native_path(lock_path)).st_ino
                if lock_identity is not None:
                    assert current_lock == lock_identity
                lock_identity = current_lock
                boot.update(ready_seconds=round(time.monotonic() - started, 2),
                            identity_verified=True, fingerprint=fingerprint)
                shutdown = {**public, "action": "shutdown", "challenge": challenge}
                assert request(port, "/api/desktop/shutdown", {
                    "challenge": challenge, "proof": proof(nonce, shutdown),
                })[0] == 202
                assert child.wait(timeout=TIMEOUT_SECONDS) == 0
                assert not os.path.exists(native_path(record_path))
                assert os.stat(native_path(lock_path)).st_ino == lock_identity
                assert os.listdir(native_path(control)) == [LOCK]
                boot.update(ok=True, exit_code=0, record_removed=True, lock_inode_preserved=True)
            finally:
                if child.poll() is None:
                    boot["forced_failure_cleanup"] = True
                    child.kill()
                    child.wait(timeout=10)
    result["ok"] = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("The ownership long-path probe requires native Windows")
    gateway, workdir, output = args.gateway.resolve(), args.workdir.resolve(), args.output.resolve()
    if not gateway.is_file():
        raise SystemExit("Packaged Gateway executable is missing")
    workdir.mkdir(parents=True, exist_ok=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "ok": False, "os": platform.platform(), "long_paths_enabled": long_paths_enabled(),
        "gateway_sha256": hashlib.sha256(gateway.read_bytes()).hexdigest(), "cases": [],
    }
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    for name, length in (("temporary-over-max-path", 245), ("record-lock-over-max-path", 310)):
        result = {"name": name, "ok": False, "boots": []}
        report["cases"].append(result)
        try:
            run_case(gateway, workdir / name, length, result)
        except Exception as error:  # noqa: BLE001 - retain every independent case's evidence
            result["error"] = f"{type(error).__name__}: {error}"
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["ok"] = all(case["ok"] for case in report["cases"])
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
