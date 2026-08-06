"""Diagnostics support bundle: one redacted zip a user can attach to a bug report.

Pure disk reader — never requires a running gateway, never mutates state
(config is read with tomllib directly, NOT via GatewayConfig.load, whose
migration path rewrites the user's config file; the doctor collector runs
against a throwaway temp copy of the config for the same reason). Best-effort
per artifact:
a missing file or unreadable DB becomes a manifest ``collection_errors``
entry, never a failed bundle. Every text artifact passes ``scrub_text``.

Excluded always: desktop-credential.json, .env files, raw decision mirrors.
Excluded at the default tier: turn-calls-*.jsonl (raw prompt/response capture)
— included only with ``include_content=True``.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sqlite3
import sys
import tempfile
import tomllib
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote

import structlog

from opensquilla import __version__
from opensquilla.observability.redact import scrub_text
from opensquilla.observability.turn_call_log import LOG_DIR_ENV
from opensquilla.paths import default_opensquilla_home

log = structlog.get_logger(__name__)

_TAIL_CAP = 5_000_000
_DAY_MS = 24 * 60 * 60 * 1000
_DAY_FILE_RE = re.compile(r"-(\d{8})\.jsonl$")

_Attempt = Callable[[str, Callable[[], None]], None]


@dataclass
class BundleResult:
    path: Path
    manifest: dict[str, Any] = field(default_factory=dict)


def _is_excluded(entry_name: str) -> bool:
    """Hard exclusion list — checked at write time as a belt-and-braces guard."""
    base = entry_name.rsplit("/", 1)[-1]
    return (
        base == "desktop-credential.json"
        or base == ".env"
        or base.startswith(".env.")
        or base.endswith("-raw.jsonl")
    )


def _write_text(archive: zipfile.ZipFile, entry_name: str, text: str) -> None:
    """Scrub and write one text artifact; refuse hard-excluded names."""
    if _is_excluded(entry_name):
        raise ValueError(f"refusing to bundle excluded artifact: {entry_name}")
    archive.writestr(entry_name, scrub_text(text))


def _tail_bytes(path: Path, cap: int = _TAIL_CAP) -> tuple[bytes, bool]:
    """Read at most the last *cap* bytes; return (data, truncated).

    When capped, the seek boundary usually bisects a line, and ``scrub_text``
    can only recognize a secret in a whole ``key=value`` line — a decapitated
    value fragment would sail through unmasked. Drop everything through the
    first newline so the tail always starts on a line boundary, then prefix
    the truncation marker.
    """
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size <= cap:
            return fh.read(), False
        fh.seek(size - cap)
        data = fh.read(cap)
    # Drop the partial first line (all of it, when no newline exists at all).
    _partial, _sep, data = data.partition(b"\n")
    return f"[truncated: showing last {cap} bytes]\n".encode() + data, True


def _add_tail(
    archive: zipfile.ZipFile,
    entry_name: str,
    path: Path,
    truncations: list[dict[str, Any]],
    cap: int = _TAIL_CAP,
) -> None:
    """Write a tail-capped text file, recording a truncation when capped."""
    data, truncated = _tail_bytes(path, cap)
    if truncated:
        truncations.append({"entry": entry_name, "source": str(path), "cap_bytes": cap})
    _write_text(archive, entry_name, data.decode("utf-8", errors="replace"))


def _recent_day_files(directory: Path, prefix: str, days: int) -> list[Path]:
    """Daily ``{prefix}-YYYYMMDD.jsonl`` files whose UTC stamp is within *days*."""
    if not directory.is_dir():
        return []
    today = datetime.now(UTC).date()
    selected: list[Path] = []
    for path in sorted(directory.glob(f"{prefix}-*.jsonl")):
        match = _DAY_FILE_RE.search(path.name)
        if match is None:
            continue
        try:
            stamp = datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        if 0 <= (today - stamp).days < days:
            selected.append(path)
    return selected


def _desktop_log_dir(home_dir: Path) -> Path | None:
    """Derive the desktop app's log dir when *home_dir* is its state dir."""
    normalized = str(home_dir).replace(os.sep, "/")
    if not normalized.endswith("/opensquilla/state"):
        return None
    candidate = home_dir.parent.parent / "logs"
    return candidate if candidate.is_dir() else None


def _resolve_log_dir(log_dir: Path | None, home_dir: Path) -> Path:
    if log_dir is not None:
        return Path(log_dir)
    env_value = os.environ.get(LOG_DIR_ENV, "").strip()
    if env_value:
        return Path(env_value).expanduser()
    return home_dir / "logs"


def _tilde(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def _load_raw_config() -> tuple[dict[str, Any], Path, str]:
    """tomllib-load the resolved config (never GatewayConfig.load, which rewrites)."""
    from opensquilla.diagnostics_sources import resolve_config_source

    config_path, source = resolve_config_source()
    with config_path.open("rb") as fh:
        data = tomllib.load(fh)
    return data, config_path, source


def _collect_config() -> tuple[str, str, str]:
    """Read the config TOML directly (never GatewayConfig.load) and redact it."""
    from opensquilla.diagnostics_sources import redact_config_payload

    data, config_path, source = _load_raw_config()
    text = json.dumps(redact_config_payload(data), indent=2, default=str)
    return text, str(config_path), source


def _collect_doctor() -> str:
    """Offline doctor report; the bundle never dials the gateway itself.

    Doctor's config loader migrates outdated payloads *in place* (rewrite +
    backup sibling), which would break this module's read-only contract. Run
    it against a throwaway temp copy of the config so any migration rewrite
    hits the copy, never the user's file.
    """
    from opensquilla.diagnostics_sources import offline_doctor_report, resolve_config_source

    config_path, _source = resolve_config_source()
    tmp_dir: str | None = None
    doctor_config_path: str | None = None
    try:
        if config_path.is_file():
            tmp_dir = tempfile.mkdtemp(prefix="opensquilla-bundle-doctor-")
            copy_path = Path(tmp_dir) / config_path.name
            shutil.copyfile(config_path, copy_path)
            doctor_config_path = str(copy_path)
        report = offline_doctor_report(
            RuntimeError("bundle offline collection"),
            gateway_url="ws://localhost:18791/ws",
            config_path=doctor_config_path,
        )
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return json.dumps(report, indent=2, default=str)


def _collect_diagnostics_flags() -> str:
    """Offline reconstruction of the ``logs.status`` RPC payload.

    Note: the snapshot reports the *ambient* process environment (env vars,
    default state paths), not the ``home_dir``/``log_dir`` overrides passed
    to ``collect_bundle`` — the artifact describes what a gateway launched
    from this environment would see.
    """
    from opensquilla.diagnostics_sources import logs_status_snapshot

    return json.dumps(logs_status_snapshot(), indent=2, default=str)


def _collect_toolchain_inventory(home_dir: Path) -> str:
    """Collect sanitized managed-component status without local paths."""

    candidates = (
        home_dir / "state" / "toolchains" / "v1" / "active",
        home_dir / "toolchains" / "v1" / "active",
    )
    active_dir = next((path for path in candidates if path.is_dir()), candidates[0])
    inventory: list[dict[str, object]] = []
    for path in sorted(active_dir.glob("*.json"))[:64]:
        try:
            if path.is_symlink() or path.stat().st_size > 64 * 1024:
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        component_id = raw.get("component_id")
        if not isinstance(component_id, str) or not component_id.strip():
            continue
        item: dict[str, object] = {"component_id": component_id[:128], "active": True}
        for key in ("version", "platform_key", "install_backend"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                item[key] = value[:128]
        inventory.append(item)
    return json.dumps(inventory, indent=2, default=str)


def _configured_state_dir() -> Path | None:
    """Best-effort ``state_dir`` from the config TOML (None when unset/unreadable)."""
    try:
        data, _path, _source = _load_raw_config()
    except Exception:  # noqa: BLE001 - config is optional for this probe
        return None
    raw = data.get("state_dir")
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip()).expanduser()
    return None


def _collect_errors(home_dir: Path, days: int, session_id: str | None = None) -> str:
    """turn_errors rows from the last *days* days, one JSON object per line."""
    candidates: list[Path] = []
    state_dir = _configured_state_dir()
    if state_dir is not None:
        candidates.append(state_dir / "sessions.db")
    candidates += [home_dir / "sessions.db", home_dir / "state" / "sessions.db"]
    db_path = next((path for path in candidates if path.exists()), candidates[-1])
    cutoff = int(datetime.now(UTC).timestamp() * 1000) - days * _DAY_MS
    sql = "SELECT * FROM turn_errors WHERE ts_ms >= ?"
    args: list[Any] = [cutoff]
    if session_id:
        sql += " AND (session_key = ? OR session_id = ?)"
        args += [session_id, session_id]
    sql += " ORDER BY ts_ms DESC, error_id DESC"
    uri = f"file:{quote(str(db_path), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(sql, args).fetchall()
    finally:
        connection.close()
    return "".join(
        json.dumps({key: row[key] for key in row.keys()}, default=str) + "\n" for row in rows
    )


def _add_full_text(archive: zipfile.ZipFile, path: Path) -> None:
    """Write one file in full under ``logs/`` (rotation already caps the set)."""
    text = path.read_bytes().decode("utf-8", errors="replace")
    _write_text(archive, f"logs/{path.name}", text)


def _add_log_artifacts(
    archive: zipfile.ZipFile,
    home_dir: Path,
    log_dir: Path,
    attempt: _Attempt,
    truncations: list[dict[str, Any]],
) -> None:
    """Structlog debug logs (full rotation set) plus the gateway supervisor log."""
    if log_dir.is_dir():
        for path in sorted(log_dir.glob("debug.log*")):
            attempt(f"logs/{path.name}", partial(_add_full_text, archive, path))
    gateway_log = home_dir / "logs" / "gateway.log"
    if gateway_log.is_file():
        attempt(
            "logs/gateway.log",
            lambda: _add_tail(archive, "logs/gateway.log", gateway_log, truncations),
        )


def _add_desktop_logs(
    archive: zipfile.ZipFile,
    home_dir: Path,
    attempt: _Attempt,
    truncations: list[dict[str, Any]],
) -> None:
    desktop_dir = _desktop_log_dir(home_dir)
    if desktop_dir is None:
        return
    for name in ("desktop.log", "gateway.log"):
        path = desktop_dir / name
        if path.is_file():
            attempt(
                f"desktop/{name}",
                partial(_add_tail, archive, f"desktop/{name}", path, truncations),
            )


def _add_day_files(
    archive: zipfile.ZipFile,
    log_dir: Path,
    days: int,
    include_content: bool,
    attempt: _Attempt,
    truncations: list[dict[str, Any]],
) -> None:
    groups = [("decisions", "decisions"), ("traces", "traces")]
    if include_content:
        groups.append(("turn-calls", "content"))
    for prefix, folder in groups:
        for path in _recent_day_files(log_dir, prefix, days):
            entry_name = f"{folder}/{path.name}"
            attempt(entry_name, partial(_add_tail, archive, entry_name, path, truncations))


def _add_extra_blob(archive: zipfile.ZipFile, key: str, value: Any) -> None:
    """Write one pre-serialized live-enrichment blob under ``live/``."""
    _write_text(archive, f"live/{key}.json", json.dumps(value, indent=2, default=str))


def _build_manifest(
    *,
    home_dir: Path,
    log_dir: Path,
    config_path: str | None,
    config_source: str | None,
    include_content: bool,
    days: int,
    collection_errors: list[dict[str, str]],
    truncations: list[dict[str, Any]],
    entries: list[str],
) -> dict[str, Any]:
    version_info = sys.version_info
    return {
        "bundle_schema": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "opensquilla_version": __version__,
        "python_version": f"{version_info.major}.{version_info.minor}.{version_info.micro}",
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "install_method": os.environ.get("OPENSQUILLA_INSTALL_METHOD")
        or ("desktop" if os.environ.get("OPENSQUILLA_DESKTOP") else "cli"),
        "home_dir": _tilde(home_dir),
        "log_dir": _tilde(log_dir),
        "config_path": config_path,
        "config_source": config_source,
        "env_present": sorted(name for name in os.environ if name.startswith("OPENSQUILLA_")),
        "content_tier": bool(include_content),
        "days": days,
        "collection_errors": collection_errors,
        "truncations": truncations,
        "entries": entries,
    }


def collect_bundle(
    dest: Path,
    *,
    days: int = 3,
    session_id: str | None = None,
    include_content: bool = False,
    home_dir: Path | None = None,
    log_dir: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> BundleResult:
    """Write a redacted diagnostics zip to *dest*; raises only if the zip can't be created."""
    home = Path(home_dir) if home_dir is not None else default_opensquilla_home()
    logs_dir = _resolve_log_dir(log_dir, home)
    collection_errors: list[dict[str, str]] = []
    truncations: list[dict[str, Any]] = []
    config_meta: dict[str, str | None] = {"path": None, "source": None}
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:

        def _attempt(artifact: str, collect: Callable[[], None]) -> None:
            try:
                collect()
            except Exception as exc:  # noqa: BLE001 - best-effort per artifact by design
                log.warning("bundle.artifact_failed", artifact=artifact, error=str(exc))
                collection_errors.append({"artifact": artifact, "error": str(exc)})

        def _config() -> None:
            text, path_str, source = _collect_config()
            config_meta["path"], config_meta["source"] = path_str, source
            _write_text(archive, "config.redacted.json", text)

        _attempt("config.redacted.json", _config)
        _attempt("doctor.json", lambda: _write_text(archive, "doctor.json", _collect_doctor()))
        _attempt(
            "diagnostics.json",
            lambda: _write_text(archive, "diagnostics.json", _collect_diagnostics_flags()),
        )
        _attempt(
            "toolchains.json",
            lambda: _write_text(
                archive,
                "toolchains.json",
                _collect_toolchain_inventory(home),
            ),
        )
        _attempt(
            "errors.jsonl",
            lambda: _write_text(archive, "errors.jsonl", _collect_errors(home, days, session_id)),
        )
        _add_log_artifacts(archive, home, logs_dir, _attempt, truncations)
        _add_desktop_logs(archive, home, _attempt, truncations)
        _add_day_files(archive, logs_dir, days, include_content, _attempt, truncations)
        for key, value in (extra or {}).items():
            _attempt(f"live/{key}.json", partial(_add_extra_blob, archive, key, value))
        manifest = _build_manifest(
            home_dir=home,
            log_dir=logs_dir,
            config_path=config_meta["path"],
            config_source=config_meta["source"],
            include_content=include_content,
            days=days,
            collection_errors=collection_errors,
            truncations=truncations,
            entries=[*archive.namelist(), "manifest.json"],
        )
        manifest_text = scrub_text(json.dumps(manifest, indent=2))
        archive.writestr("manifest.json", manifest_text)
    return BundleResult(path=dest, manifest=json.loads(manifest_text))
