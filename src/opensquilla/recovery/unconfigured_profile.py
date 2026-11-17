"""Initialize a fresh Desktop profile without selecting or storing a provider."""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from opensquilla.recovery.atomic import _native_io_path, is_path_redirecting_stat
from opensquilla.recovery.config_patch import ConfigSnapshot
from opensquilla.recovery.errors import DestinationExistsError, RecoveryError
from opensquilla.recovery.locking import LegacyGatewayLock, ProfileOperationLock, resolve_home_link
from opensquilla.recovery.models import RecoveryReport
from opensquilla.recovery.settings_transaction import (
    _durable_move_no_replace,
    _identity_matches,
    _identity_payload,
    _lexists,
    _plain_directory,
    _require_desktop_profile_kind,
    _write_no_replace,
)


def _unconfigured_candidate(payload: object) -> str:
    if not isinstance(payload, dict) or set(payload) != {"config"}:
        raise RecoveryError(
            "Invalid unconfigured profile input", stable_code="settings_input_invalid",
        )
    config = payload["config"]
    if not isinstance(config, str) or len(config.encode()) > 16 * 1024:
        raise RecoveryError(
            "Invalid unconfigured profile config", stable_code="settings_input_invalid",
        )
    try:
        candidate = tomllib.loads(config)
    except (tomllib.TOMLDecodeError, UnicodeError) as exc:
        raise RecoveryError(
            "Invalid unconfigured profile TOML", stable_code="settings_input_invalid",
        ) from exc
    control_ui = candidate.get("control_ui")
    if (
        set(candidate) != {"llm", "squilla_router", "llm_ensemble", "control_ui", "sandbox"}
        or candidate.get("llm") != {
            "provider": "", "model": "", "api_key": "", "api_key_env": "", "base_url": "",
        }
        or candidate.get("squilla_router") != {"enabled": False}
        or candidate.get("llm_ensemble") != {"enabled": False}
        or candidate.get("sandbox") != {"run_mode": "full"}
        or not isinstance(control_ui, dict)
        or set(control_ui) != {"enabled", "base_path", "default_locale"}
        or control_ui.get("enabled") is not True
        or control_ui.get("base_path") != "/control"
        or not isinstance(control_ui.get("default_locale"), str)
    ):
        raise RecoveryError(
            "The initial profile must not configure a provider or external data paths",
            stable_code="settings_input_invalid",
        )
    return config


def _initial_consent_mirror(path: Path) -> bool:
    """Recognize only the fail-closed mirror emitted before first model setup."""
    if path.lstat().st_size > 16 * 1024:
        return False
    snapshot = ConfigSnapshot.capture(path)
    if snapshot.identity is None:
        return False
    try:
        mirror: object = json.loads(snapshot.data)
    except (UnicodeError, json.JSONDecodeError):
        return False
    unset = {
        "enabled": None, "notice_version": None,
        "consented_at_utc": None, "forced_off": True,
    }
    return mirror == {"schema_version": 1, "reliability": unset, "growth": unset}


def _initial_canonical_roots(home: Path) -> bool:
    """Accept only empty data roots and known pre-Gateway control artifacts."""
    if not _lexists(home):
        return True
    _plain_directory(home)
    entries = tuple(home.iterdir())
    allowed = {"workspace", "state", "desktop-account", "desktop-layout-v2.json"}
    if any(entry.name not in allowed for entry in entries):
        return False
    for entry in entries:
        if entry.name == "desktop-layout-v2.json":
            if not _initial_layout_marker(entry):
                return False
            continue
        _plain_directory(entry)
        if entry.name == "desktop-account":
            if not _initial_desktop_accounts(entry):
                return False
            continue
        for child in entry.iterdir():
            if entry.name == "state" and child.name == "gateway.pid.lock":
                # LegacyGatewayLock already holds this exact compatibility
                # lock. Windows locks one byte, so reading it with another fd
                # would fail. Accept only the empty/one-byte regular lock file.
                info = child.lstat()
                if (is_path_redirecting_stat(info) or not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1 or info.st_size > 1):
                    return False
                continue
            if entry.name != "state" or child.name != "telemetry":
                return False
            _plain_directory(child)
            for artifact in child.iterdir():
                if (
                    artifact.name != "desktop-consent-mirror.json"
                    or not _initial_consent_mirror(artifact)
                ):
                    return False
    return True


def _initial_layout_marker(path: Path) -> bool:
    if path.lstat().st_size > 4096:
        return False
    snapshot = ConfigSnapshot.capture(path)
    if snapshot.identity is None:
        return False
    try:
        marker = json.loads(snapshot.data)
        if not isinstance(marker, dict) or set(marker) != {
            "schema_version", "migratedAt", "moved", "protectedBy",
        }:
            return False
        return (type(marker["schema_version"]) is int and marker["schema_version"] == 2
                and marker["moved"] == []
                and marker["protectedBy"] == "rc4"
                and isinstance(marker["migratedAt"], str)
                and datetime.fromisoformat(marker["migratedAt"]).tzinfo is not None)
    except (UnicodeError, ValueError):
        return False


def _initial_desktop_accounts(root: Path) -> bool:
    """Recognize Desktop's preboot account store without decrypting or rewriting it.

    Older clients could write this store before config initialization. Keep it
    in place, but do not admit arbitrary data, links, nested trees or hardlinks.
    """
    limits = {"installation-id": 36, "account.local.key": 32,
              "account.local.enc": 1024 * 1024, "account.enc": 1024 * 1024,
              "logged-out": 1}
    for origin in root.iterdir():
        if not re.fullmatch(r"[0-9a-f]{16}", origin.name):
            return False
        _plain_directory(origin)
        for artifact in origin.iterdir():
            maximum = limits.get(artifact.name)
            if maximum is None or artifact.lstat().st_size > maximum:
                return False
            snapshot = ConfigSnapshot.capture(artifact)
            if snapshot.identity is None:
                return False
            if artifact.name == "installation-id" and not re.fullmatch(
                rb"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", snapshot.data,
            ):
                return False
            if artifact.name == "account.local.key" and len(snapshot.data) != 32:
                return False
            if artifact.name == "logged-out" and snapshot.data != b"1":
                return False
    return True


def initialize_unconfigured_profile(
    home: str | Path,
    *,
    transaction_id: str,
    expected_revision: int,
    payload: object,
    lock_timeout: float = 0.0,
    _failpoint: Callable[[str], None] | None = None,
) -> RecoveryReport:
    """Publish a config once, under profile locks, without any credential file."""
    from opensquilla.recovery.engine import inspect_profile

    _require_desktop_profile_kind()
    config = _unconfigured_candidate(payload)
    home_path = resolve_home_link(Path(home).expanduser().absolute())
    config_path = home_path / "config.toml"
    callback = _failpoint or (lambda _phase: None)
    with (
        ProfileOperationLock(home_path, timeout=lock_timeout),
        LegacyGatewayLock(home_path, create_if_missing=False, timeout=lock_timeout),
    ):
        existing = ConfigSnapshot.capture(config_path)
        if existing.identity is not None:
            return inspect_profile(home_path)
        before = inspect_profile(home_path)
        if (
            before.outcome == "recovery_required"
            or before.transaction_id != transaction_id
            or before.revision != expected_revision
        ):
            raise RecoveryError(
                "Desktop profile changed after initialization preflight",
                stable_code="stale_recovery_transaction",
            )
        if (
            before.stable_code not in {
                "fresh_profile", "fresh_recovery_profile", "canonical_workspace",
                "effective_workspace_missing", "effective_state_missing",
                "unknown_layout",
            }
            or before.effective_workspace != home_path / "workspace"
            or not any(
                candidate.kind == "state" and candidate.path == home_path / "state"
                for candidate in before.candidates
            )
            or _lexists(home_path.parent / "desktop-credential.json")
            or not _initial_canonical_roots(home_path)
        ):
            raise RecoveryError(
                "Only an empty Desktop profile can be initialized",
                stable_code="profile_not_fresh",
            )
        _plain_directory(home_path.parent)
        _plain_directory(home_path, create=True)
        for root in (home_path / "workspace", home_path / "state"):
            _plain_directory(root, create=True)
        # A crash before publication may leave this private sibling behind; it
        # neither changes profile inspection nor makes config.toml a hard link.
        temporary = home_path.parent / f".{home_path.name}.unconfigured-{uuid.uuid4()}.tmp"
        identity = _write_no_replace(temporary, config.encode())
        try:
            callback("prepared")
            try:
                _durable_move_no_replace(temporary, config_path)
            except DestinationExistsError:
                return inspect_profile(home_path)
            callback("published")
        finally:
            if _identity_matches(temporary, _identity_payload(identity)):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(_native_io_path(temporary))
        return replace(inspect_profile(home_path), stable_code="unconfigured_profile_initialized")
