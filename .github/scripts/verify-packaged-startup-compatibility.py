"""Cold-start a packaged Gateway twice with each retained auto_setup input.

Every profile is created beneath a new --workdir; existing directories are
refused. No provider calls or real profile are needed. Readiness and an
owner-requested graceful exit are required; forced cleanup is always failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

AUTO_SETUP_ENV = "OPENSQUILLA_GATEWAY_SANDBOX__AUTO_SETUP"
CASES = [("clean", None)] + [
    (source, value) for source in ("process-env", "profile-dotenv", "toml")
    for value in ("true", "false")
]
READINESS_PATHS = ("/healthz", "/ready", "/control/")


def prepare_case(root: Path, gateway: Path, source: str, value: str | None) -> dict:
    name = source if value is None else f"{source}-{value}"
    profile = root / name
    profile.mkdir()
    cwd = profile / "empty-cwd"
    cwd.mkdir()
    config = profile / "config.toml"
    config.write_text(
        f"state_dir = {json.dumps(str(profile / 'state'))}\n"
        f"workspace_dir = {json.dumps(str(profile / 'workspace'))}\n"
        '[auth]\nmode = "none"\n'
        '[llm]\nprovider = "ollama"\nmodel = "startup-compatibility-fixture"\n'
        'base_url = "http://127.0.0.1:11434"\n'
        "[squilla_router]\nenabled = false\n"
        "[llm_ensemble]\nenabled = false\n"
        "[privacy]\ndisable_network_observability = true\n"
        '[sandbox]\nrun_mode = "full"\n'
        + (f"auto_setup = {value}\n" if source == "toml" else "")
        + '[control_ui]\nenabled = true\nbase_path = "/control"\n',
        encoding="utf-8",
    )
    if source == "profile-dotenv":
        (profile / ".env").write_text(f"{AUTO_SETUP_ENV}={value}\n", encoding="utf-8")
    environment = {
        key: val for key, val in os.environ.items()
        if not key.upper().startswith("OPENSQUILLA_")
        and key.upper() not in {"PYTHONPATH", "PYTHONHOME"}
        and not re.search(r"API.?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", key, re.I)
    }
    environment.update({
        "HOME": str(profile), "USERPROFILE": str(profile),
        "APPDATA": str(profile / "appdata"), "LOCALAPPDATA": str(profile / "localappdata"),
        "OPENSQUILLA_DESKTOP": "1", "OPENSQUILLA_INSTALL_METHOD": "desktop",
        "OPENSQUILLA_PROFILE_KIND": "desktop-primary",
        "OPENSQUILLA_STATE_DIR": str(profile),
        "OPENSQUILLA_USER_STATE_DIR": str(profile / "user-state"),
        "OPENSQUILLA_GATEWAY_CONFIG_PATH": str(config),
        "OPENSQUILLA_CONTROL_UI_DIST": str(gateway.parent.parent / "control-ui-dist"),
        "OPENSQUILLA_TELEMETRY_DISABLED": "1",
        "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8:replace",
    })
    # Do not set RECOVERY_OFFLINE: the production CLI would skip profile .env.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        environment[key] = "http://127.0.0.1:1"
    environment["NO_PROXY"] = environment["no_proxy"] = "127.0.0.1,localhost"
    if source == "process-env":
        environment[AUTO_SETUP_ENV] = value
    return {"name": name, "profile": profile, "cwd": cwd, "config": config, "env": environment}


def request(port: int, path: str, *, shutdown: bool = False) -> int:
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(
        url, data=b"{}" if shutdown else None,
        headers={"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=2) as response:
        return response.status


def boot_gateway(gateway: Path, case: dict, attempt: int, *, timeout: float = 90) -> dict:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    log = case["profile"] / f"gateway-boot-{attempt}.log"
    result = {"attempt": attempt, "port": port, "log": str(log), "forced_cleanup": False}
    child = None
    started = time.monotonic()
    with log.open("wb") as stream:
        try:
            child = subprocess.Popen(
                [str(gateway), "gateway", "run", "--bind", "127.0.0.1", "--port", str(port),
                 "--config", str(case["config"])],
                cwd=case["cwd"], env=case["env"], stdout=stream, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            result["pid"] = child.pid
            while True:
                if child.poll() is not None:
                    raise RuntimeError(f"Gateway exited before ready ({child.returncode})")
                try:
                    statuses = {path: request(port, path) for path in READINESS_PATHS}
                    if all(status == 200 for status in statuses.values()):
                        result["readiness"] = statuses
                        result["ready_seconds"] = round(time.monotonic() - started, 2)
                        break
                except (OSError, urllib.error.URLError):
                    pass
                if time.monotonic() - started > timeout:
                    raise TimeoutError("Gateway readiness deadline exceeded")
                time.sleep(0.2)
            result["shutdown_status"] = request(port, "/api/system/shutdown", shutdown=True)
            if result["shutdown_status"] != 202:
                raise RuntimeError("Gateway rejected graceful shutdown")
            if child.wait(timeout=timeout) != 0:
                raise RuntimeError(f"Gateway graceful exit failed ({child.returncode})")
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if child is not None:
                if child.poll() is None:
                    result["forced_cleanup"] = True
                    child.kill()
                    child.wait(timeout=10)
                result["exit_code"] = child.returncode
    result["ok"] = (
        "error" not in result and not result["forced_cleanup"]
        and result.get("exit_code") == 0 and result.get("shutdown_status") == 202
        and result.get("readiness") == dict.fromkeys(READINESS_PATHS, 200)
    )
    return result


def run(gateway: Path, workdir: Path, output: Path) -> dict:
    gateway, workdir, output = gateway.resolve(), workdir.resolve(), output.resolve()
    if not gateway.is_file():
        raise FileNotFoundError(f"Packaged Gateway not found: {gateway}")
    if workdir.exists() or output.exists():
        raise FileExistsError("Use a new synthetic workdir and output; existing paths are refused")
    workdir.mkdir(parents=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "gateway": str(gateway), "gateway_sha256": hashlib.sha256(gateway.read_bytes()).hexdigest(),
        "workdir": str(workdir), "cases": [], "ok": False,
    }

    def save() -> None:
        output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    save()
    for source, value in CASES:
        case = prepare_case(workdir, gateway, source, value)
        row = {"name": case["name"], "profile": str(case["profile"]), "boots": []}
        result["cases"].append(row)
        for attempt in (1, 2):
            boot = boot_gateway(gateway, case, attempt)
            row["boots"].append(boot)
            save()
            print(json.dumps({"case": case["name"], **boot}), flush=True)
        row["ok"] = all(boot["ok"] for boot in row["boots"])
        if source == "toml":
            migrated = tomllib.loads(case["config"].read_text(encoding="utf-8"))
            row["retired_toml_removed"] = "auto_setup" not in migrated["sandbox"]
            row["ok"] = row["ok"] and row["retired_toml_removed"]
        elif source == "profile-dotenv":
            row["dotenv_retained"] = (case["profile"] / ".env").read_text(encoding="utf-8") == (
                f"{AUTO_SETUP_ENV}={value}\n"
            )
            row["ok"] = row["ok"] and row["dotenv_retained"]
        save()
    result["ok"] = all(case["ok"] for case in result["cases"])
    save()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return 0 if run(args.gateway, args.workdir, args.output)["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
