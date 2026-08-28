"""Runtime lookup for activated managed toolchains."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from opensquilla.skills.toolchains import registry
from opensquilla.skills.toolchains.manager import (
    ToolchainError,
    backfill_legacy_historical_marker_layout,
    package_payload_matches,
    toolchains_root,
)

MEDIA_FONTS_ENV = "OPENSQUILLA_MEDIA_FONTS_DIR"
PAPER_FONTS_ENV = "OSFONTDIR"
_PAYLOAD_VALIDATION_CACHE_LIMIT = 64
_PAYLOAD_VALIDATION_CACHE_TTL_SECONDS = 30.0
_SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_monotonic = time.monotonic
_payload_validation_cache: OrderedDict[
    tuple[object, ...], _ValidatedActivationCacheEntry
] = OrderedDict()
_payload_validation_flights: dict[tuple[object, ...], _ValidationFlight] = {}
_payload_validation_cache_lock = threading.Lock()
_payload_validation_cache_pid = os.getpid()


@dataclass(frozen=True)
class ActiveComponentStatus:
    """Sanitized status for diagnostics and managed-toolchain inventory."""

    component_id: str
    version: str
    platform_key: str
    install_backend: str
    supported: bool
    active: bool = True


@dataclass(frozen=True)
class _ValidatedActivation:
    """One receipt validated against its package and the current component identity."""

    descriptor: registry.ToolchainDescriptor
    package: Path
    bin_dirs: tuple[Path, ...]
    resources: Mapping[str, str]


@dataclass(frozen=True)
class _PathIdentity:
    """Cheap mutation identity for one bounded activation sentinel."""

    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    inode: int
    device: int


@dataclass(frozen=True)
class _SentinelIdentity:
    """Directory-entry and followed-target identity for one sentinel path."""

    entry: _PathIdentity
    symlink_target: str | None
    followed_target: _PathIdentity


@dataclass(frozen=True)
class _ValidatedActivationCacheEntry:
    activation: _ValidatedActivation
    validated_at: float
    sentinels: tuple[tuple[Path, _SentinelIdentity], ...]


@dataclass
class _ValidationFlight:
    """One transient validation shared only by callers already waiting on it."""

    event: threading.Event = field(default_factory=threading.Event)
    result: _ValidatedActivation | None = None
    error: BaseException | None = None
    invalidated: bool = False


def _verified_brew_prefix(formula: str) -> Path | None:
    trusted_brew = registry.trusted_brew_executable()
    if trusted_brew is None:
        return None
    brew = str(trusted_brew)
    try:
        completed = subprocess.run(
            [brew, "--prefix", formula],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.decode("utf-8", errors="replace").strip()
    path = Path(value) if value else None
    return path if path is not None and path.is_absolute() and path.is_dir() else None


def _active_activations(root: Path) -> tuple[_ValidatedActivation, ...]:
    activations: list[_ValidatedActivation] = []
    for component_id in registry.component_ids():
        if not (root / "active" / f"{component_id}.json").is_file():
            continue
        descriptor = registry.describe_component(component_id)
        activation = _validated_activation(root, descriptor, verify_payload=True)
        if activation is not None:
            activations.append(activation)
    return tuple(activations)


def _activation_bin_dirs(
    activations: tuple[_ValidatedActivation, ...],
) -> tuple[Path, ...]:
    directories: list[Path] = []
    for activation in activations:
        directories.extend(path for path in activation.bin_dirs if path not in directories)
    return tuple(directories)


def _reset_payload_validation_cache_after_fork() -> None:
    """Drop inherited entries and locks in a forked child process."""

    global _payload_validation_cache_lock
    global _payload_validation_cache_pid
    _payload_validation_cache.clear()
    _payload_validation_flights.clear()
    _payload_validation_cache_lock = threading.Lock()
    _payload_validation_cache_pid = os.getpid()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_payload_validation_cache_after_fork)


def _ensure_payload_validation_cache_process() -> None:
    """Defend against PID changes in runtimes that bypass at-fork hooks."""

    if os.getpid() != _payload_validation_cache_pid:
        _reset_payload_validation_cache_after_fork()


def invalidate_payload_validation_cache(
    component_id: str | None = None,
    *,
    root: Path | None = None,
) -> None:
    """Clear successful activation validations after managed-state changes."""

    _ensure_payload_validation_cache_process()
    normalized_root = (
        os.path.normcase(os.path.abspath(root)) if root is not None else None
    )

    def matches(candidate_root: object, candidate_component: object) -> bool:
        return bool(
            (normalized_root is None or candidate_root == normalized_root)
            and (component_id is None or candidate_component == component_id)
        )

    with _payload_validation_cache_lock:
        for key in tuple(_payload_validation_cache):
            if matches(key[0], key[1]):
                _payload_validation_cache.pop(key, None)
        for key, flight in tuple(_payload_validation_flights.items()):
            if matches(key[0], key[1]):
                flight.invalidated = True
                _payload_validation_flights.pop(key, None)


def _activation_cache_key(
    root: Path,
    descriptor: registry.ToolchainDescriptor,
) -> tuple[object, ...]:
    return (
        os.path.normcase(os.path.abspath(root)),
        descriptor.component_id,
        descriptor,
    )


def _stat_identity(value: os.stat_result) -> _PathIdentity:
    return _PathIdentity(
        mode=value.st_mode,
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        ctime_ns=value.st_ctime_ns,
        inode=value.st_ino,
        device=value.st_dev,
    )


def _path_identity(path: Path) -> _SentinelIdentity | None:
    try:
        entry = path.lstat()
        symlink_target = os.readlink(path) if stat.S_ISLNK(entry.st_mode) else None
        target = path.stat()
    except OSError:
        return None
    return _SentinelIdentity(
        entry=_stat_identity(entry),
        symlink_target=symlink_target,
        followed_target=_stat_identity(target),
    )


def _activation_sentinels(
    root: Path,
    activation: _ValidatedActivation,
) -> tuple[tuple[Path, _SentinelIdentity], ...] | None:
    paths = [
        root / "active" / f"{activation.descriptor.component_id}.json",
        activation.package / ".opensquilla-toolchain.json",
        activation.package,
        *activation.bin_dirs,
    ]
    probe_path = os.pathsep.join(str(path) for path in activation.bin_dirs)
    for command in activation.descriptor.probe_commands:
        if not command:
            return None
        resolved = shutil.which(command[0], path=probe_path)
        if resolved is None:
            return None
        paths.append(Path(resolved))
    for relative_resource in activation.resources.values():
        try:
            paths.append(_contained_path(activation.package, relative_resource))
        except ToolchainError:
            return None

    sentinels: list[tuple[Path, _SentinelIdentity]] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        identity = _path_identity(path)
        if identity is None:
            return None
        sentinels.append((path, identity))
    return tuple(sentinels)


def _sentinels_match(sentinels: tuple[tuple[Path, _SentinelIdentity], ...]) -> bool:
    return all(_path_identity(path) == identity for path, identity in sentinels)


def _fresh_cached_activation(
    key: tuple[object, ...],
    *,
    now: float,
) -> _ValidatedActivation | None:
    with _payload_validation_cache_lock:
        entry = _payload_validation_cache.get(key)
    if entry is None:
        return None
    age = now - entry.validated_at
    if not 0 <= age < _PAYLOAD_VALIDATION_CACHE_TTL_SECONDS:
        with _payload_validation_cache_lock:
            if _payload_validation_cache.get(key) is entry:
                _payload_validation_cache.pop(key, None)
        return None
    if not _sentinels_match(entry.sentinels):
        with _payload_validation_cache_lock:
            if _payload_validation_cache.get(key) is entry:
                _payload_validation_cache.pop(key, None)
        return None
    with _payload_validation_cache_lock:
        if _payload_validation_cache.get(key) is not entry:
            return None
        _payload_validation_cache.move_to_end(key)
    return entry.activation


def _join_validation_flight(
    key: tuple[object, ...],
) -> tuple[_ValidationFlight | None, bool]:
    """Join an active validation or become its leader.

    ``None`` asks the caller to retry the success cache because another leader
    populated it after the caller's first cache lookup.
    """

    with _payload_validation_cache_lock:
        if key in _payload_validation_cache:
            return None, False
        flight = _payload_validation_flights.get(key)
        if flight is not None:
            return flight, False
        flight = _ValidationFlight()
        _payload_validation_flights[key] = flight
        return flight, True


def _wait_for_validation_flight(flight: _ValidationFlight) -> _ValidatedActivation | None:
    flight.event.wait()
    if flight.error is not None:
        raise flight.error
    return flight.result


def _complete_validation_flight(
    key: tuple[object, ...],
    flight: _ValidationFlight,
    activation: _ValidatedActivation | None,
    sentinels: tuple[tuple[Path, _SentinelIdentity], ...] | None,
    *,
    error: BaseException | None = None,
) -> _ValidatedActivation | None:
    """Publish one flight and retain only successful activation state."""

    with _payload_validation_cache_lock:
        current = _payload_validation_flights.get(key)
        publish = (
            current is flight
            and not flight.invalidated
            and error is None
            and activation is not None
            and sentinels is not None
        )
        if publish:
            assert activation is not None
            assert sentinels is not None
            _payload_validation_cache[key] = _ValidatedActivationCacheEntry(
                activation=activation,
                validated_at=_monotonic(),
                sentinels=sentinels,
            )
            _payload_validation_cache.move_to_end(key)
            while len(_payload_validation_cache) > _PAYLOAD_VALIDATION_CACHE_LIMIT:
                _payload_validation_cache.popitem(last=False)
        if current is flight:
            _payload_validation_flights.pop(key, None)
        flight.result = activation if publish else None
        flight.error = error
        flight.event.set()
        return flight.result


def _package_payload_matches_cached(
    package: Path,
    descriptor: registry.ToolchainDescriptor,
) -> bool:
    return package_payload_matches(package, descriptor)


def _fully_validated_activation(
    root: Path,
    current: registry.ToolchainDescriptor,
    receipt: Mapping[str, object],
) -> tuple[
    _ValidatedActivation | None,
    tuple[tuple[Path, _SentinelIdentity], ...] | None,
]:
    """Validate one activation while rejecting fixed-sentinel races."""

    activation = _validated_activation_receipt(
        root,
        current,
        receipt,
        verify_payload=False,
    )
    if activation is None:
        return None, None
    before = _activation_sentinels(root, activation)
    if before is None:
        return None, None
    if not _package_payload_matches_cached(activation.package, activation.descriptor):
        return None, None
    after = _activation_sentinels(root, activation)
    if after is None or after != before:
        return None, None
    return activation, after


def _passive_archive_bin_dirs(
    root: Path,
    descriptor: registry.ToolchainDescriptor,
) -> tuple[Path, ...] | None:
    """Inspect an archive activation without spawning any native executable.

    This intentionally performs only receipt/marker/path checks. Full payload
    integrity and capability checks remain launch-time gates.
    """

    if descriptor.install_backend != "archive":
        return None
    activation = _validated_activation(root, descriptor, verify_payload=False)
    return activation.bin_dirs if activation is not None else None


def _validated_component_bin_dirs(
    root: Path,
    descriptor: registry.ToolchainDescriptor,
) -> tuple[Path, ...] | None:
    activation = _validated_activation(root, descriptor, verify_payload=True)
    return activation.bin_dirs if activation is not None else None


def list_active_components(*, root: Path | None = None) -> tuple[ActiveComponentStatus, ...]:
    """List only catalog- and marker-validated activations without exposing paths."""
    state_root = toolchains_root(root)
    statuses: list[ActiveComponentStatus] = []
    for component_id in registry.component_ids():
        current_descriptor = registry.describe_component(component_id)
        activation = _validated_activation(
            state_root,
            current_descriptor,
            verify_payload=True,
        )
        if activation is None:
            continue
        descriptor = activation.descriptor
        statuses.append(
            ActiveComponentStatus(
                component_id=descriptor.component_id,
                version=descriptor.version,
                platform_key=descriptor.platform_key,
                install_backend=descriptor.install_backend,
                supported=descriptor.supported,
            )
        )
    return tuple(statuses)


def _read_mapping(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _marker_mapping_matches(
    marker: Mapping[str, object],
    descriptor: registry.ToolchainDescriptor,
    *,
    require_payload_manifest: bool = False,
) -> bool:
    return bool(
        marker.get("component_id") == descriptor.component_id
        and marker.get("version") == descriptor.version
        and marker.get("platform_key") == descriptor.platform_key
        and marker.get("install_backend") == descriptor.install_backend
        and marker.get("sha256") == descriptor.sha256
        and (
            not require_payload_manifest
            or (
                marker.get("payload_manifest_version") == 1
                and isinstance(marker.get("payload_manifest"), dict)
            )
        )
    )


def _descriptor_for_receipt(
    receipt: Mapping[str, object],
    marker: Mapping[str, object],
    current: registry.ToolchainDescriptor,
) -> registry.ToolchainDescriptor | None:
    """Bind a current catalog identity to a current or historical package receipt.

    The catalog remains authoritative for component, host platform, install
    backend, and bin/resource layout.  Version and archive digest may come from
    a historical receipt only when the package marker agrees exactly.  The
    historical marker is a local install-time attestation rather than a remote
    trust anchor, so coordinated replacement of the state directory is outside
    this boundary; single-file receipt, marker, or payload changes fail closed.
    This is what lets an atomic rollback remain usable after the catalog advances.
    """

    if not (
        receipt.get("component_id") == current.component_id
        and receipt.get("platform_key") == current.platform_key
        and receipt.get("install_backend") == current.install_backend
    ):
        return None
    version = receipt.get("version")
    if not isinstance(version, str) or _SAFE_VERSION_RE.fullmatch(version) is None:
        return None

    marker_bin_relpaths = marker.get("bin_relpaths")
    if version == current.version and marker_bin_relpaths is None:
        # Compatibility for a current-version package installed by the first
        # managed-toolchain build. Reuse/install backfills the marker before a
        # future catalog can make this package historical.
        marker_bin_relpaths = list(current.bin_relpaths)
    historical_bin_relpaths = _validated_relative_path_list(
        marker_bin_relpaths,
        require_nonempty=True,
    )
    if historical_bin_relpaths is None or receipt.get("bin_relpaths") != list(
        historical_bin_relpaths
    ):
        return None

    raw_receipt_sha = receipt.get("sha256")
    if version == current.version:
        expected_receipt_sha = current.sha256 or ""
        # Activation receipts have carried this field since managed installs
        # shipped.  Accepting an omitted value for the current version keeps
        # older hand-authored state compatible because the marker digest is
        # still pinned by the code-owned catalog.
        if raw_receipt_sha not in {None, expected_receipt_sha}:
            return None
        if historical_bin_relpaths != current.bin_relpaths:
            return None
        effective = current
    elif current.install_backend == "archive":
        if (
            not isinstance(raw_receipt_sha, str)
            or _SHA256_RE.fullmatch(raw_receipt_sha) is None
            or marker.get("sha256") != raw_receipt_sha
        ):
            return None
        effective = replace(
            current,
            version=version,
            sha256=raw_receipt_sha,
            bin_relpaths=historical_bin_relpaths,
        )
    elif current.install_backend == "brew":
        # A Homebrew receipt points at the formula's live external prefix, not
        # an OpenSquilla-owned versioned payload.  Once the catalog version
        # changes, that prefix cannot prove it still contains the historical
        # executable, so archive-style historical activation is intentionally
        # unavailable for this backend.
        return None
    else:
        return None

    if not _marker_mapping_matches(marker, effective, require_payload_manifest=True):
        return None
    return effective


def _validated_relative_path(value: object) -> str | None:
    """Return one canonical archive-relative path or fail closed."""

    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
    ):
        return None
    path = PurePosixPath(value)
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if not parts or ".." in parts:
        return None
    normalized = PurePosixPath(*parts).as_posix()
    return normalized if normalized == value else None


def _validated_relative_path_list(
    value: object,
    *,
    require_nonempty: bool,
) -> tuple[str, ...] | None:
    if not isinstance(value, list) or (require_nonempty and not value):
        return None
    normalized: list[str] = []
    for item in value:
        selected = _validated_relative_path(item)
        if selected is None or selected in normalized:
            return None
        normalized.append(selected)
    return tuple(normalized)


def _validated_resources(
    package: Path,
    receipt: Mapping[str, object],
    marker: Mapping[str, object],
    descriptor: registry.ToolchainDescriptor,
    current: registry.ToolchainDescriptor,
) -> dict[str, str] | None:
    raw_resources = receipt.get("resources")
    marker_assets = marker.get("auxiliary_assets")
    marker_resources = marker.get("resources")
    marker_asset_kinds = marker.get("auxiliary_asset_kinds")
    manifest = marker.get("payload_manifest")
    if not (
        isinstance(raw_resources, dict)
        and isinstance(marker_assets, dict)
        and isinstance(manifest, dict)
    ):
        return None
    expected_resources = {
        asset.asset_id: asset.destination for asset in current.auxiliary_assets
    }
    expected_asset_kinds = {
        asset.asset_id: "archive" if asset.archive_type is not None else "direct"
        for asset in current.auxiliary_assets
    }
    if descriptor.version == current.version:
        expected_assets = {asset.asset_id: asset.sha256 for asset in current.auxiliary_assets}
        if marker_resources is None:
            marker_resources = expected_resources
        if marker_asset_kinds is None:
            marker_asset_kinds = expected_asset_kinds
        if (
            marker_assets != expected_assets
            or marker_resources != expected_resources
            or marker_asset_kinds != expected_asset_kinds
        ):
            return None
    elif not isinstance(marker_resources, dict) or not isinstance(
        marker_asset_kinds, dict
    ):
        return None

    if not isinstance(marker_resources, dict) or not isinstance(marker_asset_kinds, dict):
        return None
    if not (
        set(raw_resources) == set(marker_resources) == set(marker_assets) == set(marker_asset_kinds)
    ):
        return None

    resources: dict[str, str] = {}
    destinations: set[str] = set()
    for raw_asset_id, raw_destination in marker_resources.items():
        if (
            not isinstance(raw_asset_id, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", raw_asset_id) is None
        ):
            return None
        destination = _validated_relative_path(raw_destination)
        if destination is None or destination in destinations:
            return None
        if raw_resources.get(raw_asset_id) != destination:
            return None
        resources[raw_asset_id] = destination
        destinations.add(destination)

    for asset_id, relative_resource in resources.items():
        expected_digest = marker_assets.get(asset_id)
        asset_kind = marker_asset_kinds.get(asset_id)
        manifest_entry = manifest.get(relative_resource)
        if (
            not isinstance(expected_digest, str)
            or _SHA256_RE.fullmatch(expected_digest) is None
            or asset_kind not in {"archive", "direct"}
            or not isinstance(manifest_entry, dict)
            or manifest_entry.get("type") != "file"
            or _SHA256_RE.fullmatch(str(manifest_entry.get("sha256", ""))) is None
        ):
            return None
        # Direct resources remain byte-identical to their download. Archived
        # resources are source-verified before extraction and may then be
        # transformed before the complete payload manifest is written.
        if asset_kind == "direct" and manifest_entry.get("sha256") != expected_digest:
            return None
        try:
            resource = _contained_path(package, relative_resource)
        except ToolchainError:
            return None
        if not resource.is_file() or resource.is_symlink():
            return None
    return resources


def _validated_activation(
    root: Path,
    current: registry.ToolchainDescriptor,
    *,
    verify_payload: bool,
) -> _ValidatedActivation | None:
    if verify_payload:
        _ensure_payload_validation_cache_process()
        key = _activation_cache_key(root, current)
        while True:
            cached = _fresh_cached_activation(key, now=_monotonic())
            if cached is not None:
                return cached
            flight, is_leader = _join_validation_flight(key)
            if flight is None:
                continue
            if not is_leader:
                return _wait_for_validation_flight(flight)
            break

        activation: _ValidatedActivation | None = None
        sentinels: tuple[tuple[Path, _SentinelIdentity], ...] | None = None
        try:
            receipt = _read_mapping(root / "active" / f"{current.component_id}.json")
            if receipt is not None:
                activation, sentinels = _fully_validated_activation(root, current, receipt)
        except BaseException as exc:
            _complete_validation_flight(
                key,
                flight,
                None,
                None,
                error=exc,
            )
            raise
        return _complete_validation_flight(key, flight, activation, sentinels)

    receipt = _read_mapping(root / "active" / f"{current.component_id}.json")
    if receipt is None:
        return None
    return _validated_activation_receipt(
        root,
        current,
        receipt,
        verify_payload=verify_payload,
    )


def _validated_activation_receipt(
    root: Path,
    current: registry.ToolchainDescriptor,
    receipt: Mapping[str, object],
    *,
    verify_payload: bool,
) -> _ValidatedActivation | None:
    """Validate a receipt, safely upgrading a verified legacy archive marker."""

    raw_package = receipt.get("package_relpath")
    if not isinstance(raw_package, str):
        return None
    try:
        package = _contained_path(root, raw_package)
    except ToolchainError:
        return None
    marker = _read_mapping(package / ".opensquilla-toolchain.json")
    if marker is None:
        return None
    if receipt.get("version") != current.version and any(
        field not in marker
        for field in ("bin_relpaths", "resources", "auxiliary_asset_kinds")
    ):
        marker = backfill_legacy_historical_marker_layout(
            root,
            package,
            current,
            receipt,
            marker,
        )
        if marker is None:
            return None
    descriptor = _descriptor_for_receipt(receipt, marker, current)
    if descriptor is None:
        return None
    expected_package = (
        Path("packages") / descriptor.component_id / descriptor.version / descriptor.platform_key
    ).as_posix()
    if raw_package != expected_package:
        return None
    resources = _validated_resources(package, receipt, marker, descriptor, current)
    if resources is None:
        return None
    if verify_payload and not _package_payload_matches_cached(package, descriptor):
        return None

    raw_external = receipt.get("external_root")
    bin_root = package
    if isinstance(raw_external, str):
        if descriptor.install_backend != "brew" or not current.brew_formula:
            return None
        bin_root = Path(raw_external)
        if not bin_root.is_absolute() or ".." in bin_root.parts or not bin_root.is_dir():
            return None
        expected_prefix = _verified_brew_prefix(current.brew_formula)
        if expected_prefix is None:
            return None
        try:
            if bin_root.resolve(strict=True) != expected_prefix.resolve(strict=True):
                return None
        except OSError:
            return None
    elif raw_external is not None or descriptor.install_backend != "archive":
        return None

    try:
        directories = tuple(
            _contained_path(bin_root, raw_bin) for raw_bin in descriptor.bin_relpaths
        )
    except ToolchainError:
        return None
    if not directories or not all(directory.is_dir() for directory in directories):
        return None
    return _ValidatedActivation(
        descriptor=descriptor,
        package=package,
        bin_dirs=directories,
        resources=resources,
    )


def _marker_matches(
    path: Path,
    descriptor: registry.ToolchainDescriptor,
    *,
    require_payload_manifest: bool = False,
) -> bool:
    marker = _read_mapping(path)
    return bool(
        marker is not None
        and _marker_mapping_matches(
            marker,
            descriptor,
            require_payload_manifest=require_payload_manifest,
        )
    )


def _contained_path(root: Path, relative_value: str) -> Path:
    if not relative_value or "\x00" in relative_value or "\\" in relative_value:
        raise ToolchainError("Invalid relative path in activation receipt")
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ToolchainError("Activation receipt contains a path traversal")
    root_resolved = root.resolve(strict=False)
    candidate = root.joinpath(*relative.parts).resolve(strict=False)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ToolchainError("Activation receipt escaped the managed root") from exc
    return candidate


def resolve_managed_resource(
    asset_id: str,
    *,
    component_id: str | None = None,
    root: Path | None = None,
) -> Path | None:
    """Resolve a named, catalog-pinned resource from an active component."""
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-"
    if not asset_id or any(character not in allowed for character in asset_id):
        return None
    component_ids = (component_id,) if component_id is not None else registry.component_ids()
    state_root = toolchains_root(root)
    for selected_component in component_ids:
        if selected_component not in registry.component_ids():
            return None
        current = registry.describe_component(selected_component)
        activation = _validated_activation(state_root, current, verify_payload=True)
        if activation is None:
            continue
        resource = _resource_from_activation(activation, asset_id)
        if resource is not None:
            return resource
    return None


def _resource_from_activation(
    activation: _ValidatedActivation,
    asset_id: str,
) -> Path | None:
    relative_resource = activation.resources.get(asset_id)
    if relative_resource is None:
        return None
    try:
        resource = _contained_path(activation.package, relative_resource)
    except ToolchainError:
        return None
    return resource if resource.is_file() else None


def managed_env(
    base_env: Mapping[str, str] | None = None,
    *,
    root: Path | None = None,
) -> dict[str, str]:
    """Return an environment with explicitly active managed bins before system PATH."""
    env = dict(os.environ if base_env is None else base_env)
    state_root = toolchains_root(root)
    activations = _active_activations(state_root)
    existing = env.get("PATH", "")
    segments = [segment for segment in existing.split(os.pathsep) if segment]
    managed_segments = [str(path) for path in _activation_bin_dirs(activations)]
    ordered = [*managed_segments, *segments]
    deduplicated: list[str] = []
    normalized: set[str] = set()
    for value in ordered:
        key = os.path.normcase(os.path.abspath(value))
        if key not in normalized:
            deduplicated.append(value)
            normalized.add(key)
    env["PATH"] = os.pathsep.join(deduplicated)
    by_component = {
        activation.descriptor.component_id: activation for activation in activations
    }
    media_activation = by_component.get("media-ffmpeg")
    font = (
        _resource_from_activation(media_activation, "noto-cjk-font")
        if media_activation is not None
        else None
    )
    if font is not None and MEDIA_FONTS_ENV not in env:
        env[MEDIA_FONTS_ENV] = str(font.parent)
    paper_activation = by_component.get("paper-tex")
    paper_font = (
        _resource_from_activation(paper_activation, "noto-cjk-font")
        if paper_activation is not None
        else None
    )
    if paper_font is not None:
        existing_font_dirs = env.get(PAPER_FONTS_ENV, "")
        env[PAPER_FONTS_ENV] = os.pathsep.join(
            value for value in (str(paper_font.parent), existing_font_dirs) if value
        )
    return env


def resolve_managed_binary(
    name: str,
    *,
    root: Path | None = None,
    base_env: Mapping[str, str] | None = None,
) -> Path | None:
    """Resolve from explicitly active managed bins, then fall back to system PATH."""
    if not name or Path(name).name != name or any(separator in name for separator in ("/", "\\")):
        return None
    env = dict(os.environ if base_env is None else base_env)
    managed = managed_env(env, root=root)
    resolved = shutil.which(name, path=managed.get("PATH", ""))
    return Path(resolved) if resolved else None


def resolve_managed_binary_passive(
    name: str,
    *,
    root: Path | None = None,
) -> Path | None:
    """Resolve only marker-valid archive bins without executing host tooling."""

    if not name or Path(name).name != name or any(separator in name for separator in ("/", "\\")):
        return None
    state_root = toolchains_root(root)
    for component_id in registry.component_ids():
        descriptor = registry.describe_component(component_id)
        directories = _passive_archive_bin_dirs(state_root, descriptor)
        if directories is None:
            continue
        resolved = shutil.which(name, path=os.pathsep.join(str(path) for path in directories))
        if resolved:
            return Path(resolved)
    return None
