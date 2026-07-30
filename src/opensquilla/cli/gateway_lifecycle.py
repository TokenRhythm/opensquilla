"""Local process lifecycle helpers for ``opensquilla gateway``."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from opensquilla.cli.url_utils import normalize_gateway_url
from opensquilla.paths import default_opensquilla_home, state_dir

UNMANAGED_GATEWAY_RUNNING = "UNMANAGED_GATEWAY_RUNNING"
MANAGED_GATEWAY_TARGET_MISMATCH = "MANAGED_GATEWAY_TARGET_MISMATCH"
REMOTE_GATEWAY_UNAVAILABLE = "REMOTE_GATEWAY_UNAVAILABLE"
DESKTOP_PROFILE_RECOVERY_REQUIRED = "DESKTOP_PROFILE_RECOVERY_REQUIRED"
DESKTOP_CONFIG_OUTSIDE_PROFILE = "desktop_config_outside_profile"

_DESKTOP_PROFILE_KINDS = frozenset({"desktop-primary", "desktop-recovery"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def desktop_profile_lifecycle_active() -> bool:
    """Return whether lifecycle bookkeeping belongs to a Desktop profile."""

    profile_kind = os.environ.get("OPENSQUILLA_PROFILE_KIND", "").strip().lower()
    if profile_kind:
        return profile_kind in _DESKTOP_PROFILE_KINDS
    return os.environ.get("OPENSQUILLA_DESKTOP", "").strip().lower() in _TRUTHY


def desktop_config_path_is_profile_local(config_path: str | None = None) -> bool:
    """Desktop may only boot from the selected profile's canonical config."""

    if not desktop_profile_lifecycle_active():
        return True
    requested = config_path or os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH", "").strip()
    if not requested:
        return True
    candidate = Path(requested).expanduser()
    if not candidate.is_absolute():
        candidate = candidate.absolute()
    expected = default_opensquilla_home().expanduser().absolute() / "config.toml"
    return os.path.normcase(os.path.normpath(str(candidate))) == os.path.normcase(
        os.path.normpath(str(expected))
    )


def _desktop_lifecycle_root() -> Path | None:
    if not desktop_profile_lifecycle_active():
        return None
    from opensquilla.recovery.locking import profile_lock_key, user_state_dir

    home = default_opensquilla_home()
    return user_state_dir() / "OpenSquilla" / "gateway-lifecycle" / profile_lock_key(home)


def gateway_pidfile_path() -> Path:
    desktop_root = _desktop_lifecycle_root()
    if desktop_root is not None:
        return desktop_root / "gateway.json"
    return state_dir("gateway", "gateway.json")


def gateway_log_path() -> Path:
    desktop_root = _desktop_lifecycle_root()
    if desktop_root is not None:
        return desktop_root / "gateway.log"
    return default_opensquilla_home() / "logs" / "gateway.log"


def _running_on_windows() -> bool:
    """Indirection over ``os.name`` so the kill path is easy to exercise in tests."""
    return os.name == "nt"


# Short bound on how long to wait for a process to disappear after a *hard*
# terminate (TerminateProcess / SIGKILL), which is near-instant — distinct from
# the graceful ``shutdown_timeout`` budget that precedes it. Reusing the full
# graceful budget here would double the worst-case stop time.
_HARD_KILL_BACKSTOP_S = 5.0


@dataclass
class GatewayLifecycleResult:
    action: str
    state: str
    ok: bool = True
    pid: int | None = None
    host: str = "127.0.0.1"
    probe_host: str | None = None
    port: int = 18791
    managed: bool = False
    code: str | None = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    pidfile: str = ""
    log_path: str = ""
    started_at: str | None = None
    exit_code_value: int = 0
    remote: bool = False
    gateway_url: str | None = None
    url_override: str | None = None
    health_url_override: str | None = None

    @property
    def url(self) -> str:
        if self.url_override:
            return self.url_override
        return _http_url(self.host, self.port)

    @property
    def health_url(self) -> str:
        if self.health_url_override:
            return self.health_url_override
        return f"{_http_url(self.probe_host or self.host, self.port)}/health"

    @property
    def exit_code(self) -> int:
        if self.ok:
            return 0
        if self.code == UNMANAGED_GATEWAY_RUNNING:
            return 3
        return self.exit_code_value or 1

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "action": self.action,
            "state": self.state,
            "host": self.host,
            "port": self.port,
            "url": self.url,
            "healthUrl": self.health_url,
            "managed": self.managed,
            "pidfile": self.pidfile,
            "logPath": self.log_path,
        }
        if self.remote:
            payload["remote"] = True
        if self.gateway_url:
            payload["gatewayUrl"] = self.gateway_url
        if self.probe_host and self.probe_host != self.host:
            payload["probeHost"] = self.probe_host
        if self.pid is not None:
            payload["pid"] = self.pid
        if self.started_at:
            payload["startedAt"] = self.started_at
        if self.message:
            payload["message"] = self.message
        if self.code:
            payload["code"] = self.code
        if self.details:
            payload["details"] = self.details
        elif not self.ok:
            payload["details"] = {}
        return payload


class GatewayLifecycleManager:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 18791,
        config_path: str | None = None,
        health_timeout: float = 60.0,
        shutdown_timeout: float = 10.0,
        poll_interval: float = 0.2,
    ) -> None:
        self.host = host
        self.probe_host = _health_probe_host(host)
        self.port = port
        self.config_path = str(config_path) if config_path else None
        self.health_timeout = health_timeout
        self.shutdown_timeout = shutdown_timeout
        self.poll_interval = poll_interval
        self.pidfile = gateway_pidfile_path()
        self.log_path = gateway_log_path()

    def status(self) -> GatewayLifecycleResult:
        record, error = self._read_pidfile()
        if error is not None:
            return self._result(
                "status",
                "stale",
                managed=False,
                message="Gateway pidfile is unreadable.",
                details={"error": error},
            )

        if record is None:
            if self._probe_health():
                return self._unmanaged_result("status", ok=True)
            return self._result("status", "not_started", managed=False)

        if not self._record_matches_target(record):
            pid = self._record_pid(record)
            if pid is not None and self._pid_running(pid):
                return self._target_mismatch_result(
                    "status",
                    ok=True,
                    pid=pid,
                    record=record,
                )
            if self._probe_health():
                return self._unmanaged_result("status", ok=True)
            return self._result(
                "status",
                "stale",
                managed=False,
                details={"reason": "pidfile_target_mismatch"},
            )

        pid = self._record_pid(record)
        if pid is None or not self._pid_running(pid):
            if self._probe_health():
                return self._unmanaged_result("status", ok=True)
            return self._result(
                "status",
                "stale",
                pid=pid,
                managed=False,
                started_at=self._record_started_at(record),
            )

        if self._probe_health():
            return self._result(
                "status",
                "running",
                pid=pid,
                managed=True,
                started_at=self._record_started_at(record),
            )

        return self._result(
            "status",
            "unhealthy",
            pid=pid,
            managed=True,
            started_at=self._record_started_at(record),
        )

    def start(self) -> GatewayLifecycleResult:
        blocked = self._desktop_profile_preflight("start")
        if blocked is not None:
            return blocked
        current = self.status()
        if current.state == "running" and current.managed:
            current.action = "start"
            current.message = "Gateway is already running."
            return current
        if current.state == "unmanaged":
            return self._unmanaged_result("start", ok=False)
        if current.state == "target_mismatch":
            return self._target_mismatch_result(
                "start",
                ok=False,
                pid=current.pid,
                record=current.details,
            )
        if current.state == "unhealthy" and current.managed:
            return self._result(
                "start",
                "start_failed",
                ok=False,
                pid=current.pid,
                managed=True,
                code="RECORDED_GATEWAY_UNHEALTHY",
                message="Recorded gateway process is running but health check failed.",
                exit_code_value=1,
            )
        if current.state == "stale":
            self._remove_pidfile()

        argv = self._gateway_run_argv()
        started_at = self._now()
        try:
            process = self._spawn_gateway(argv)
        except OSError as exc:
            return self._result(
                "start",
                "start_failed",
                ok=False,
                code="SPAWN_FAILED",
                message=str(exc),
                exit_code_value=1,
            )

        record = self._record(process.pid, argv, started_at)
        self._write_pidfile(record)
        if self._wait_for_health():
            return self._result(
                "start",
                "running",
                pid=process.pid,
                managed=True,
                started_at=started_at,
                message="Gateway started.",
            )

        self._terminate_pid(process.pid)
        self._remove_pidfile()
        return self._result(
            "start",
            "start_failed",
            ok=False,
            pid=process.pid,
            managed=True,
            code="HEALTH_TIMEOUT",
            message="Gateway did not become ready before the timeout.",
            exit_code_value=1,
        )

    def stop(self) -> GatewayLifecycleResult:
        current = self.status()
        if current.state == "not_started":
            return self._result("stop", "stopped", managed=False, message="Gateway is not running.")
        if current.state == "unmanaged":
            return self._unmanaged_result("stop", ok=False)
        if current.state == "target_mismatch":
            return self._target_mismatch_result(
                "stop",
                ok=False,
                pid=current.pid,
                record=current.details,
            )
        if current.state == "stale":
            self._remove_pidfile()
            return self._result("stop", "cleared_stale", managed=False)
        if current.pid is None:
            return self._result(
                "stop",
                "stop_failed",
                ok=False,
                code="PID_MISSING",
                message="Recorded gateway pid is missing.",
                exit_code_value=1,
            )

        if not self._terminate_pid(current.pid):
            return self._result(
                "stop",
                "stop_failed",
                ok=False,
                pid=current.pid,
                managed=True,
                code="TERMINATE_FAILED",
                message="Gateway process did not stop before the timeout.",
                exit_code_value=1,
            )

        self._remove_pidfile()
        return self._result(
            "stop",
            "stopped",
            pid=current.pid,
            managed=True,
            message="Gateway stopped.",
        )

    def restart(self) -> GatewayLifecycleResult:
        blocked = self._desktop_profile_preflight("restart")
        if blocked is not None:
            return blocked
        stopped = self.stop()
        if stopped.exit_code != 0:
            return self._result(
                "restart",
                stopped.state,
                ok=False,
                pid=stopped.pid,
                managed=stopped.managed,
                code=stopped.code,
                message=stopped.message,
                details={"stop": stopped.to_payload()},
                exit_code_value=stopped.exit_code,
            )

        started = self.start()
        started.action = "restart"
        started.details = {**started.details, "stop": stopped.to_payload()}
        return started

    def _desktop_profile_preflight(
        self,
        action: str,
    ) -> GatewayLifecycleResult | None:
        """Reject unsafe Desktop profiles before parent lifecycle bookkeeping writes."""

        if not desktop_profile_lifecycle_active():
            return None

        if not desktop_config_path_is_profile_local(self.config_path):
            return self._result(
                action,
                "recovery_required",
                ok=False,
                code=DESKTOP_PROFILE_RECOVERY_REQUIRED,
                message=(
                    "Desktop gateway refused a config outside the primary profile. "
                    "Repair the primary profile through Desktop before retrying."
                ),
                details={
                    "stableCode": DESKTOP_CONFIG_OUTSIDE_PROFILE,
                    "outcome": "recovery_required",
                    "allowedActions": ["retry-primary"],
                },
                exit_code_value=1,
            )

        from opensquilla.recovery import RecoveryRequiredError, guard_desktop_profile

        try:
            guard_desktop_profile()
        except RecoveryRequiredError as exc:
            report = exc.report
            return self._result(
                action,
                "recovery_required",
                ok=False,
                code=DESKTOP_PROFILE_RECOVERY_REQUIRED,
                message=(
                    "Desktop profile requires offline recovery before the gateway "
                    f"can be {action}ed ({report.stable_code})."
                ),
                details={
                    "stableCode": report.stable_code,
                    "outcome": report.outcome,
                    "allowedActions": list(report.allowed_actions),
                },
                exit_code_value=1,
            )
        return None

    def _result(
        self,
        action: str,
        state: str,
        *,
        ok: bool = True,
        pid: int | None = None,
        managed: bool = False,
        code: str | None = None,
        message: str = "",
        details: dict[str, Any] | None = None,
        started_at: str | None = None,
        exit_code_value: int = 0,
    ) -> GatewayLifecycleResult:
        return GatewayLifecycleResult(
            action=action,
            state=state,
            ok=ok,
            pid=pid,
            host=self.host,
            probe_host=self.probe_host,
            port=self.port,
            managed=managed,
            code=code,
            message=message,
            details=details or {},
            pidfile=str(self.pidfile),
            log_path=str(self.log_path),
            started_at=started_at,
            exit_code_value=exit_code_value,
        )

    def _unmanaged_result(self, action: str, *, ok: bool) -> GatewayLifecycleResult:
        return self._result(
            action,
            "unmanaged",
            ok=ok,
            managed=False,
            code=None if ok else UNMANAGED_GATEWAY_RUNNING,
            message=(
                f"A healthy gateway is already running at {_where(self.host, self.port)}, "
                "but OpenSquilla does not own it. "
                "Use that URL to talk to the existing gateway, or stop it first."
            ),
            exit_code_value=3,
        )

    def _target_mismatch_result(
        self,
        action: str,
        *,
        ok: bool,
        pid: int | None,
        record: dict[str, Any],
    ) -> GatewayLifecycleResult:
        recorded_host = record.get("host") or record.get("recordedHost")
        recorded_port = record.get("port") or record.get("recordedPort")
        details = {
            "recordedHost": recorded_host,
            "recordedPort": recorded_port,
            "requestedHost": self.host,
            "requestedPort": self.port,
        }
        if self.config_path or record.get("configPath"):
            details["recordedConfigPath"] = record.get("configPath")
            details["requestedConfigPath"] = self.config_path
        return self._result(
            action,
            "target_mismatch",
            ok=ok,
            pid=pid,
            managed=True,
            code=None if ok else MANAGED_GATEWAY_TARGET_MISMATCH,
            message=(
                f"A managed gateway is recorded at {_where(recorded_host, recorded_port)}, "
                f"but this target is {_where(self.host, self.port)}. "
                "Refusing to mutate the recorded gateway from this target. "
                "Use the recorded target, or stop and restart the gateway on the new one."
            ),
            details=details,
            exit_code_value=3,
        )

    def _read_pidfile(self) -> tuple[dict[str, Any] | None, str | None]:
        if not self.pidfile.exists():
            return None, None
        try:
            payload = json.loads(self.pidfile.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, str(exc)
        if not isinstance(payload, dict):
            return None, "pidfile payload is not an object"
        return payload, None

    def _write_pidfile(self, record: dict[str, Any]) -> None:
        self.pidfile.parent.mkdir(
            mode=0o700 if desktop_profile_lifecycle_active() else 0o777,
            parents=True,
            exist_ok=True,
        )
        self.pidfile.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")

    def _remove_pidfile(self) -> None:
        try:
            self.pidfile.unlink()
        except FileNotFoundError:
            pass

    def _record(self, pid: int, argv: list[str], started_at: str) -> dict[str, Any]:
        record: dict[str, Any] = {
            "pid": pid,
            "host": self.host,
            "port": self.port,
            "url": _http_url(self.host, self.port),
            "healthUrl": f"{_http_url(self.probe_host, self.port)}/health",
            "logPath": str(self.log_path),
            "startedAt": started_at,
            "argv": argv,
        }
        if self.probe_host != self.host:
            record["probeHost"] = self.probe_host
        if self.config_path:
            record["configPath"] = self.config_path
        return record

    def _record_matches_target(self, record: dict[str, Any]) -> bool:
        try:
            record_port = int(record.get("port", -1))
        except (TypeError, ValueError):
            return False
        if record.get("host") != self.host or record_port != self.port:
            return False
        record_config_path = record.get("configPath")
        if self.config_path is not None and record_config_path:
            return bool(record_config_path == self.config_path)
        return True

    def _record_pid(self, record: dict[str, Any]) -> int | None:
        value = record.get("pid")
        if value is None:
            return None
        try:
            pid = int(value)
        except (TypeError, ValueError):
            return None
        return pid if pid > 0 else None

    def _record_started_at(self, record: dict[str, Any]) -> str | None:
        value = record.get("startedAt")
        return value if isinstance(value, str) else None

    def _gateway_run_argv(self) -> list[str]:
        # A PyInstaller-frozen build (the desktop bundle) has sys.executable
        # already pointing at the CLI entrypoint, so "-m opensquilla.cli.main"
        # would be passed through as arguments and the child would exit on a
        # usage error. Invoke the subcommand directly there.
        if getattr(sys, "frozen", False):
            argv = [sys.executable, "gateway", "run"]
        else:
            argv = [sys.executable, "-m", "opensquilla.cli.main", "gateway", "run"]
        argv += ["--listen", self.host, "--port", str(self.port)]
        if self.config_path:
            argv.extend(["--config", self.config_path])
        return argv

    def _spawn_gateway(self, argv: list[str]) -> subprocess.Popen[Any]:
        self.log_path.parent.mkdir(
            mode=0o700 if desktop_profile_lifecycle_active() else 0o777,
            parents=True,
            exist_ok=True,
        )
        env = os.environ.copy()
        if self.config_path:
            env["OPENSQUILLA_GATEWAY_CONFIG_PATH"] = self.config_path

        log = self.log_path.open("ab")
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            process = subprocess.Popen(  # noqa: S603 - argv is constructed internally.
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                shell=False,
                start_new_session=os.name != "nt",
                creationflags=creationflags,
            )
        finally:
            log.close()
        return process

    def _probe_health(self) -> bool:
        for path in ("health", "healthz"):
            request = Request(f"{_http_url(self.probe_host, self.port)}/{path}", method="GET")
            try:
                with urlopen(request, timeout=0.5) as response:  # noqa: S310 - local health probe.
                    if 200 <= int(response.status) < 300:
                        return True
            except (HTTPError, OSError, URLError, TimeoutError):
                continue
        return False

    def _probe_ready(self) -> bool:
        saw_ready_endpoint = False
        for path in ("ready", "readyz"):
            request = Request(f"{_http_url(self.probe_host, self.port)}/{path}", method="GET")
            try:
                with urlopen(request, timeout=0.5) as response:  # noqa: S310 - local readiness probe.
                    saw_ready_endpoint = True
                    if 200 <= int(response.status) < 300:
                        return True
            except HTTPError as exc:
                if int(getattr(exc, "code", 0)) != 404:
                    saw_ready_endpoint = True
                continue
            except (OSError, URLError, TimeoutError):
                continue
        if saw_ready_endpoint:
            return False
        return self._probe_health()

    def _wait_for_health(self) -> bool:
        deadline = time.monotonic() + max(self.health_timeout, 0.0)
        while time.monotonic() <= deadline:
            if self._probe_ready():
                return True
            time.sleep(self.poll_interval)
        return False

    def _pid_running(self, pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            return _windows_pid_running(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _request_graceful_shutdown(self) -> bool:
        """POST the owner-only graceful shutdown endpoint.

        Returns True if the gateway accepted the request (2xx). The endpoint runs
        the full ``GatewayServer.close()`` drain before exiting, so the caller
        should then poll for process exit rather than killing immediately. Used
        on Windows, where ``os.kill`` cannot deliver a drain-triggering SIGTERM.

        The endpoint is owner-gated (loopback-proven, matching elevated mode), so
        this returns False for a gateway bound to a non-loopback host (e.g.
        ``0.0.0.0``): the request gets 403 and the caller falls back to a hard
        terminate. Graceful drain on Windows therefore requires a loopback bind,
        which the desktop and the default CLI both use.
        """
        url = f"{_http_url(self.probe_host, self.port)}/api/system/shutdown"
        headers = {"Content-Type": "application/json"}
        try:
            from opensquilla.cli.gateway_rpc import default_gateway_token

            token = default_gateway_token(self.config_path)
        except Exception:  # noqa: BLE001 — token resolution must never block stop
            token = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(url, data=b"{}", headers=headers, method="POST")
        try:
            with urlopen(request, timeout=2.0) as response:  # noqa: S310 - local shutdown.
                return 200 <= int(response.status) < 300
        except HTTPError as exc:
            return 200 <= int(getattr(exc, "code", 0)) < 300
        except (OSError, URLError, TimeoutError):
            return False

    def _wait_for_exit(self, pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + max(timeout, 0.0)
        while time.monotonic() <= deadline:
            if not self._pid_running(pid):
                return True
            time.sleep(self.poll_interval)
        return not self._pid_running(pid)

    def _hard_kill(self, pid: int) -> bool:
        sigkill = getattr(signal, "SIGKILL", None)
        if sigkill is not None and not _running_on_windows():
            try:
                os.kill(pid, sigkill)
            except OSError:
                pass
            return not self._pid_running(pid)
        # Windows has no SIGKILL; os.kill maps SIGTERM to TerminateProcess.
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        return self._wait_for_exit(pid, _HARD_KILL_BACKSTOP_S)

    def _terminate_pid(self, pid: int) -> bool:
        if not self._pid_running(pid):
            return True

        if _running_on_windows():
            # Windows os.kill cannot deliver a drain-triggering SIGTERM (it maps
            # to an immediate TerminateProcess). Prefer the gateway's owner-only
            # HTTP shutdown endpoint, which runs the full close() drain, and fall
            # back to a hard terminate only if it does not exit in time.
            if self._request_graceful_shutdown() and self._wait_for_exit(
                pid, self.shutdown_timeout
            ):
                return True
            return self._hard_kill(pid)

        # POSIX: SIGTERM triggers the gateway's asyncio shutdown drain.
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except OSError:
            return False

        if self._wait_for_exit(pid, self.shutdown_timeout):
            return True
        return self._hard_kill(pid)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _health_probe_host(host: str) -> str:
    if host == "0.0.0.0":
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def _http_url(host: str, port: int) -> str:
    return f"http://{_format_url_host(host)}:{port}"


def _where(host: str | None, port: int | None) -> str:
    """One-line "url (host=X, port=Y)" used in human error messages.

    Tolerates None inputs (old pidfiles that did not record host/port)
    by falling back to "<unknown>". Module-level (not a method on
    GatewayLifecycleManager) so it can format arbitrary host/port pairs
    such as the recorded target in target_mismatch.
    """
    if host is None or port is None:
        return f"<unknown> (host={host}, port={port})"
    return f"{_http_url(host, port)} (host={host}, port={port})"


def remote_gateway_status(gateway_url: str, *, timeout: float = 0.5) -> GatewayLifecycleResult:
    normalized = normalize_gateway_url(gateway_url)
    base_url = _gateway_http_base_url(normalized)
    attempts: list[dict[str, Any]] = []

    for path in ("health", "healthz"):
        health_url = f"{base_url}/{path}"
        request = Request(health_url, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-provided gateway URL.
                status = int(response.status)
                if 200 <= status < 300:
                    return GatewayLifecycleResult(
                        action="status",
                        state="running",
                        ok=True,
                        managed=False,
                        remote=True,
                        gateway_url=normalized,
                        url_override=base_url,
                        health_url_override=health_url,
                        details={"status": status},
                    )
                attempts.append({"url": health_url, "status": status})
        except HTTPError as exc:
            attempts.append({"url": health_url, "status": int(exc.code)})
        except (OSError, URLError, TimeoutError) as exc:
            attempts.append(
                {
                    "url": health_url,
                    "errorType": type(exc).__name__,
                    "error": str(exc),
                }
            )

    return GatewayLifecycleResult(
        action="status",
        state="unavailable",
        ok=False,
        managed=False,
        remote=True,
        code=REMOTE_GATEWAY_UNAVAILABLE,
        message="Remote gateway is unavailable.",
        details={"attempts": attempts},
        exit_code_value=1,
        gateway_url=normalized,
        url_override=base_url,
        health_url_override=f"{base_url}/health",
    )


def _gateway_http_base_url(normalized_gateway_url: str) -> str:
    parsed = urlparse(normalized_gateway_url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    return urlunparse((scheme, parsed.netloc, "", "", "", ""))


def _format_url_host(host: str) -> str:
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        return f"[{host}]"
    return host


def _windows_pid_running(pid: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False

    process_query_limited_information = 0x1000
    still_active = 259
    ctypes_mod = cast(Any, ctypes)
    kernel32 = ctypes_mod.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return int(exit_code.value) == still_active
    finally:
        kernel32.CloseHandle(handle)
