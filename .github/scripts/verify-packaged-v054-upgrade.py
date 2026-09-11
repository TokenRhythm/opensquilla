"""Exercise the installed Gateway on a synthetic complete v0.5.4 database twice.

No provider calls. Readiness, history, ledger and owner-requested graceful exits
are required. Forced termination is failure cleanup only, never a passing exit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

from upgrade_baseline import read_ledger, verify_ledger

ROOT = Path(__file__).resolve().parents[2]


def applied_operations(database: Path) -> dict[str, str]:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        return dict(connection.execute(
            "SELECT id, migration_id FROM _yoyo_log WHERE operation = 'apply'"
        ))


def history(database: Path) -> list:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise AssertionError("upgraded database integrity_check failed")
        sessions = connection.execute(
            "SELECT session_key, session_id FROM sessions ORDER BY session_key"
        ).fetchall()
        messages = connection.execute(
            "SELECT session_id, session_key, message_id, role, content, created_at "
            "FROM transcript_entries ORDER BY id"
        ).fetchall()
        if not sessions or not messages:
            raise AssertionError("upgrade gate requires retained sessions and messages")
        return [sessions, messages]


def request(port: int, path: str, *, shutdown: bool = False) -> int:
    url = f"http://127.0.0.1:{port}{path}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        url, data=b"{}" if shutdown else None,
        headers={"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"},
    )
    with opener.open(req, timeout=2) as response:
        return response.status


def run(gateway: Path, home: Path, output: Path, *, timeout: float = 90) -> dict:
    gateway, home, output = gateway.resolve(), home.resolve(), output.resolve()
    database = home / "state/sessions.db"
    # Refuse arbitrary profiles; this command only accepts the preservation seed.
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("SELECT COUNT(*) FROM release_preservation_chat").fetchone() != (1,):
            raise AssertionError("not a synthetic release preservation profile")
    verify_ledger(database, exact=True)
    before = history(database)
    expected = {path.stem for path in (ROOT / "migrations").glob("V*.py")}
    initial = read_ledger(database)
    if not set(initial) <= expected:
        raise AssertionError("candidate source no longer retains the complete v0.5.4 ledger")
    # Desktop requires the profile's canonical config.toml. The dedicated seed
    # already disables Router and uses an uncalled loopback provider.
    config = home / "config.toml"
    runtime = gateway.parent.parent
    environment = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("OPENSQUILLA_")
    }
    environment.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "APPDATA": str(home / "appdata"), "LOCALAPPDATA": str(home / "localappdata"),
        "OPENSQUILLA_DESKTOP": "1", "OPENSQUILLA_INSTALL_METHOD": "desktop",
        "OPENSQUILLA_STATE_DIR": str(home),
        "OPENSQUILLA_USER_STATE_DIR": str(home / "user-state"),
        "OPENSQUILLA_GATEWAY_CONFIG_PATH": str(config),
        "OPENSQUILLA_CONTROL_UI_DIST": str(runtime / "control-ui-dist"),
        "OPENSQUILLA_RECOVERY_OFFLINE": "1", "PYTHONUTF8": "1",
        "OPENSQUILLA_TELEMETRY_DISABLED": "1",
    })
    result = {
        "gateway_sha256": hashlib.sha256(gateway.read_bytes()).hexdigest(),
        "baseline_ids": sorted(initial), "expected_ids": sorted(expected), "boots": [],
    }
    previous = initial
    previous_operations = applied_operations(database)
    output.parent.mkdir(parents=True, exist_ok=True)
    for attempt in (1, 2):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        log = output.with_name(f"{output.stem}-boot-{attempt}.log")
        with log.open("wb") as stream:
            started = time.monotonic()
            child = subprocess.Popen(
                [str(gateway), "gateway", "run", "--bind", "127.0.0.1", "--port", str(port),
                 "--config", str(config)],
                cwd=gateway.parent, env=environment, stdout=stream, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                while True:
                    if child.poll() is not None:
                        raise AssertionError(
                            f"Gateway exited before ready ({child.returncode}); {log}"
                        )
                    try:
                        if all(
                            request(port, path) == 200
                            for path in ("/health", "/ready", "/control/")
                        ):
                            break
                    except (OSError, urllib.error.URLError):
                        pass
                    if time.monotonic() - started > timeout:
                        raise AssertionError(f"Gateway readiness deadline exceeded; {log}")
                    time.sleep(0.2)
                ready_seconds = round(time.monotonic() - started, 2)
                if request(port, "/api/system/shutdown", shutdown=True) != 202:
                    raise AssertionError("Gateway rejected graceful shutdown")
                if child.wait(timeout=timeout) != 0:
                    raise AssertionError(f"Gateway graceful exit failed; {log}")
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
        ledger = verify_ledger(database)
        if set(ledger) != expected or (attempt == 2 and ledger != previous):
            raise AssertionError("unexpected or repeated migration ledger change")
        operations = applied_operations(database)
        added_operations = [
            value for key, value in operations.items() if key not in previous_operations
        ]
        if sorted(added_operations) != sorted(set(ledger) - set(previous)):
            raise AssertionError("migration apply audit does not match newly added IDs")
        if history(database) != before:
            raise AssertionError("retained session/message identity, order or content changed")
        result["boots"].append({
            "attempt": attempt, "ready_seconds": ready_seconds, "exit_code": 0,
            "added_ids": sorted(set(ledger) - set(previous)), "history_retained": True,
        })
        previous = ledger
        previous_operations = operations
    result["ok"] = True
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.gateway, args.home, args.output), indent=2))


if __name__ == "__main__":
    main()
