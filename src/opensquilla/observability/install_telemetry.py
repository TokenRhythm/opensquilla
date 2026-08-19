"""Anonymous installation telemetry.

The telemetry surface is intentionally narrow: it tracks installation instances
and version distribution, not users. The default endpoint points at the official
OpenSquilla collector and can be overridden or disabled by environment variable.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import platform
import re
import socket
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opensquilla import __version__
from opensquilla.observability.network_policy import network_observability_disabled
from opensquilla.paths import default_opensquilla_home

log = logging.getLogger(__name__)

TELEMETRY_SCHEMA_VERSION = 1
TELEMETRY_STATE_FILE = "install_telemetry.json"
TELEMETRY_DISABLED_ENV = "OPENSQUILLA_TELEMETRY_DISABLED"
TELEMETRY_ENDPOINT_ENV = "OPENSQUILLA_TELEMETRY_ENDPOINT"
TELEMETRY_INSTALL_METHOD_ENV = "OPENSQUILLA_INSTALL_METHOD"
TELEMETRY_TESTING_ENV = "OPENSQUILLA_TESTING"

DEFAULT_TELEMETRY_ENDPOINT = "https://telemetry.opensquilla.ai/v1/install"
DEFAULT_TIMEOUT_SECONDS = 2.0

_TRUE_VALUES = {"1", "true", "yes", "on"}
_SUCCESS_STATUS_CODES = {200, 201, 202, 204}
_AUTO_SKIP_ENV_VARS = ("GITHUB_ACTIONS", "PYTEST_CURRENT_TEST", TELEMETRY_TESTING_ENV)
_STABLE_INSTALL_ID_PREFIX = "opensquilla-install-v2"
_STABLE_INSTALL_ID_SOURCES = {"stable-v2-mac", "stable-v2-ip"}
_MAC_HEX_RE = re.compile(r"^[0-9a-f]{12}$")
_COLLECT_LOCK = threading.Lock()
_STATE_LOCK = threading.RLock()
_STATE_TRANSACTION_TIMEOUT_SECONDS = 1.0
_RESULT_STATE_TRANSACTION_TIMEOUT_SECONDS = 5.0
_WINDOWS_STATE_REPLACE_RETRY_DELAYS_SECONDS = (0.02, 0.05, 0.1, 0.2)
_WINDOWS_TRANSIENT_STATE_REPLACE_ERRORS = frozenset({5, 32, 33})


@contextmanager
def _state_transaction(
    path: Path,
    *,
    timeout: float = _STATE_TRANSACTION_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Serialize one short telemetry-state transaction across threads/processes."""
    # Import lazily so merely importing telemetry during boot does not load the
    # platform-specific lock implementation. The exact JSON path is the key, so
    # this never contends with the long-lived profile-home operation lock.
    from opensquilla.profile_operation_lock import ProfileOperationLock

    with _STATE_LOCK:
        with ProfileOperationLock(path, timeout=timeout):
            yield


@dataclass(frozen=True)
class InstallTelemetryResult:
    state_path: Path
    endpoint_configured: bool
    disabled: bool = False
    event: str | None = None
    sent: bool = False
    uploaded: bool = False
    skipped_reason: str | None = None
    error: str | None = None


def collect_install_telemetry(
    *,
    config: Any | None = None,
    state_path: str | Path | None = None,
    version: str | None = None,
) -> InstallTelemetryResult:
    """Collect and upload anonymous installation telemetry if needed.

    This function never raises. It is safe to call during gateway startup.
    """
    path = _state_path(config=config, explicit=state_path)
    endpoint = _endpoint()

    try:
        skip_reason = _telemetry_skip_reason(config=config)
        if skip_reason:
            return InstallTelemetryResult(
                state_path=path,
                endpoint_configured=bool(endpoint),
                disabled=True,
                skipped_reason=skip_reason,
            )

        current_version = (version or __version__ or "unknown").strip() or "unknown"
        # Serialize install/version event selection, but do not hold the state
        # lock across network I/O. Daily usage telemetry shares this state file
        # for the anonymous install id and must remain free to read it.
        with _COLLECT_LOCK:
            with _state_transaction(path):
                state = _load_or_create_state(path)
                event = _next_event(state, current_version)
                if event is None:
                    return InstallTelemetryResult(
                        state_path=path,
                        endpoint_configured=bool(endpoint),
                        skipped_reason="already_uploaded",
                    )

                if not endpoint:
                    state["last_skip_reason"] = "endpoint_empty"
                    _write_state(path, state)
                    return InstallTelemetryResult(
                        state_path=path,
                        endpoint_configured=False,
                        event=event,
                        skipped_reason="endpoint_empty",
                    )

                now = _utc_now()
                payload = _build_payload(
                    state,
                    event=event,
                    current_version=current_version,
                    sent_at=now,
                )
                skip_reason = _telemetry_skip_reason(config=config)
                if skip_reason:
                    return InstallTelemetryResult(
                        state_path=path,
                        endpoint_configured=True,
                        disabled=True,
                        event=event,
                        skipped_reason=skip_reason,
                    )
                state["last_attempt_at"] = now
                state["last_skip_reason"] = None
                # Persist the selected install id before the background network
                # call. A concurrent usage upload will then use the same id.
                _write_state(path, state)

            # The privacy setting is live-editable. Re-check at the last safe
            # point before starting a new request; an already-entered socket
            # operation cannot be recalled, but no later request should begin.
            skip_reason = _telemetry_skip_reason(config=config)
            if skip_reason:
                return InstallTelemetryResult(
                    state_path=path,
                    endpoint_configured=True,
                    disabled=True,
                    event=event,
                    skipped_reason=skip_reason,
                )
            uploaded, error = _post_payload(
                endpoint,
                payload,
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            with _state_transaction(
                path,
                timeout=_RESULT_STATE_TRANSACTION_TIMEOUT_SECONDS,
            ):
                # Merge the result into the latest state rather than overwriting
                # another telemetry writer's atomic update.
                state = _load_or_create_state(path)
                if uploaded:
                    state["last_success_at"] = now
                    state["last_error"] = None
                    state["uploaded_install"] = True
                    _add_uploaded_version(state, current_version)
                else:
                    state["last_error"] = error or "upload_failed"
                _write_state(path, state)

            return InstallTelemetryResult(
                state_path=path,
                endpoint_configured=True,
                event=event,
                sent=True,
                uploaded=uploaded,
                error=error,
            )
    except Exception as exc:  # pragma: no cover - defensive startup guard
        log.debug("Install telemetry skipped: %s", exc, exc_info=True)
        return InstallTelemetryResult(
            state_path=path,
            endpoint_configured=bool(endpoint),
            skipped_reason="error",
            error=str(exc),
        )


def start_background_install_telemetry(
    *,
    config: Any | None = None,
    state_path: str | Path | None = None,
    version: str | None = None,
    on_result: Callable[[InstallTelemetryResult], None] | None = None,
) -> threading.Thread | None:
    """Collect install telemetry in a daemon thread without delaying startup.

    Privacy-disabled calls are resolved synchronously without creating state or
    a thread. The returned thread exists only to let tests observe completion;
    gateway shutdown deliberately never joins this best-effort worker.
    """

    def _notify_result(result: InstallTelemetryResult) -> None:
        if on_result is None:
            return
        try:
            on_result(result)
        except Exception:  # pragma: no cover - caller diagnostic guard
            log.debug("Install telemetry result callback failed", exc_info=True)

    if _telemetry_skip_reason(config=config):
        result = collect_install_telemetry(
            config=config,
            state_path=state_path,
            version=version,
        )
        _notify_result(result)
        return None

    def _run() -> None:
        result = collect_install_telemetry(
            config=config,
            state_path=state_path,
            version=version,
        )
        _notify_result(result)

    try:
        thread = threading.Thread(
            target=_run,
            name="opensquilla-install-telemetry",
            daemon=True,
        )
        thread.start()
        return thread
    except Exception:  # pragma: no cover - thread spawn failure
        log.debug("Could not start install telemetry thread", exc_info=True)
        return None


def ensure_install_telemetry_id(
    *,
    config: Any | None = None,
    state_path: str | Path | None = None,
) -> str:
    """Return and durably persist the shared anonymous install id."""
    path = _state_path(config=config, explicit=state_path)
    with _state_transaction(path):
        state = _load_or_create_state(path)
        _write_state(path, state)
        return str(state["install_id"])


def _state_path(*, config: Any | None, explicit: str | Path | None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    configured_state_dir = getattr(config, "state_dir", None)
    if isinstance(configured_state_dir, str) and configured_state_dir.strip():
        root = Path(configured_state_dir.strip()).expanduser()
    else:
        root = default_opensquilla_home() / "state"
    return root / TELEMETRY_STATE_FILE


def _telemetry_disabled(*, config: Any | None = None) -> bool:
    return network_observability_disabled(config=config)


def _telemetry_skip_reason(*, config: Any | None = None) -> str | None:
    if _telemetry_disabled(config=config):
        return "disabled"
    ci_env = _ci_environment_name()
    if ci_env is not None:
        return f"environment:{ci_env}"
    return None


def _ci_environment_detected() -> bool:
    return _ci_environment_name() is not None


def _ci_environment_name() -> str | None:
    for name in _AUTO_SKIP_ENV_VARS:
        value = os.environ.get(name, "")
        if name == "PYTEST_CURRENT_TEST":
            if value.strip():
                return name
            continue
        if value.strip().lower() in _TRUE_VALUES:
            return name
    return None


def _endpoint() -> str:
    return os.environ.get(TELEMETRY_ENDPOINT_ENV, DEFAULT_TELEMETRY_ENDPOINT).strip()


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_or_create_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return _normalize_state(data)
        except (json.JSONDecodeError, OSError):
            log.debug("Install telemetry state unreadable; replacing", exc_info=True)
    return _new_state()


def _new_state() -> dict[str, Any]:
    now = _utc_now()
    install_id, install_id_source = _resolve_install_id(existing_install_id=None)
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "install_id": install_id,
        "install_id_source": install_id_source,
        "first_seen_at": now,
        "uploaded_install": False,
        "uploaded_versions": [],
        "last_attempt_at": None,
        "last_success_at": None,
        "last_error": None,
        "last_skip_reason": None,
    }


def _normalize_state(data: dict[str, Any]) -> dict[str, Any]:
    state = dict(data)
    install_id = _valid_install_id(state.get("install_id"))
    install_id_source = state.get("install_id_source")
    if isinstance(install_id_source, str) and install_id_source in _STABLE_INSTALL_ID_SOURCES:
        if install_id is None:
            install_id, install_id_source = _resolve_install_id(existing_install_id=None)
    else:
        install_id, install_id_source = _resolve_install_id(existing_install_id=install_id)
    state["install_id"] = install_id
    state["install_id_source"] = install_id_source
    first_seen = state.get("first_seen_at")
    if not isinstance(first_seen, str) or not first_seen.strip():
        state["first_seen_at"] = _utc_now()
    uploaded_versions = state.get("uploaded_versions")
    if not isinstance(uploaded_versions, list):
        uploaded_versions = []
    state["uploaded_versions"] = [
        str(item) for item in uploaded_versions if isinstance(item, str) and item.strip()
    ]
    state["schema_version"] = TELEMETRY_SCHEMA_VERSION
    state["uploaded_install"] = bool(state.get("uploaded_install"))
    state.setdefault("last_attempt_at", None)
    state.setdefault("last_success_at", None)
    state.setdefault("last_error", None)
    state.setdefault("last_skip_reason", None)
    return state


def _resolve_install_id(existing_install_id: str | None) -> tuple[str, str]:
    mac_addresses = _normalized_mac_addresses(_collect_mac_address_candidates())
    if mac_addresses:
        return _stable_install_id("mac", mac_addresses), "stable-v2-mac"

    ip_addresses = _normalized_ip_addresses(_collect_ip_address_candidates())
    if ip_addresses:
        return _stable_install_id("ip", ip_addresses), "stable-v2-ip"

    if existing_install_id:
        return existing_install_id, "random-persisted"
    return str(uuid.uuid4()), "random-persisted"


def _valid_install_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _stable_install_id(source: str, values: list[str]) -> str:
    stable_input = f"{_STABLE_INSTALL_ID_PREFIX}|{source}|{','.join(values)}"
    return hashlib.sha256(stable_input.encode("utf-8")).hexdigest()


def _collect_mac_address_candidates() -> list[str]:
    candidates: list[str] = []
    sys_class_net = Path("/sys/class/net")
    try:
        for address_path in sys_class_net.glob("*/address"):
            try:
                candidates.append(address_path.read_text(encoding="utf-8").strip())
            except OSError:
                continue
    except OSError:
        pass

    try:
        candidates.append(f"{uuid.getnode():012x}")
    except Exception:
        pass

    return candidates


def _normalized_mac_addresses(candidates: list[str]) -> list[str]:
    addresses: set[str] = set()
    for candidate in candidates:
        normalized = _normalize_mac_address(candidate)
        if normalized is not None:
            addresses.add(normalized)
    return sorted(addresses)


def _normalize_mac_address(value: object) -> str | None:
    text = re.sub(r"[^0-9a-fA-F]", "", str(value or "")).lower()
    if not _MAC_HEX_RE.match(text):
        return None
    if text in {"000000000000", "ffffffffffff"}:
        return None
    first_octet = int(text[:2], 16)
    if first_octet & 1:
        return None
    return text


def _collect_ip_address_candidates() -> list[str]:
    candidates: list[str] = []
    hosts = {socket.gethostname(), socket.getfqdn()}
    for host in hosts:
        if not host:
            continue
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_DGRAM)
        except OSError:
            continue
        for info in infos:
            sockaddr = info[4]
            if sockaddr:
                candidates.append(str(sockaddr[0]))

    udp_targets = (
        (socket.AF_INET, ("8.8.8.8", 80)),
        (socket.AF_INET6, ("2001:4860:4860::8888", 80)),
    )
    for family, target in udp_targets:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.connect(target)
                candidates.append(str(sock.getsockname()[0]))
        except OSError:
            continue

    return candidates


def _normalized_ip_addresses(candidates: list[str]) -> list[str]:
    addresses: set[str] = set()
    for candidate in candidates:
        normalized = _normalize_ip_address(candidate)
        if normalized is not None:
            addresses.add(normalized)
    return sorted(addresses)


def _normalize_ip_address(value: object) -> str | None:
    text = str(value or "").strip()
    if "%" in text:
        text = text.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    if ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip.is_multicast:
        return None
    return str(ip)


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp_name, 0o600)
        _replace_state_file(tmp_name, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _replace_state_file(source: str | Path, destination: Path) -> None:
    """Publish telemetry state after transient Windows readers release it."""
    for delay in (*_WINDOWS_STATE_REPLACE_RETRY_DELAYS_SECONDS, None):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            if (
                os.name != "nt"
                or getattr(exc, "winerror", None)
                not in _WINDOWS_TRANSIENT_STATE_REPLACE_ERRORS
                or delay is None
            ):
                raise
            time.sleep(delay)


def _next_event(state: dict[str, Any], current_version: str) -> str | None:
    if not state.get("uploaded_install", False):
        return "install"
    uploaded_versions = set(state.get("uploaded_versions") or [])
    if current_version not in uploaded_versions:
        return "version_seen"
    return None


def _add_uploaded_version(state: dict[str, Any], current_version: str) -> None:
    versions = list(state.get("uploaded_versions") or [])
    if current_version not in versions:
        versions.append(current_version)
    state["uploaded_versions"] = versions


def _build_payload(
    state: dict[str, Any],
    *,
    event: str,
    current_version: str,
    sent_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "event": event,
        "install_id": state["install_id"],
        "opensquilla_version": current_version,
        "install_method": _detect_install_method(),
        "os": _safe_str(platform.system()),
        "os_version": _safe_str(platform.release()),
        "architecture": _safe_str(platform.machine()),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "first_seen_at": state["first_seen_at"],
        "sent_at": sent_at,
        "ci_environment": _ci_environment_detected(),
    }


def _safe_str(value: object) -> str:
    text = str(value or "").strip()
    return text or "unknown"


def _detect_install_method() -> str:
    explicit = os.environ.get(TELEMETRY_INSTALL_METHOD_ENV, "").strip().lower()
    if explicit in {"pip", "source", "docker", "desktop", "unknown"}:
        return explicit
    if os.environ.get("OPENSQUILLA_DESKTOP", "").strip().lower() in _TRUE_VALUES:
        return "desktop"
    if os.environ.get("OPENSQUILLA_RUNNING_IN_CONTAINER", "").strip().lower() in _TRUE_VALUES:
        return "docker"
    if Path("/.dockerenv").exists():
        return "docker"
    source_root = Path(__file__).resolve().parents[3]
    if (source_root / ".git").exists():
        return "source"
    return "pip"


def _post_payload(
    endpoint: str,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> tuple[bool, str | None]:
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            response = client.post(endpoint, json=payload)
        if response.status_code in _SUCCESS_STATUS_CODES:
            return True, None
        return False, f"http_status_{response.status_code}"
    except Exception as exc:
        log.debug("Install telemetry upload failed: %s", exc)
        return False, str(exc)
