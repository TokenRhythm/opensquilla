#!/usr/bin/env python3
"""Drive a packaged Desktop build through injected failure environments.

The release gate this replaces only proved the packaged process stayed alive for
eight seconds and that ``recovery inspect`` reported ``ready``.  That is why the
0.5.0 profile-consolidation regression shipped: nothing asserted the user could
actually reach the control surface, and nothing built a hostile profile first.

Each scenario constructs an Electron ``userData`` tree, launches the packaged
application against it, and classifies the outcome as ``entered`` (the gateway
answered, so the user got into the product) or ``blocked`` (startup stopped on
the primary-repair page).  Scenarios then assert the consolidation side effects
that the maintainer's requirements depend on: every recovery profile consumed,
primary configuration authoritative, the container archived rather than deleted.

Usage:
    desktop_fault_injection.py list
    desktop_fault_injection.py run --scenario NAME --app /path/OpenSquilla.app \\
        --workdir DIR [--port 18931] [--timeout 180] [--report out.json]
    desktop_fault_injection.py run --scenario NAME --workdir DIR --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# A launch that neither answers nor records a terminal event inside this window
# is reported as ``timeout`` rather than silently passing.
DEFAULT_TIMEOUT_SECONDS = 180
_WINDOWS = sys.platform == "win32"
POLL_INTERVAL_SECONDS = 1.0
_FAKE_MODEL = "synthetic-fault-injection-model"

_MINIMAL_SESSION_SCHEMA = """
CREATE TABLE sessions (
    session_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    label TEXT,
    estimated_cost_usd REAL NOT NULL DEFAULT 0.0
);
CREATE TABLE transcript_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    message_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    created_at INTEGER NOT NULL
);
"""

# Terminal desktop.log events. Reaching either means the launch has decided.
_BLOCKED_EVENTS = frozenset({"desktop_open_failed"})
_CONSOLIDATION_EVENT = "desktop_profile_consolidation_completed"


def _seed_sessions_db(path: Path, *, session_key: str, session_id: str, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(_MINIMAL_SESSION_SCHEMA)
        connection.execute(
            "INSERT INTO sessions(session_key, session_id, updated_at, label) VALUES (?, ?, 1, ?)",
            (session_key, session_id, label),
        )
        connection.execute(
            "INSERT INTO transcript_entries("
            "session_id, session_key, message_id, role, content, created_at"
            ") VALUES (?, ?, ?, 'user', ?, 1)",
            (session_id, session_key, f"message-{session_id}", label),
        )
        connection.commit()
    finally:
        connection.close()


def _session_labels(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT label FROM sessions").fetchall()
    finally:
        connection.close()
    return {str(row[0]) for row in rows}


class _FakeProviderHandler(BaseHTTPRequestHandler):
    """Answer the few provider calls the desktop performs while starting up."""

    protocol_version = "HTTP/1.1"

    def _json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._json({"object": "list", "data": [{"id": _FAKE_MODEL}]})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        self._json(
            {
                "id": "fault-injection",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def _start_fake_provider() -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def _credential(provider_port: int, marker: str) -> str:
    """A plain-encryption credential so startup skips onboarding.

    Without one the desktop legitimately waits in setup and never binds a
    gateway, which would look identical to a blocked startup.
    """

    return (
        json.dumps(
            {
                "provider": "minimax_openai",
                "model": _FAKE_MODEL,
                "baseUrl": f"http://127.0.0.1:{provider_port}/v1",
                "encryptedApiKey": marker,
                "modelRoutingMode": "direct",
                "routerMode": "disabled",
                "searchProvider": "duckduckgo",
                "encryption": "plain",
                "createdAt": "2026-07-20T00:00:00.000Z",
                "updatedAt": "2026-07-20T00:00:00.000Z",
            },
            indent=2,
        )
        + "\n"
    )


def _marker_config(marker: int) -> str:
    """A valid, non-empty GatewayConfig body tagged with a distinguishable value.

    Synthetic keys are rejected by the real gateway (``extra_forbidden``), which
    would look like a consolidation failure rather than a bad fixture.  The
    value must also keep the file non-empty: an empty or comment-only
    ``config.toml`` counts as *no* configuration for the adoption predicate.
    """

    return f"log_file_backup_count = {marker}\n"


def _write_primary_context(user_data: Path) -> None:
    (user_data / "desktop-profile-context.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_profile_kind": "primary",
                "active_recovery_id": None,
                "attention_acknowledgement": None,
                "updated_at": "2026-07-13T00:00:00.000Z",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


@dataclass
class Fixture:
    """The userData tree a scenario launches against."""

    user_data: Path
    recovery_ids: list[str] = field(default_factory=list)
    expected_session_labels: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    # Seeded by a warmup launch rather than written directly: only the packaged
    # gateway knows the current session schema.
    primary_session_label: str | None = None


def _primary(
    user_data: Path,
    *,
    config: str | None,
    session_label: str | None,
    provider_port: int,
    credential: bool = True,
) -> Path:
    home = user_data / "opensquilla"
    (home / "workspace").mkdir(parents=True, exist_ok=True)
    (home / "state").mkdir(parents=True, exist_ok=True)
    if config is not None:
        (home / "config.toml").write_text(config, encoding="utf-8")
    # A primary sessions.db is created by the warmup launch, not fabricated
    # here: a hand-written schema makes the live gateway exit with
    # "no such column".
    del session_label
    if credential:
        (user_data / "desktop-credential.json").write_text(
            _credential(provider_port, "primary-credential"), encoding="utf-8"
        )
    _write_primary_context(user_data)
    return home


def _recovery(
    user_data: Path,
    recovery_id: str,
    *,
    config: str,
    session_label: str,
    provider_port: int,
) -> Path:
    root = user_data / "recovery-profiles" / recovery_id
    home = root / "opensquilla"
    (home / "workspace").mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(config, encoding="utf-8")
    (home / ".env").write_text(f"OPENSQUILLA_SOURCE_MARKER={recovery_id}\n", encoding="utf-8")
    (root / "desktop-credential.json").write_text(
        _credential(provider_port, f"recovery-credential-{recovery_id}"), encoding="utf-8"
    )
    (home / "workspace" / "MEMORY.md").write_text(f"memory {recovery_id}\n", encoding="utf-8")
    _seed_sessions_db(
        home / "state" / "sessions.db",
        # A distinct key per source avoids conflating this harness's coverage
        # with the separate same-key collision path.
        session_key=f"agent:{recovery_id[:8]}:main",
        session_id=f"session-{recovery_id}",
        label=session_label,
    )
    return root


# ── Scenario builders ──────────────────────────────────────────────────────


def _fresh_install(user_data: Path, provider_port: int) -> Fixture:
    """The common case: no legacy container at all. Must reach the product."""

    _primary(user_data, config="", session_label=None, provider_port=provider_port)
    return Fixture(user_data, notes=["no recovery-profiles container exists"])


def _single_recovery(user_data: Path, provider_port: int) -> Fixture:
    _primary(
        user_data,
        config=_marker_config(1),
        session_label="primary chat",
        provider_port=provider_port,
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config=_marker_config(2),
        session_label="recovery A",
        provider_port=provider_port,
    )
    return Fixture(
        user_data,
        recovery_ids=[recovery_id],
        expected_session_labels={"primary chat", "recovery A"},
        primary_session_label="primary chat",
    )


def _multi_recovery(user_data: Path, provider_port: int) -> Fixture:
    """R1: every recovery profile is consolidated, with no chooser."""

    _primary(
        user_data,
        config=_marker_config(1),
        session_label="primary chat",
        provider_port=provider_port,
    )
    ids = []
    for index in range(3):
        recovery_id = str(uuid.uuid4())
        ids.append(recovery_id)
        _recovery(
            user_data,
            recovery_id,
            config=_marker_config(10 + index),
            session_label=f"recovery {index}",
            provider_port=provider_port,
        )
    return Fixture(
        user_data,
        recovery_ids=ids,
        expected_session_labels={"primary chat", "recovery 0", "recovery 1", "recovery 2"},
        primary_session_label="primary chat",
    )


def _empty_primary_config(user_data: Path, provider_port: int) -> Fixture:
    """R2: an empty primary adopts configuration from the newest recovery.

    No primary credential either: a credential on its own counts as existing
    configuration, so seeding one would skip the adoption path under test. The
    gateway therefore only starts if the adopted recovery credential works.
    """

    _primary(
        user_data,
        config="",
        session_label=None,
        provider_port=provider_port,
        credential=False,
    )
    older = str(uuid.uuid4())
    _recovery(
        user_data,
        older,
        config=_marker_config(3),
        session_label="older recovery",
        provider_port=provider_port,
    )
    time.sleep(1.1)
    newer = str(uuid.uuid4())
    _recovery(
        user_data,
        newer,
        config=_marker_config(7),
        session_label="newer recovery",
        provider_port=provider_port,
    )
    return Fixture(
        user_data,
        recovery_ids=[older, newer],
        expected_session_labels={"older recovery", "newer recovery"},
        notes=[f"newest recovery is {newer}"],
    )


def _corrupt_primary_config(user_data: Path, provider_port: int) -> Fixture:
    """R2: a corrupt-but-present primary configuration stays authoritative."""

    _primary(
        user_data,
        config="this is = = not valid toml [\n",
        session_label="primary chat",
        provider_port=provider_port,
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config=_marker_config(4),
        session_label="recovery A",
        provider_port=provider_port,
    )
    return Fixture(
        user_data,
        recovery_ids=[recovery_id],
        expected_session_labels={"primary chat", "recovery A"},
        primary_session_label="primary chat",
    )


def _stray_shell_metadata(user_data: Path, provider_port: int) -> Fixture:
    """Inert shell/antivirus files must never strand startup."""

    _primary(
        user_data,
        config=_marker_config(1),
        session_label="primary chat",
        provider_port=provider_port,
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config=_marker_config(2),
        session_label="recovery A",
        provider_port=provider_port,
    )
    container = user_data / "recovery-profiles"
    (container / ".DS_Store").write_bytes(b"finder metadata")
    (container / ".localized").write_bytes(b"")
    (container / "desktop.ini").write_bytes(b"[.ShellClassInfo]\n")
    (container / "Thumbs.db").write_bytes(b"thumbs")
    (container / "sessions.db.avquarantine").write_bytes(b"quarantine sidecar")
    return Fixture(
        user_data,
        recovery_ids=[recovery_id],
        expected_session_labels={"primary chat", "recovery A"},
        primary_session_label="primary chat",
        notes=["five inert stray files in the container"],
    )


def _stray_directory(user_data: Path, provider_port: int) -> Fixture:
    """A profile-shaped directory is a deliberate fail-closed boundary."""

    _primary(
        user_data,
        config=_marker_config(1),
        session_label="primary chat",
        provider_port=provider_port,
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config=_marker_config(2),
        session_label="recovery A",
        provider_port=provider_port,
    )
    (user_data / "recovery-profiles" / f"{uuid.uuid4()} - Copy").mkdir()
    return Fixture(
        user_data,
        recovery_ids=[recovery_id],
        notes=["a manual '- Copy' directory must block rather than be archived blindly"],
    )


def _only_stray_files(user_data: Path, provider_port: int) -> Fixture:
    """A container holding no real profile is a noop, not a blocked startup."""

    _primary(
        user_data,
        config=_marker_config(1),
        session_label="primary chat",
        provider_port=provider_port,
    )
    container = user_data / "recovery-profiles"
    container.mkdir(parents=True)
    (container / "desktop.ini").write_bytes(b"[.ShellClassInfo]\n")
    (container / "Thumbs.db").write_bytes(b"thumbs")
    return Fixture(
        user_data,
        expected_session_labels={"primary chat"},
        primary_session_label="primary chat",
    )


def _deny_read(path: Path) -> str:
    """Make ``path`` unreadable, and say how it was done.

    ``chmod 000`` does not deny the owner a read on Windows, so fall back to an
    explicit ACL denial there and report when neither is available.
    """

    if not _WINDOWS:
        os.chmod(path, 0o000)
        return "mode 000"
    account = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if account:
        completed = subprocess.run(
            ["icacls", str(path), "/deny", f"{account}:(R)"],
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0:
            return f"ACL read denied for {account}"
    os.chmod(path, 0o444)
    return "read-only attribute only (read denial unavailable)"


def _windows_reparse_point(user_data: Path, provider_port: int) -> Fixture:
    """A directory junction in the container must not be followed.

    Windows reparse points are the analogue of the POSIX symlink boundary, and
    ``mklink /J`` needs no elevation, so ordinary users really can create one.
    """

    _primary(
        user_data,
        config=_marker_config(1),
        session_label="primary chat",
        provider_port=provider_port,
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config=_marker_config(2),
        session_label="recovery A",
        provider_port=provider_port,
    )
    outside = user_data.parent / "outside-the-container"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "canary.txt").write_text("must not be touched\n", encoding="utf-8")
    junction = user_data / "recovery-profiles" / "junction-probe"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        check=False,
    )
    created = completed.returncode == 0
    return Fixture(
        user_data,
        recovery_ids=[recovery_id],
        primary_session_label="primary chat",
        notes=[
            f"junction created: {created}"
            + ("" if created else f" ({completed.stderr.decode(errors='replace').strip()})")
        ],
    )


def _readonly_recovery_source(user_data: Path, provider_port: int) -> Fixture:
    """A recovery profile the process cannot read must not lose primary access."""

    _primary(
        user_data,
        config=_marker_config(1),
        session_label="primary chat",
        provider_port=provider_port,
    )
    recovery_id = str(uuid.uuid4())
    root = _recovery(
        user_data,
        recovery_id,
        config=_marker_config(2),
        session_label="recovery A",
        provider_port=provider_port,
    )
    database = root / "opensquilla" / "state" / "sessions.db"
    note = _deny_read(database)
    return Fixture(
        user_data,
        recovery_ids=[recovery_id],
        notes=[f"recovery sessions.db: {note}; primary itself is healthy"],
    )


SCENARIOS: dict[str, dict[str, Any]] = {
    "fresh-install": {
        "build": _fresh_install,
        "expect": "entered",
        "why": "A new 0.5.x install has no legacy container; consolidation must be a fast noop.",
    },
    "single-recovery": {
        "build": _single_recovery,
        "expect": "entered",
        "consume_all": True,
        "assert_sessions": True,
        "why": "The baseline upgrade path: one legacy profile folded into primary.",
    },
    "multi-recovery": {
        "build": _multi_recovery,
        "expect": "entered",
        "consume_all": True,
        "assert_sessions": True,
        "why": "R1: several recovery profiles are all consolidated with no chooser.",
    },
    "empty-primary-config": {
        "build": _empty_primary_config,
        "expect": "entered",
        "consume_all": True,
        "assert_sessions": True,
        "assert_config_adopted": True,
        "why": "R2: an empty primary adopts configuration from the newest recovery only.",
    },
    "corrupt-primary-config": {
        "build": _corrupt_primary_config,
        "expect": "entered",
        "consume_all": True,
        "assert_sessions": True,
        "why": "R2: corrupt-but-present primary configuration is never clobbered.",
    },
    "stray-shell-metadata": {
        "build": _stray_shell_metadata,
        "expect": "entered",
        "consume_all": True,
        "assert_sessions": True,
        "why": "Explorer and antivirus write into a folder the app created; that cannot brick it.",
    },
    "stray-directory": {
        "build": _stray_directory,
        "expect": "blocked",
        "why": "Deliberate boundary: an unknown directory blocks up front, not mid-archival.",
    },
    "only-stray-files": {
        "build": _only_stray_files,
        "expect": "entered",
        "assert_sessions": True,
        "why": "A container with no real profile must be a noop.",
    },
    "windows-reparse-point": {
        "build": _windows_reparse_point,
        "expect": "blocked",
        "why": (
            "Windows-only: a directory junction in the container is the reparse-point "
            "analogue of the symlink boundary and must be refused, not followed."
        ),
        "platforms": ("win32",),
    },
    "readonly-recovery-source": {
        "build": _readonly_recovery_source,
        "expect": "entered",
        "why": (
            "An unreadable legacy source must not cost the user their healthy primary: "
            "the fan-in fails, startup continues silently against the primary, and a "
            "later launch retries."
        ),
    },
}


def _insert_session_row(database: Path, *, session_key: str, session_id: str, label: str) -> bool:
    """Insert one session using the live schema discovered from the database.

    The packaged gateway owns the session schema and it moves between releases,
    so the row is built from ``PRAGMA table_info`` rather than a pinned DDL.
    """

    if not database.is_file():
        return False
    connection = sqlite3.connect(database)
    try:
        columns = connection.execute("PRAGMA table_info(sessions)").fetchall()
        if not columns:
            return False
        names = {str(column[1]) for column in columns}
        values: dict[str, Any] = {}
        for column in columns:
            name, declared, not_null, default = str(column[1]), str(column[2]), column[3], column[4]
            if name == "session_key":
                values[name] = session_key
            elif name == "session_id":
                values[name] = session_id
            elif name in {"label", "title", "name"}:
                values[name] = label
            elif not_null and default is None:
                # Satisfy remaining NOT NULL columns with a type-appropriate zero.
                upper = declared.upper()
                if "INT" in upper:
                    values[name] = 0
                elif "REAL" in upper or "FLOA" in upper or "DOUB" in upper:
                    values[name] = 0.0
                else:
                    values[name] = ""
        if "session_key" not in names or "session_id" not in names:
            return False
        placeholders = ", ".join("?" for _ in values)
        quoted = ", ".join(f'"{name}"' for name in values)
        connection.execute(
            f"INSERT OR REPLACE INTO sessions({quoted}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        connection.commit()
    except sqlite3.Error:
        return False
    finally:
        connection.close()
    return True


def _warmup_primary(
    binary: Path,
    user_data: Path,
    *,
    port: int,
    isolated_home: Path,
    label: str,
    timeout: float,
) -> dict[str, Any]:
    """Let the packaged gateway create a real primary database, then seed a row.

    Recovery sources are staged out of the way so this launch cannot consolidate
    before the primary has any content worth preserving.
    """

    container = user_data / "recovery-profiles"
    parked = user_data.parent / "staged-recovery-profiles"
    staged = container.exists()
    if staged:
        shutil.move(str(container), str(parked))

    process = _launch(binary, user_data, port=port, isolated_home=isolated_home)
    try:
        deadline = time.monotonic() + timeout
        answered = False
        while time.monotonic() < deadline:
            if _gateway_answered(port):
                answered = True
                break
            if process.poll() is not None:
                break
            time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        _terminate(process)

    database = user_data / "opensquilla" / "state" / "sessions.db"
    seeded = _insert_session_row(
        database, session_key="agent:main:main", session_id="primary-session", label=label
    )
    if staged:
        shutil.move(str(parked), str(container))
    return {"gateway_answered": answered, "database_seeded": seeded}


# ── Launch and classification ──────────────────────────────────────────────


def _app_binary(app: Path) -> Path:
    """Resolve the launchable executable from a macOS bundle or Windows layout."""

    if app.suffix == ".app":
        candidate = app / "Contents" / "MacOS" / "OpenSquilla"
        if candidate.is_file():
            return candidate
        raise SystemExit(f"no launchable binary inside {app}")
    if app.is_file():
        return app
    if app.is_dir():
        # An unpacked Windows install directory, or a portable extraction.
        for name in ("OpenSquilla.exe", "opensquilla.exe"):
            candidate = app / name
            if candidate.is_file():
                return candidate
        matches = sorted(app.glob("*.exe"))
        if len(matches) == 1:
            return matches[0]
        raise SystemExit(
            f"could not identify a single executable in {app}; pass --app <path-to-exe>"
        )
    raise SystemExit(f"unrecognized application path: {app}")


def _read_events(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.is_file():
        return []
    events = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _gateway_answered(port: int) -> bool:
    for path in ("/healthz", "/health"):
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed loopback URL
                f"http://127.0.0.1:{port}{path}", timeout=2
            ) as response:
                if 200 <= response.status < 500:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    return False


def _launch(
    binary: Path,
    user_data: Path,
    *,
    port: int,
    isolated_home: Path,
) -> subprocess.Popen[bytes]:
    environment = {
        key: value
        for key, value in os.environ.items()
        # Keep the X11 handles the Linux virtual display needs; drop ambient
        # OpenSquilla configuration so the fixture is the only input.
        if key in {"DISPLAY", "XAUTHORITY"} or not key.startswith("OPENSQUILLA_")
    }
    environment.update(
        {
            "HOME": str(isolated_home),
            "OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE": "1",
            "OPENSQUILLA_DESKTOP_GATEWAY_PORT": str(port),
            "OPENSQUILLA_DESKTOP_SECRET_STORAGE": "plain",
            "OPENSQUILLA_USER_STATE_DIR": str(isolated_home / "user-state"),
        }
    )
    log_handle = (user_data.parent / "launch-stdio.log").open("ab")
    # Electron spawns the gateway as a child, so the launch needs its own group
    # to be killable as a tree. Windows has no process groups in the POSIX
    # sense; CREATE_NEW_PROCESS_GROUP is the closest equivalent and taskkill /T
    # does the tree walk at termination.
    grouping: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
        if _WINDOWS
        else {"start_new_session": True}
    )
    return subprocess.Popen(
        [
            str(binary),
            "--use-mock-keychain",
            f"--user-data-dir={user_data}",
        ],
        env=environment,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        **grouping,
    )


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Stop the launch and every process it spawned.

    Leaving the gateway child alive would let it answer the next scenario's
    health probe and report a false success.
    """

    if process.poll() is not None:
        return
    if _WINDOWS:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=30)
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.wait(timeout=20)


def _classify(
    process: subprocess.Popen[bytes],
    log_path: Path,
    *,
    port: int,
    timeout: float,
    kill_after: float | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Return ``entered`` | ``blocked`` | ``timeout`` | ``killed`` plus log events."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if kill_after is not None and time.monotonic() >= kill_after:
            _terminate(process)
            return "killed", _read_events(log_path)
        if _gateway_answered(port):
            return "entered", _read_events(log_path)
        events = _read_events(log_path)
        if any(event.get("event") in _BLOCKED_EVENTS for event in events):
            return "blocked", events
        if process.poll() is not None and not _gateway_answered(port):
            # The process exited without ever answering.
            return "blocked", events
        time.sleep(POLL_INTERVAL_SECONDS)
    return "timeout", _read_events(log_path)


def _assert_side_effects(
    scenario: dict[str, Any],
    fixture: Fixture,
    events: list[dict[str, Any]],
) -> list[str]:
    """Return human-readable failures; empty means the scenario held."""

    failures: list[str] = []
    user_data = fixture.user_data
    primary_home = user_data / "opensquilla"
    container = user_data / "recovery-profiles"

    consolidation = [event for event in events if event.get("event") == _CONSOLIDATION_EVENT]

    if scenario.get("consume_all"):
        if not consolidation:
            failures.append(f"no {_CONSOLIDATION_EVENT} event was recorded")
        else:
            consumed = consolidation[-1].get("consumedRecoveryProfileCount")
            if consumed != len(fixture.recovery_ids):
                failures.append(
                    f"consolidation consumed {consumed} profiles, "
                    f"expected {len(fixture.recovery_ids)}"
                )
        if container.exists():
            failures.append(
                "recovery-profiles container is still in place; it must be archived so the "
                "on-disk world converges to a single primary"
            )
        backups = user_data / "backups" / "profile-consolidation"
        if not backups.is_dir() or not any(backups.iterdir()):
            failures.append("no consolidation backup was recorded; sources must be archived")

    if scenario.get("assert_sessions"):
        found = _session_labels(primary_home / "state" / "sessions.db")
        missing = fixture.expected_session_labels - found
        if missing:
            failures.append(f"sessions missing from the primary profile: {sorted(missing)}")

    if scenario.get("assert_config_adopted"):
        config = primary_home / "config.toml"
        text = config.read_text(encoding="utf-8") if config.is_file() else ""
        if _marker_config(7).strip() not in text:
            failures.append(
                "primary config.toml did not adopt the newest recovery configuration; "
                f"contents were {text!r}"
            )

    return failures


def _run_scenario(
    name: str,
    *,
    app: Path | None,
    workdir: Path,
    port: int,
    timeout: float,
    dry_run: bool,
) -> dict[str, Any]:
    scenario = SCENARIOS[name]
    platforms = scenario.get("platforms")
    if platforms and sys.platform not in platforms:
        return {
            "scenario": name,
            "why": scenario["why"],
            "expected": scenario["expect"],
            "verdict": "skipped",
            "skipped_reason": f"requires {'/'.join(platforms)}, running on {sys.platform}",
            "ok": True,
        }
    # Consolidation refuses profile roots reached through a link, and on macOS
    # /tmp is a symlink to /private/tmp. Resolve so the fixture exercises the
    # product rather than tripping the path guard.
    workdir = workdir.resolve()
    root = workdir / name
    if root.exists():
        shutil.rmtree(root)
    user_data = root / "user-data"
    isolated_home = root / "home"
    user_data.mkdir(parents=True)
    isolated_home.mkdir(parents=True)

    provider, provider_port = _start_fake_provider()
    try:
        fixture: Fixture = scenario["build"](user_data, provider_port)
    except BaseException:
        provider.shutdown()
        raise
    result: dict[str, Any] = {
        "scenario": name,
        "why": scenario["why"],
        "expected": scenario["expect"],
        "recovery_profiles": len(fixture.recovery_ids),
        "notes": fixture.notes,
        "user_data": str(user_data),
    }

    if dry_run:
        provider.shutdown()
        result["verdict"] = "dry-run"
        result["ok"] = True
        return result

    assert app is not None
    binary = _app_binary(app)
    if fixture.primary_session_label is not None:
        result["warmup"] = _warmup_primary(
            binary,
            user_data,
            port=port,
            isolated_home=isolated_home,
            label=fixture.primary_session_label,
            timeout=min(timeout, 120.0),
        )
        if not result["warmup"]["database_seeded"]:
            provider.shutdown()
            result["verdict"] = "warmup-failed"
            result["failures"] = [
                "the warmup launch never produced a seedable primary sessions.db, so this "
                "scenario cannot prove existing chats survive"
            ]
            result["ok"] = False
            return result
    process = _launch(binary, user_data, port=port, isolated_home=isolated_home)
    log_path = user_data / "logs" / "desktop.log"
    try:
        verdict, events = _classify(process, log_path, port=port, timeout=timeout)
    finally:
        _terminate(process)
        provider.shutdown()

    result["verdict"] = verdict
    result["events"] = [event.get("event") for event in events]
    blocked = [
        event for event in events if event.get("event") == "desktop_profile_consolidation_completed"
    ]
    if blocked:
        result["consolidation"] = blocked[-1]

    expected = scenario["expect"]
    failures: list[str] = []
    if expected != "any" and verdict != expected:
        failures.append(f"expected verdict {expected!r} but observed {verdict!r}")
    if verdict == "entered":
        failures.extend(_assert_side_effects(scenario, fixture, events))
    result["failures"] = failures
    result["ok"] = not failures
    return result


def _touch_tree(root: Path) -> None:
    """Bump mtimes the way a user browsing the folder in Finder/Explorer would."""

    if not root.exists():
        return
    stamp = time.time() + 1
    for path in [root, *root.rglob("*")]:
        try:
            os.utime(path, (stamp, stamp))
        except OSError:
            continue


def _run_wedge_probe(
    *,
    app: Path,
    workdir: Path,
    port: int,
    kill_after: float,
    timeout: float,
) -> dict[str, Any]:
    """Interrupt a consolidation, disturb the sources, then relaunch.

    Resume refuses to continue when a recorded source snapshot no longer
    reproduces, so a crash followed by an ordinary metadata change is the
    documented path into a permanently blocked startup.  This probe reports what
    actually happens instead of assuming.
    """

    workdir = workdir.resolve()
    label = f"wedge-{kill_after:g}s"
    root = workdir / label
    if root.exists():
        shutil.rmtree(root)
    user_data = root / "user-data"
    isolated_home = root / "home"
    user_data.mkdir(parents=True)
    isolated_home.mkdir(parents=True)
    provider, provider_port = _start_fake_provider()
    fixture = _multi_recovery(user_data, provider_port)

    binary = _app_binary(app)
    log_path = user_data / "logs" / "desktop.log"

    first = _launch(binary, user_data, port=port, isolated_home=isolated_home)
    try:
        deadline = time.monotonic() + kill_after
        entered_before_kill = False
        while time.monotonic() < deadline:
            if _gateway_answered(port):
                entered_before_kill = True
                break
            time.sleep(0.2)
    finally:
        _terminate(first)

    journal = user_data / ".opensquilla-profile-consolidation.json"
    journal_present = journal.is_file()
    journal_phase = None
    if journal_present:
        try:
            journal_phase = json.loads(journal.read_text(encoding="utf-8")).get("phase")
        except (json.JSONDecodeError, OSError):
            journal_phase = "unreadable"

    _touch_tree(user_data / "recovery-profiles")
    _touch_tree(user_data / "backups")

    second = _launch(binary, user_data, port=port + 1, isolated_home=isolated_home)
    try:
        verdict, events = _classify(second, log_path, port=port + 1, timeout=timeout)
    finally:
        _terminate(second)
        provider.shutdown()

    return {
        "probe": label,
        "kill_after_seconds": kill_after,
        "gateway_answered_before_kill": entered_before_kill,
        "journal_present_after_kill": journal_present,
        "journal_phase_after_kill": journal_phase,
        "relaunch_verdict": verdict,
        "wedged": verdict != "entered",
        "recovery_profiles": len(fixture.recovery_ids),
        "events": [event.get("event") for event in events],
        "user_data": str(user_data),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="print the scenario catalogue as JSON")

    summarize = sub.add_parser("summarize", help="render a report as a Markdown summary")
    summarize.add_argument("--report", type=Path, required=True)
    summarize.add_argument("--title", default="Packaged fault injection")

    run = sub.add_parser("run", help="build a fault environment and launch the packaged app")
    run.add_argument("--scenario", action="append", default=None, help="repeatable; default all")
    run.add_argument("--app", type=Path, default=None, help="path to OpenSquilla.app")
    run.add_argument("--workdir", type=Path, required=True)
    run.add_argument("--port", type=int, default=18931)
    run.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    run.add_argument("--report", type=Path, default=None)
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="build fixtures and validate the catalogue without launching",
    )

    wedge = sub.add_parser(
        "wedge",
        help="interrupt consolidation, disturb the sources, and report whether startup recovers",
    )
    wedge.add_argument("--app", type=Path, required=True)
    wedge.add_argument("--workdir", type=Path, required=True)
    wedge.add_argument("--port", type=int, default=18951)
    wedge.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    wedge.add_argument("--report", type=Path, default=None)
    wedge.add_argument(
        "--kill-after",
        type=float,
        action="append",
        default=None,
        help="seconds before SIGKILL; repeatable (default 1 2 4 8)",
    )

    args = parser.parse_args(argv)

    if args.command == "wedge":
        args.workdir.mkdir(parents=True, exist_ok=True)
        delays = args.kill_after or [1.0, 2.0, 4.0, 8.0]
        probes = []
        for index, delay in enumerate(delays):
            probes.append(
                _run_wedge_probe(
                    app=args.app,
                    workdir=args.workdir,
                    port=args.port + index * 2,
                    kill_after=delay,
                    timeout=args.timeout,
                )
            )
            latest = probes[-1]
            state = "WEDGED" if latest["wedged"] else "recovered"
            print(
                f"[{state}] kill after {delay:g}s -> relaunch {latest['relaunch_verdict']}"
                f" (journal phase {latest['journal_phase_after_kill']})",
                flush=True,
            )
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(probes, indent=2) + "\n", encoding="utf-8")
        wedged = [probe for probe in probes if probe["wedged"]]
        print(f"\n{len(wedged)}/{len(probes)} interruptions left startup blocked", flush=True)
        # Reported, never gating: these paths are known-unfixed, and failing the
        # workflow on them would mask new regressions.
        return 0

    if args.command == "summarize":
        if not args.report.is_file():
            print(f"No report at {args.report}.")
            return 0
        results = json.loads(args.report.read_text(encoding="utf-8"))
        print(f"## {args.title}\n")
        print("| scenario | expected | observed | result |")
        print("| --- | --- | --- | --- |")
        for item in results:
            if item.get("verdict") == "skipped":
                mark = "skipped"
            elif item["ok"]:
                mark = "pass"
            else:
                mark = "**FAIL**"
            print(
                f"| `{item['scenario']}` | {item.get('expected', '-')} "
                f"| {item['verdict']} | {mark} |"
            )
        print("")
        for item in results:
            if item.get("ok") or item.get("verdict") == "skipped":
                continue
            print(f"### `{item['scenario']}`\n")
            print(f"{item['why']}\n")
            for failure in item.get("failures", []):
                print(f"- {failure}")
            print("")
        return 0

    if args.command == "list":
        print(
            json.dumps(
                {
                    name: {"expect": spec["expect"], "why": spec["why"]}
                    for name, spec in SCENARIOS.items()
                },
                indent=2,
            )
        )
        return 0

    if not args.dry_run and args.app is None:
        parser.error("--app is required unless --dry-run is used")

    names = args.scenario or list(SCENARIOS)
    unknown = [name for name in names if name not in SCENARIOS]
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(unknown)}")

    args.workdir.mkdir(parents=True, exist_ok=True)
    results = []
    for index, name in enumerate(names):
        # A distinct port per scenario keeps a lingering gateway from being
        # mistaken for the next scenario's success.
        results.append(
            _run_scenario(
                name,
                app=args.app,
                workdir=args.workdir,
                port=args.port + index,
                timeout=args.timeout,
                dry_run=args.dry_run,
            )
        )
        latest = results[-1]
        if latest["verdict"] == "skipped":
            print(f"[skip] {name}: {latest['skipped_reason']}", flush=True)
            continue
        status = "ok" if latest["ok"] else "FAIL"
        print(f"[{status}] {name}: verdict={latest['verdict']}", flush=True)
        for failure in latest.get("failures", []):
            print(f"       - {failure}", flush=True)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")

    failed = [result for result in results if not result["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} scenarios ok", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
