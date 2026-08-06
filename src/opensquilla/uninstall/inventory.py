"""Discovery: how OpenSquilla was installed and what it left on disk.

:func:`discover` returns an :class:`Inventory` describing the install method, the
resolved state roots, the removable data buckets (with SQLite WAL sidecars and
relocatable overrides), any installed OS service units, and the install receipt.
It performs read-only probing — no mutation — so it is safe to call for
``--dry-run`` / ``--json`` and is the single source of truth the planner consumes.
"""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opensquilla.paths import default_opensquilla_home, media_root_from_config
from opensquilla.uninstall import safety
from opensquilla.uninstall.receipt import read_receipt

# Install-method ids (superset of observability.install_telemetry, which is coarser).
METHOD_DOCKER = "docker"
METHOD_DESKTOP = "desktop"
METHOD_PORTABLE = "portable"
METHOD_SOURCE = "source-editable"
METHOD_UV_TOOL = "uv-tool"
METHOD_PIPX = "pipx"
METHOD_PIP = "pip"
METHOD_UNKNOWN = "unknown"

# Methods where the package is not user-managed and the in-package uninstaller
# must refuse to delete the runtime (image layers / Electron-bundled python).
REFUSE_PROGRAM_REMOVAL = frozenset({METHOD_DOCKER, METHOD_DESKTOP, METHOD_UNKNOWN})

_TRUTHY = {"1", "true", "yes", "on"}

# Flag buckets: which --purge-* flag authorizes deleting a bucket.
PURGE_STATE = "state"
PURGE_CONFIG = "config"
PURGE_ALL_ONLY = "all-only"


@dataclass
class DataBucket:
    """One removable on-disk location (file or directory)."""

    name: str
    path: Path
    category: str  # user-data | runtime-data | config | service | cache | desktop-data
    purge_flag: str  # PURGE_STATE | PURGE_CONFIG | PURGE_ALL_ONLY
    sidecars: tuple[str, ...] = ()  # suffixes, e.g. ("-wal", "-shm")
    glob: bool = False  # treat path.name as a glob pattern in path.parent
    outside_home: bool = False  # relocated outside the OpenSquilla home

    def existing_paths(self) -> list[Path]:
        """Concrete paths that currently exist (incl. sidecars / glob matches)."""
        out: list[Path] = []
        if self.glob:
            try:
                out.extend(sorted(self.path.parent.glob(self.path.name)))
            except OSError:
                pass
            return out
        if self.path.exists() or self.path.is_symlink():
            out.append(self.path)
        for suffix in self.sidecars:
            sidecar = self.path.with_name(self.path.name + suffix)
            if sidecar.exists():
                out.append(sidecar)
        return out


@dataclass
class ServiceUnit:
    """An OS service the user may have installed from the shipped templates."""

    platform: str
    label: str
    path: Path | None  # the installed unit file, if any
    commands: list[list[str]] = field(default_factory=list)  # unregister argv(s)


@dataclass
class Inventory:
    method: str
    home: Path
    state_root: Path
    config_path: Path | None
    entrypoints: list[Path]
    program_paths: list[Path]  # filesystem trees to remove (portable); else []
    package_uninstall: list[str] | None  # argv for the package manager, or None
    buckets: list[DataBucket]
    services: list[ServiceUnit]
    receipt: dict[str, Any] | None
    notes: list[str]
    source_checkout: Path | None = None  # the git checkout for source-editable installs
    home_recognized: bool = False  # home is positively OpenSquilla-shaped (gates rmtree)


# --------------------------------------------------------------------------- #
# Install-method detection
# --------------------------------------------------------------------------- #
def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


# The Dockerfile pins the container's state root here (OPENSQUILLA_STATE_DIR).
_DOCKER_DATA_ROOT = Path("/var/lib/opensquilla")


def _docker_image_install() -> bool:
    """True only for a genuine OpenSquilla docker image.

    A plain pip/uv install that merely runs inside a CI runner or devcontainer
    (which also has ``/.dockerenv``) is NOT a docker install and must stay
    uninstallable — so require the state root to be the image's data root too.
    """
    try:
        if not Path("/.dockerenv").exists():
            return False
    except OSError:
        return False
    return default_opensquilla_home() == _DOCKER_DATA_ROOT


def _module_root() -> Path:
    import opensquilla

    return Path(opensquilla.__file__).resolve().parent


def _portable_venv_dir() -> Path | None:
    """The portable per-release venv, detected by its install marker file."""
    try:
        venv = Path(sys.executable).resolve().parent.parent
        if any(venv.glob(".opensquilla-wheelhouse-*")):
            return venv
    except OSError:
        return None
    return None


def _is_editable_install() -> bool:
    try:
        dist = importlib_metadata.distribution("opensquilla")
    except importlib_metadata.PackageNotFoundError:
        dist = None
    if dist is not None:
        try:
            raw = dist.read_text("direct_url.json")
        except (OSError, ValueError):
            raw = None
        if raw:
            try:
                info = json.loads(raw)
                if bool(info.get("dir_info", {}).get("editable")):
                    return True
            except (json.JSONDecodeError, ValueError):
                pass
    # Fallback: running from a source checkout (module not under site-packages,
    # repo .git present) — never let this delete the checkout, only the shim.
    module_root = _module_root()
    if "site-packages" not in module_root.parts and "dist-packages" not in module_root.parts:
        repo_root = module_root.parent.parent  # <repo>/src/opensquilla -> <repo>
        if (repo_root / ".git").exists():
            return True
    return False


def _venv_ancestry() -> str | None:
    """Return ``uv-tool`` / ``pipx`` if the package lives in such a venv."""
    location = str(_module_root()).replace("\\", "/")
    if "/uv/tools/" in location:
        return METHOD_UV_TOOL
    if "/pipx/venvs/" in location:
        return METHOD_PIPX
    return None


def _has_distribution() -> bool:
    try:
        importlib_metadata.distribution("opensquilla")
        return True
    except importlib_metadata.PackageNotFoundError:
        return False


_KNOWN_METHODS = frozenset(
    {
        METHOD_DOCKER,
        METHOD_DESKTOP,
        METHOD_PORTABLE,
        METHOD_SOURCE,
        METHOD_UV_TOOL,
        METHOD_PIPX,
        METHOD_PIP,
    }
)


def detect_install_method(receipt_hint: str | None = None) -> str:
    """Detect the install method (priority: docker > desktop > portable > ...).

    ``receipt_hint`` (the install receipt's recorded method) is consulted only as
    a last resort, when runtime signals are inconclusive — never to override a
    concrete runtime detection.
    """
    explicit = os.environ.get("OPENSQUILLA_INSTALL_METHOD", "").strip().lower()

    if explicit == "docker":
        return METHOD_DOCKER
    if explicit == "desktop" or _env_truthy("OPENSQUILLA_DESKTOP"):
        return METHOD_DESKTOP
    if _env_truthy("OPENSQUILLA_RUNNING_IN_CONTAINER") or _docker_image_install():
        return METHOD_DOCKER
    if _portable_venv_dir() is not None:
        return METHOD_PORTABLE
    # Venv ancestry (uv-tool / pipx) is checked BEFORE the editable heuristic so
    # an editable install inside a uv/pipx venv removes via the right manager
    # rather than being misclassified as a plain source checkout.
    venv_kind = _venv_ancestry()
    if venv_kind is not None:
        return venv_kind
    if _is_editable_install():
        return METHOD_SOURCE
    if _has_distribution():
        return METHOD_PIP
    # Honor an explicit override (telemetry uses "source" for source-editable).
    if explicit == "source":
        return METHOD_SOURCE
    if explicit in {METHOD_PIP, METHOD_UNKNOWN}:
        return explicit
    if receipt_hint in _KNOWN_METHODS:
        return receipt_hint  # type: ignore[return-value]
    return METHOD_UNKNOWN


# --------------------------------------------------------------------------- #
# Program removal
# --------------------------------------------------------------------------- #
def _resolve_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (Path.home() / ".local/bin/uv", Path.home() / ".cargo/bin/uv"):
        if candidate.exists():
            return str(candidate)
    return None


def package_uninstall_argv(method: str) -> list[str] | None:
    """Argv for the package manager that owns this install, or ``None``."""
    if method == METHOD_UV_TOOL:
        uv = _resolve_uv()
        return [uv, "tool", "uninstall", "opensquilla"] if uv else None
    if method == METHOD_PIPX:
        pipx = shutil.which("pipx")
        return [pipx, "uninstall", "opensquilla"] if pipx else None
    if method in (METHOD_PIP, METHOD_SOURCE):
        # RECORD-driven removal with the interpreter that owns the dist; cleans
        # both console scripts and (for editable) the .pth without touching the
        # user's checkout.
        return [sys.executable, "-m", "pip", "uninstall", "-y", "opensquilla"]
    return None


def locate_entrypoints() -> list[Path]:
    """Resolve the ``opensquilla`` / ``gateway`` console scripts on PATH."""
    out: list[Path] = []
    for name in ("opensquilla", "gateway"):
        found = shutil.which(name)
        if found:
            out.append(Path(found))
    return out


# --------------------------------------------------------------------------- #
# Data buckets
# --------------------------------------------------------------------------- #
def _load_config(config_path: Path | None) -> Any | None:
    try:
        from opensquilla.gateway.config import GatewayConfig

        return GatewayConfig.load(str(config_path) if config_path else None)
    except Exception:  # noqa: BLE001 — config load must never block inventory
        return None


def _config_path() -> Path | None:
    env = os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH", "").strip()
    if env:
        return Path(env).expanduser()
    default = default_opensquilla_home() / "config.toml"
    return default if default.exists() else None


_WAL = ("-wal", "-shm")


def build_data_buckets(home: Path, config: Any | None) -> list[DataBucket]:
    """Enumerate every removable bucket under (and relocated out of) the home."""
    state = home / "state"
    buckets: list[DataBucket] = [
        # --- config (secrets / settings) ---
        DataBucket("config.toml", home / "config.toml", "config", PURGE_CONFIG),
        DataBucket(
            "config.toml backups",
            home / "config.toml.backup.*",
            "config",
            PURGE_CONFIG,
            glob=True,
        ),
        DataBucket(".env (secrets)", home / ".env", "config", PURGE_CONFIG),
        DataBucket("skills-taps.json", home / "skills-taps.json", "config", PURGE_CONFIG),
        DataBucket("skills-lock.json", home / "skills-lock.json", "config", PURGE_CONFIG),
        DataBucket("install receipt", home / "install-receipt.json", "config", PURGE_CONFIG),
        # --- state / runtime data ---
        DataBucket("state directory", state, "user-data", PURGE_STATE),
        DataBucket(
            "managed toolchains",
            state / "toolchains",
            "runtime-data",
            PURGE_STATE,
        ),
        DataBucket("logs", home / "logs", "desktop-data", PURGE_STATE),
        DataBucket("cache", home / "cache", "cache", PURGE_STATE),
        DataBucket("media / artifacts", home / "media", "user-data", PURGE_STATE),
        DataBucket("managed skills", home / "skills", "user-data", PURGE_STATE),
        DataBucket("skills quarantine", home / "quarantine", "cache", PURGE_STATE),
        DataBucket("skill proposals", home / "proposals", "user-data", PURGE_STATE),
        DataBucket("workspace", home / "workspace", "user-data", PURGE_STATE),
        DataBucket("session archive", home / "session-archive", "user-data", PURGE_STATE),
        DataBucket("code-task runs", home / "code-task", "user-data", PURGE_STATE),
        DataBucket("swebench artifacts", home / "swebench", "user-data", PURGE_STATE),
        DataBucket("migration backups", home / "migration", "user-data", PURGE_STATE),
    ]

    # Relocatable overrides that may land OUTSIDE the home. Reported but never
    # auto-deleted (they can point into user-owned trees, e.g. ~/Documents).
    relocated: list[tuple[str, Path | None]] = [
        ("scheduler DB", _env_path("OPENSQUILLA_SCHEDULER_DB")),
        ("memory DB", _env_path("OPENSQUILLA_MEMORY_DB")),
        ("meta-runs DB", _env_path("OPENSQUILLA_META_RUNS_DB")),
        ("log dir", _env_path("OPENSQUILLA_LOG_DIR")),
        ("turn-call log dir", _env_path("OPENSQUILLA_TURN_CALL_LOG_DIR")),
        ("session archive dir", _env_path("OPENSQUILLA_SESSION_ARCHIVE_DIR")),
        ("swebench artifacts dir", _env_path("OPENSQUILLA_SWEBENCH_ARTIFACTS_DIR")),
    ]
    if config is not None:
        try:
            media = media_root_from_config(config)
            relocated.append(("media root", media))
        except Exception:  # noqa: BLE001
            pass
        state_override = getattr(config, "state_dir", None)
        if isinstance(state_override, str) and state_override.strip():
            relocated.append(("relocated state dir", Path(state_override).expanduser()))
        workspace_override = getattr(config, "workspace_dir", None)
        if isinstance(workspace_override, str) and workspace_override.strip():
            relocated.append(("relocated workspace", Path(workspace_override).expanduser()))

    seen = {b.path.resolve() for b in buckets if not b.glob}
    for name, path in relocated:
        if path is None:
            continue
        resolved = safety.resolve_real(path)
        if resolved in seen:
            continue  # already enumerated as a hardcoded bucket
        seen.add(resolved)
        within = safety.is_within(path, home)
        sidecars = _WAL if name.endswith("DB") else ()
        # In-home relocations (e.g. a config.state_dir pointing elsewhere under
        # the home) are auto-purged like any state bucket; relocations OUTSIDE the
        # home are reported as manual (they may point into user-owned trees).
        buckets.append(
            DataBucket(
                name, path, "user-data", PURGE_STATE, sidecars=sidecars, outside_home=not within
            )
        )

    return buckets


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #
def detect_services() -> list[ServiceUnit]:
    """Detect installed OS service units from the shipped templates (best-effort)."""
    services: list[ServiceUnit] = []
    home = safety.home_dir()

    plist = home / "Library/LaunchAgents/ai.opensquilla.gateway.plist"
    if plist.exists():
        commands = [["launchctl", "unload", str(plist)]]
        uid = getattr(os, "getuid", None)
        if uid is not None:
            commands.insert(0, ["launchctl", "bootout", f"gui/{uid()}/ai.opensquilla.gateway"])
        services.append(ServiceUnit("launchd", "ai.opensquilla.gateway", plist, commands))

    unit = home / ".config/systemd/user/opensquilla.service"
    if unit.exists():
        services.append(
            ServiceUnit(
                "systemd",
                "opensquilla.service",
                unit,
                [["systemctl", "--user", "disable", "--now", "opensquilla.service"]],
            )
        )

    if os.name == "nt":
        services.append(
            ServiceUnit(
                "windows-task",
                "OpenSquilla",
                None,
                [["schtasks", "/Delete", "/TN", "OpenSquilla", "/F"]],
            )
        )

    return services


# --------------------------------------------------------------------------- #
# discover()
# --------------------------------------------------------------------------- #
def discover() -> Inventory:
    """Probe the environment and return a read-only :class:`Inventory`."""
    home = default_opensquilla_home()
    receipt = read_receipt(home)
    receipt_hint = receipt.get("install_method") if receipt else None
    method = detect_install_method(receipt_hint if isinstance(receipt_hint, str) else None)
    config_path = _config_path()
    config = _load_config(config_path)
    notes: list[str] = []

    program_paths: list[Path] = []
    package_uninstall: list[str] | None = None
    source_checkout: Path | None = None
    if method == METHOD_PORTABLE:
        venv = _portable_venv_dir()
        if venv is not None:
            program_paths.append(venv)
    elif method == METHOD_SOURCE:
        package_uninstall = package_uninstall_argv(method)
        module_root = _module_root()
        if "site-packages" not in module_root.parts and "dist-packages" not in module_root.parts:
            source_checkout = module_root.parent.parent
    elif method not in REFUSE_PROGRAM_REMOVAL:
        package_uninstall = package_uninstall_argv(method)
        if package_uninstall is None:
            notes.append(
                f"Could not resolve the package manager for '{method}'; "
                "program removal will be reported as a manual step."
            )

    buckets = build_data_buckets(home, config)

    return Inventory(
        method=method,
        home=home,
        state_root=home / "state",
        config_path=config_path,
        entrypoints=locate_entrypoints(),
        program_paths=program_paths,
        package_uninstall=package_uninstall,
        buckets=buckets,
        services=detect_services(),
        receipt=receipt,
        notes=notes,
        source_checkout=source_checkout,
        home_recognized=safety.looks_like_opensquilla_home(home),
    )
