"""Resolve a privacy-bounded source build identity for reliability events."""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from importlib import metadata as importlib_metadata
from pathlib import Path

import opensquilla
from opensquilla.paths import default_opensquilla_home

_GIT_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_MAX_METADATA_BYTES = 4096
_MAX_PACKED_REFS_BYTES = 2 * 1024 * 1024
_SOURCE_VERSION_SEPARATOR = "+source."
_INSTALL_RECEIPT_FILENAME = "install-receipt.json"


def reliability_app_version(base_version: str) -> str:
    """Append a source checkout's full commit while preserving the wire schema.

    Packaged installs and any inconclusive lookup keep the ordinary application
    version.  This function never shells out to Git and never reports branch,
    path, remote, or working-tree state.
    """

    commit_id = current_source_commit_id()
    if commit_id is None:
        return base_version
    candidate = f"{base_version}{_SOURCE_VERSION_SEPARATOR}{commit_id}"
    if len(candidate) > 64:
        return base_version
    return candidate


@lru_cache(maxsize=1)
def current_source_commit_id() -> str | None:
    """Return a full lowercase SHA-1 only for an explicit source installation."""

    try:
        checkout_root, direct_url = _source_distribution_details()
        vcs_commit = _vcs_commit_from_direct_url(direct_url)
        if vcs_commit is not None:
            return vcs_commit
        if checkout_root is not None and _is_editable_direct_url(direct_url):
            return _read_checkout_commit(checkout_root)

        # The receipt lives outside the installed package and may outlive a
        # later upgrade to an official wheel.  Trust it only when PEP 610 says
        # the package that is running actually came from a local source tree.
        if _is_local_directory_direct_url(direct_url):
            receipt_commit = _source_commit_from_receipt()
            if receipt_commit is not None:
                return receipt_commit

        # A source tree imported directly through PYTHONPATH may have no
        # installed distribution metadata at all.  In that case the checkout
        # itself remains the only explicit installation signal.
        if checkout_root is not None and direct_url is None:
            return _read_checkout_commit(checkout_root)
        return None
    except Exception:
        return None


def _source_distribution_details() -> tuple[Path | None, object]:
    package_file = getattr(opensquilla, "__file__", None)
    if not isinstance(package_file, str) or not package_file:
        return None, None
    package_root = Path(package_file).resolve().parent
    if package_root.name != "opensquilla" or package_root.parent.name != "src":
        checkout_root = None
    else:
        candidate = package_root.parent.parent
        checkout_root = candidate if (candidate / ".git").exists() else None

    try:
        distribution = importlib_metadata.distribution("opensquilla")
    except importlib_metadata.PackageNotFoundError:
        return checkout_root, None

    try:
        direct_url = distribution.read_text("direct_url.json")
    except (OSError, ValueError):
        direct_url = None
    if direct_url is None:
        return checkout_root, None
    try:
        payload = json.loads(direct_url)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, object()
    return checkout_root, payload


def _is_editable_direct_url(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    directory_info = value.get("dir_info")
    return isinstance(directory_info, dict) and directory_info.get("editable") is True


def _is_local_directory_direct_url(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    url = value.get("url")
    directory_info = value.get("dir_info")
    return (
        isinstance(url, str)
        and url.casefold().startswith("file:")
        and isinstance(directory_info, dict)
    )


def _vcs_commit_from_direct_url(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    vcs_info = value.get("vcs_info")
    if not isinstance(vcs_info, dict) or vcs_info.get("vcs") != "git":
        return None
    commit_id = vcs_info.get("commit_id")
    return _normalize_commit(commit_id) if isinstance(commit_id, str) else None


def _source_commit_from_receipt() -> str | None:
    try:
        raw = _read_bounded_text(_install_receipt_path(), _MAX_METADATA_BYTES)
    except FileNotFoundError:
        legacy_path = _legacy_install_receipt_path()
        if legacy_path is None:
            return None
        try:
            raw = _read_bounded_text(legacy_path, _MAX_METADATA_BYTES)
        except FileNotFoundError:
            return None
    try:
        payload = json.loads(raw.lstrip("\ufeff"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or type(version) is not int:
        return None
    commit_id = payload.get("source_commit_id")
    return _normalize_commit(commit_id) if isinstance(commit_id, str) else None


def _install_receipt_path() -> Path:
    return default_opensquilla_home() / _INSTALL_RECEIPT_FILENAME


def _legacy_install_receipt_path() -> Path | None:
    if os.environ.get("OPENSQUILLA_STATE_DIR", "").strip():
        return None
    if not (
        os.environ.get("OPENSQUILLA_PROFILE", "").strip()
        or os.environ.get("OPENSQUILLA_HOME", "").strip()
    ):
        return None
    configured_home = os.environ.get("HOME", "").strip()
    user_home = Path(configured_home).expanduser() if configured_home else Path.home()
    candidate = user_home / ".opensquilla" / _INSTALL_RECEIPT_FILENAME
    return None if candidate == _install_receipt_path() else candidate


def _read_checkout_commit(checkout_root: Path) -> str | None:
    git_dir = _resolve_git_dir(checkout_root / ".git")
    if git_dir is None:
        return None
    head = _read_bounded_text(git_dir / "HEAD", _MAX_METADATA_BYTES).strip()
    direct_commit = _normalize_commit(head)
    if direct_commit is not None:
        return direct_commit
    if not head.startswith("ref: "):
        return None
    ref = head.removeprefix("ref: ").strip()
    refs_root = _resolve_refs_root(git_dir)
    ref_path = _safe_ref_path(refs_root, ref)
    if ref_path is None:
        return None
    try:
        loose_commit = _normalize_commit(
            _read_bounded_text(ref_path, _MAX_METADATA_BYTES).strip()
        )
    except FileNotFoundError:
        loose_commit = None
    if loose_commit is not None:
        return loose_commit
    return _read_packed_ref(refs_root / "packed-refs", ref)


def _resolve_git_dir(git_marker: Path) -> Path | None:
    if git_marker.is_dir():
        return git_marker
    if not git_marker.is_file():
        return None
    marker = _read_bounded_text(git_marker, _MAX_METADATA_BYTES).strip()
    if not marker.startswith("gitdir: "):
        return None
    raw_path = marker.removeprefix("gitdir: ").strip()
    if not raw_path:
        return None
    git_dir = Path(raw_path)
    if not git_dir.is_absolute():
        git_dir = git_marker.parent / git_dir
    return git_dir.resolve()


def _resolve_refs_root(git_dir: Path) -> Path:
    try:
        raw_path = _read_bounded_text(git_dir / "commondir", _MAX_METADATA_BYTES).strip()
    except FileNotFoundError:
        return git_dir
    if not raw_path:
        return git_dir
    common_dir = Path(raw_path)
    if not common_dir.is_absolute():
        common_dir = git_dir / common_dir
    return common_dir.resolve()


def _safe_ref_path(refs_root: Path, ref: str) -> Path | None:
    if not ref.startswith("refs/") or "\\" in ref:
        return None
    parts = ref.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    candidate = refs_root.joinpath(*parts)
    try:
        candidate.relative_to(refs_root)
    except ValueError:
        return None
    return candidate


def _read_packed_ref(packed_refs: Path, ref: str) -> str | None:
    try:
        contents = _read_bounded_text(packed_refs, _MAX_PACKED_REFS_BYTES)
    except FileNotFoundError:
        return None
    for line in contents.splitlines():
        if not line or line.startswith(("#", "^")):
            continue
        fields = line.split(" ", 1)
        if len(fields) != 2 or fields[1] != ref:
            continue
        return _normalize_commit(fields[0])
    return None


def _read_bounded_text(path: Path, max_bytes: int) -> str:
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("source metadata exceeds the bounded read limit")
    return raw.decode("utf-8", errors="strict")


def _normalize_commit(value: str) -> str | None:
    if not _GIT_COMMIT_RE.fullmatch(value):
        return None
    return value


__all__ = ["current_source_commit_id", "reliability_app_version"]
