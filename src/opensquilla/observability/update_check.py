"""Passive update-availability check.

Queries the static OpenSquilla release-channel manifest mirrored to Aliyun OSS
and compares it with the running version, so the Control UI (and the
``opensquilla version --check`` command) can show a friendly "a newer version
is available" notice. This is intentionally passive: it never downloads or
installs anything, never blocks startup, and never raises.

The result is cached under the state dir with a 24h TTL so each passive channel
check performs at most one network attempt per day. The check honours the same
disable switch as anonymous install telemetry (so a single env var silences all
outbound "phone-home" calls) plus a dedicated switch, and is skipped
automatically in CI and test environments.

Electron surfaces use the native updater when it is available. This module also
powers the fallback notice used by unsigned desktop builds and the browser /
wheel / portable / Docker surfaces that share the same Control UI.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opensquilla import __version__
from opensquilla.observability.network_policy import network_observability_disabled
from opensquilla.paths import default_opensquilla_home

log = logging.getLogger(__name__)

UPDATE_CHECK_SCHEMA_VERSION = 2
UPDATE_CHECK_STATE_FILE = "update_check.json"
RC_UPDATE_CHECK_STATE_FILE = "update_check_rc.json"

# Dedicated switch plus the shared telemetry switch — either one disables the
# check, so users who opted out of telemetry get no surprise outbound calls.
UPDATE_CHECK_DISABLED_ENV = "OPENSQUILLA_UPDATE_CHECK_DISABLED"
TELEMETRY_DISABLED_ENV = "OPENSQUILLA_TELEMETRY_DISABLED"
UPDATE_CHECK_ENDPOINT_ENV = "OPENSQUILLA_UPDATE_CHECK_ENDPOINT"
TELEMETRY_TESTING_ENV = "OPENSQUILLA_TESTING"

DEFAULT_UPDATE_CHANNEL_ROOT = (
    "https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/channels"
)
DEFAULT_UPDATE_CHECK_ENDPOINT = f"{DEFAULT_UPDATE_CHANNEL_ROOT}/stable.json"
# RC checks are scoped to the running release line. ``_channel_for`` expands
# this template before it reaches the network; keeping it as a named constant
# also makes the public endpoint contract easy to exercise in tests.
DEFAULT_RC_UPDATE_CHECK_ENDPOINT = f"{DEFAULT_UPDATE_CHANNEL_ROOT}/preview/{{base}}.json"
DEFAULT_RELEASE_TAG_PAGE = "https://github.com/opensquilla/opensquilla/releases/tag"
DEFAULT_RELEASES_INDEX_PAGE = "https://github.com/opensquilla/opensquilla/releases"
# Compatibility name for callers that imported the old fallback constant.
# A lookup without an exact release URL leads to the generic index rather than
# implying that any particular GitHub Release was selected.
DEFAULT_RELEASES_PAGE = DEFAULT_RELEASES_INDEX_PAGE

DEFAULT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 3.0

_TRUE_VALUES = {"1", "true", "yes", "on"}
_AUTO_SKIP_ENV_VARS = ("GITHUB_ACTIONS", "PYTEST_CURRENT_TEST", TELEMETRY_TESTING_ENV)

# In-process results and background work are keyed by state file + channel
# scope. This prevents a stable result, or an RC result for another release
# line, from leaking across callers that share a process.
_CacheKey = tuple[str, str]
_CACHED_INFO: dict[_CacheKey, UpdateCheckInfo] | None = {}
_REFRESH_LOCKS: dict[_CacheKey, threading.Lock] = {}
_BACKGROUND_THREADS: dict[_CacheKey, threading.Thread] = {}
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class _UpdateChannel:
    kind: str
    scope: str
    state_file: str
    endpoint: str
    releases_page: str
    base: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class UpdateCheckInfo:
    current_version: str
    latest_version: str | None
    update_available: bool
    release_url: str
    checked_at: str | None = None
    disabled: bool = False
    error: str | None = None
    from_cache: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        """The minimal shape injected into the Control UI bootstrap context."""
        return {
            "current": self.current_version,
            "latest": self.latest_version,
            "available": self.update_available,
            "url": self.release_url,
            "checkedAt": self.checked_at,
        }


# ── Public API ───────────────────────────────────────────────────────────────


def default_update_info(*, version: str | None = None) -> UpdateCheckInfo:
    """Return the fixed no-candidate shape for the running update channel."""
    current = _current_version(version)
    return _empty_info(current, _channel_for(current))


def refresh_update_check(
    *,
    config: Any | None = None,
    state_path: str | Path | None = None,
    version: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    force: bool = False,
) -> UpdateCheckInfo:
    """Check for a newer release, using the cached result when still fresh.

    May perform one network call. Never raises. Writes the result to the cache
    file and the in-process cache. Pass ``force=True`` to bypass the TTL (used by
    ``opensquilla version --check``).
    """
    current = _current_version(version)
    channel = _channel_for(current)
    path = _state_path(config=config, explicit=state_path, channel=channel)
    key = _cache_key(path, channel)

    if _skip_reason(config=config):
        return _empty_info(current, channel, disabled=True)

    refresh_lock = _refresh_lock(key)
    with refresh_lock:
        try:
            state = _state_for_channel(_load_state(path), channel)
            last_attempt = state.get("last_attempt_ts")
            if not isinstance(last_attempt, (int, float)):
                # v1 stable caches predate attempt throttling. Their last
                # successful check is the best compatible throttle anchor.
                last_attempt = state.get("checked_ts")
            if not force and _is_fresh(last_attempt, ttl_seconds):
                info = _info_from_state(
                    state,
                    current=current,
                    channel=channel,
                    from_cache=True,
                )
                _store_cache(key, info)
                return info

            latest, release_url, error = _fetch_latest_release(
                _endpoint(channel), current, timeout=DEFAULT_TIMEOUT_SECONDS
            )
            now_ts = _now_ts()
            if error is not None:
                # A failed attempt is throttled separately from the last
                # successful result. Preserve only a same-scope candidate.
                state.update(
                    {
                        "schema_version": UPDATE_CHECK_SCHEMA_VERSION,
                        "cache_scope": channel.scope,
                        "last_attempt_ts": now_ts,
                        "last_error": error,
                    }
                )
                _write_state(path, state)
                info = _info_from_state(
                    state,
                    current=current,
                    channel=channel,
                    error=error,
                    from_cache=True,
                )
                _store_cache(key, info)
                return info

            # A successful lookup with no eligible candidate is a real result,
            # not a parse failure. Persist latest_version=null so repeated page
            # polling remains within the same TTL contract and clears any old
            # candidate that is no longer present.
            now_iso = _utc_now()
            resolved_url = release_url or channel.releases_page
            state.update(
                {
                    "schema_version": UPDATE_CHECK_SCHEMA_VERSION,
                    "cache_scope": channel.scope,
                    "latest_version": latest,
                    "release_url": resolved_url,
                    "checked_at": now_iso,
                    "checked_ts": now_ts,
                    "last_attempt_ts": now_ts,
                    "last_error": None,
                }
            )
            _write_state(path, state)
            info = _info_from_state(state, current=current, channel=channel)
            _store_cache(key, info)
            return info
        except Exception as exc:  # pragma: no cover - defensive guard
            log.debug("Update check failed: %s", exc, exc_info=True)
            return _empty_info(current, channel, error=str(exc))


def get_cached_update_info(
    *,
    config: Any | None = None,
    state_path: str | Path | None = None,
    version: str | None = None,
) -> UpdateCheckInfo | None:
    """Return the last known update info WITHOUT any network call.

    Reads the in-process cache, falling back to the state file (so a freshly
    started process picks up the previous run's result instantly — important for
    the desktop app, which restarts on every launch). ``update_available`` is
    recomputed against the *current* running version so a just-upgraded build
    immediately stops showing the notice. Returns ``None`` when no check has ever
    completed.
    """
    current = _current_version(version)
    if _skip_reason(config=config):
        return None

    channel = _channel_for(current)
    path = _state_path(config=config, explicit=state_path, channel=channel)
    key = _cache_key(path, channel)
    cached = _cached_info(key)
    if cached is not None:
        if cached.latest_version is None and cached.checked_at is None:
            return None
        return _recompute(cached, current, channel)

    state = _state_for_channel(_load_state(path), channel)
    latest = _cached_latest(state)
    if latest is None and not isinstance(state.get("checked_ts"), (int, float)):
        return None
    info = _info_from_state(
        state,
        current=current,
        channel=channel,
        from_cache=True,
    )
    _store_cache(key, info)
    return info


def start_background_update_check(
    *,
    config: Any | None = None,
    state_path: str | Path | None = None,
    version: str | None = None,
) -> threading.Thread | None:
    """Run :func:`refresh_update_check` in a daemon thread (fire-and-forget).

    Returns the thread (so tests can join it) or ``None`` when the check is
    disabled or its persisted attempt TTL is still fresh. Never raises.
    """
    current = _current_version(version)
    if _skip_reason(config=config):
        return None

    channel = _channel_for(current)
    path = _state_path(config=config, explicit=state_path, channel=channel)
    key = _cache_key(path, channel)

    def _run() -> None:
        try:
            refresh_update_check(
                config=config,
                state_path=path,
                version=current,
            )
        except Exception:  # pragma: no cover - defensive guard
            log.debug("Background update check failed", exc_info=True)
        finally:
            with _CACHE_LOCK:
                if _BACKGROUND_THREADS.get(key) is threading.current_thread():
                    _BACKGROUND_THREADS.pop(key, None)

    try:
        with _CACHE_LOCK:
            existing = _BACKGROUND_THREADS.get(key)
            if existing is not None and existing.is_alive():
                return existing
            if not _refresh_due(path, channel, DEFAULT_TTL_SECONDS):
                return None
            thread = threading.Thread(target=_run, name="opensquilla-update-check", daemon=True)
            _BACKGROUND_THREADS[key] = thread
            try:
                thread.start()
            except Exception:
                _BACKGROUND_THREADS.pop(key, None)
                raise
            return thread
    except Exception:  # pragma: no cover - thread spawn failure
        log.debug("Could not start update-check thread", exc_info=True)
        return None


# ── Internals ────────────────────────────────────────────────────────────────


def _current_version(version: str | None) -> str:
    return (version or __version__ or "unknown").strip() or "unknown"


def _channel_for(current: str) -> _UpdateChannel:
    parsed = _parse_rc_version(current)
    if parsed is not None and parsed[1] is not None:
        base = parsed[0]
        base_text = ".".join(str(part) for part in base)
        return _UpdateChannel(
            kind="rc",
            scope=f"rc:{base_text}",
            state_file=RC_UPDATE_CHECK_STATE_FILE,
            endpoint=DEFAULT_RC_UPDATE_CHECK_ENDPOINT.format(base=base_text),
            releases_page=DEFAULT_RELEASES_INDEX_PAGE,
            base=base,
        )
    return _UpdateChannel(
        kind="stable",
        scope="stable",
        state_file=UPDATE_CHECK_STATE_FILE,
        endpoint=DEFAULT_UPDATE_CHECK_ENDPOINT,
        releases_page=DEFAULT_RELEASES_PAGE,
    )


def _cache_key(path: Path, channel: _UpdateChannel) -> _CacheKey:
    return str(path.absolute()), channel.scope


def _cached_info(key: _CacheKey) -> UpdateCheckInfo | None:
    with _CACHE_LOCK:
        if not isinstance(_CACHED_INFO, dict):
            return None
        return _CACHED_INFO.get(key)


def _store_cache(key: _CacheKey, info: UpdateCheckInfo) -> None:
    global _CACHED_INFO
    with _CACHE_LOCK:
        if not isinstance(_CACHED_INFO, dict):
            _CACHED_INFO = {}
        _CACHED_INFO[key] = info


def _refresh_lock(key: _CacheKey) -> threading.Lock:
    with _CACHE_LOCK:
        lock = _REFRESH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _REFRESH_LOCKS[key] = lock
        return lock


def _refresh_due(path: Path, channel: _UpdateChannel, ttl_seconds: int) -> bool:
    state = _state_for_channel(_load_state(path), channel)
    last_attempt = state.get("last_attempt_ts")
    if not isinstance(last_attempt, (int, float)):
        last_attempt = state.get("checked_ts")
    return not _is_fresh(last_attempt, ttl_seconds)


def _cached_latest(state: dict[str, Any]) -> str | None:
    latest = state.get("latest_version")
    return latest if isinstance(latest, str) and latest.strip() else None


def _state_for_channel(state: dict[str, Any], channel: _UpdateChannel) -> dict[str, Any]:
    scope = state.get("cache_scope")
    if channel.kind == "stable":
        # v1 stable caches have no scope and remain valid. A non-stable scope
        # indicates contamination and must not be consumed by a stable build.
        if scope in (None, "stable"):
            return state
    elif scope == channel.scope:
        return state
    return {
        "schema_version": UPDATE_CHECK_SCHEMA_VERSION,
        "cache_scope": channel.scope,
    }


def _empty_info(
    current: str,
    channel: _UpdateChannel,
    *,
    disabled: bool = False,
    error: str | None = None,
) -> UpdateCheckInfo:
    return UpdateCheckInfo(
        current_version=current,
        latest_version=None,
        update_available=False,
        release_url=channel.releases_page,
        disabled=disabled,
        error=error,
    )


def _info_from_state(
    state: dict[str, Any],
    *,
    current: str,
    channel: _UpdateChannel,
    error: str | None = None,
    from_cache: bool = False,
) -> UpdateCheckInfo:
    latest = _cached_latest(state)
    checked_at = state.get("checked_at")
    state_error = state.get("last_error")
    return UpdateCheckInfo(
        current_version=current,
        latest_version=latest,
        update_available=_is_newer_for_channel(latest, current, channel),
        release_url=str(state.get("release_url") or channel.releases_page),
        checked_at=checked_at if isinstance(checked_at, str) else None,
        error=(
            error if error is not None else state_error if isinstance(state_error, str) else None
        ),
        from_cache=from_cache,
    )


def _recompute(info: UpdateCheckInfo, current: str, channel: _UpdateChannel) -> UpdateCheckInfo:
    if info.current_version == current:
        return info
    latest = info.latest_version
    return UpdateCheckInfo(
        current_version=current,
        latest_version=latest,
        update_available=_is_newer_for_channel(latest, current, channel),
        release_url=info.release_url,
        checked_at=info.checked_at,
        error=info.error,
        from_cache=True,
    )


def _state_path(
    *,
    config: Any | None,
    explicit: str | Path | None,
    channel: _UpdateChannel | None = None,
) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    configured_state_dir = getattr(config, "state_dir", None)
    if isinstance(configured_state_dir, str) and configured_state_dir.strip():
        root = Path(configured_state_dir.strip()).expanduser()
    else:
        root = default_opensquilla_home() / "state"
    return root / (channel.state_file if channel is not None else UPDATE_CHECK_STATE_FILE)


def _disabled(*, config: Any | None = None) -> bool:
    return network_observability_disabled(config=config)


def _skip_reason(*, config: Any | None = None) -> str | None:
    if _disabled(config=config):
        return "disabled"
    for name in _AUTO_SKIP_ENV_VARS:
        value = os.environ.get(name, "")
        if name == "PYTEST_CURRENT_TEST":
            if value.strip():
                return f"environment:{name}"
            continue
        if value.strip().lower() in _TRUE_VALUES:
            return f"environment:{name}"
    return None


def _endpoint(channel: _UpdateChannel | None = None) -> str:
    resolved = channel or _channel_for("0.0.0")
    return os.environ.get(UPDATE_CHECK_ENDPOINT_ENV, resolved.endpoint).strip()


def _releases_page(channel: _UpdateChannel | None = None) -> str:
    return (channel or _channel_for("0.0.0")).releases_page


def _now_ts() -> int:
    return int(datetime.now(UTC).timestamp())


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_fresh(checked_ts: object, ttl_seconds: int) -> bool:
    if isinstance(checked_ts, bool) or not isinstance(checked_ts, (int, float)):
        return False
    timestamp = float(checked_ts)
    if not math.isfinite(timestamp):
        return False
    age = _now_ts() - timestamp
    return 0 <= age < ttl_seconds


def _reject_nonfinite_json(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {value}")


def _load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=_reject_nonfinite_json,
            )
            if isinstance(data, dict):
                return data
        except (ValueError, UnicodeError, OSError):
            log.debug("Update-check state unreadable; replacing", exc_info=True)
    return {"schema_version": UPDATE_CHECK_SCHEMA_VERSION}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


_MANIFEST_TAG_RE = re.compile(
    r"^v(0|[1-9]\d*)[.](0|[1-9]\d*)[.](0|[1-9]\d*)"
    r"(?:rc(0|[1-9]\d*))?$"
)
_MANIFEST_VERSION_RE = re.compile(
    r"^(0|[1-9]\d*)[.](0|[1-9]\d*)[.](0|[1-9]\d*)"
    r"(?:-rc(0|[1-9]\d*))?$"
)


def _strict_manifest_version(
    value: object,
    pattern: re.Pattern[str],
) -> tuple[tuple[int, int, int], int | None] | None:
    if not isinstance(value, str):
        return None
    match = pattern.fullmatch(value.strip())
    if match is None:
        return None
    base = int(match.group(1)), int(match.group(2)), int(match.group(3))
    ordinal = int(match.group(4)) if match.group(4) is not None else None
    return base, ordinal


def _manifest_release(
    payload: object,
    channel: _UpdateChannel,
) -> tuple[str, str]:
    """Validate a v1 channel manifest and return its version and release page.

    Platform assets deliberately remain an Electron concern. This passive
    checker validates only the common discovery contract it consumes, plus the
    requested stable/preview scope so a misplaced moving object cannot leak a
    release from another channel into the cache.
    """
    if not isinstance(payload, dict):
        raise ValueError("channel manifest must be an object")
    schema_version = payload.get("schemaVersion")
    if type(schema_version) is not int or schema_version != 1:
        raise ValueError("unsupported channel manifest schemaVersion")

    tag_value = payload.get("tag")
    version_value = payload.get("version")
    tag = tag_value.strip() if isinstance(tag_value, str) else ""
    version = version_value.strip() if isinstance(version_value, str) else ""
    tag_parsed = _strict_manifest_version(tag, _MANIFEST_TAG_RE)
    version_parsed = _strict_manifest_version(version, _MANIFEST_VERSION_RE)
    if tag_parsed is None or version_parsed is None or tag_parsed != version_parsed:
        raise ValueError("channel manifest tag and version disagree")

    base, ordinal = tag_parsed
    base_text = ".".join(str(part) for part in base)
    base_value = payload.get("baseVersion")
    if not isinstance(base_value, str) or base_value.strip() != base_text:
        raise ValueError("channel manifest baseVersion disagrees with tag")
    prerelease = payload.get("prerelease")
    if type(prerelease) is not bool or prerelease is not (ordinal is not None):
        raise ValueError("channel manifest prerelease disagrees with tag")

    if channel.kind == "stable":
        if ordinal is not None:
            raise ValueError("stable channel manifest contains a prerelease")
    elif channel.base is None or base != channel.base:
        raise ValueError("preview channel manifest contains another release line")

    canonical_release_url = f"{DEFAULT_RELEASE_TAG_PAGE}/{tag}"
    if payload.get("releaseUrl") != canonical_release_url:
        raise ValueError("channel manifest releaseUrl is not canonical")
    return version, canonical_release_url


def _fetch_latest_release(
    endpoint: str,
    current_version: str,
    *,
    timeout: float,
) -> tuple[str | None, str | None, str | None]:
    """Return ``(manifest_version, canonical_release_url, error)``.

    Transport, HTTP, JSON, and schema failures return a non-null error. This is
    important for the cache contract: a damaged or temporarily unreachable
    moving manifest must preserve the last successful same-scope result rather
    than masquerade as a successful "no update" lookup.
    """
    if not endpoint:
        return None, None, "endpoint_empty"
    try:
        import httpx

        headers = {
            "Accept": "application/json",
            "User-Agent": f"opensquilla/{current_version}",
        }
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(endpoint, headers=headers)
        if response.status_code != 200:
            return None, None, f"http_status_{response.status_code}"
        payload = response.json()
    except Exception as exc:
        log.debug("Update-check fetch failed: %s", exc)
        return None, None, str(exc)

    try:
        latest, release_url = _manifest_release(
            payload,
            _channel_for(current_version),
        )
    except ValueError as exc:
        log.debug("Update-channel manifest rejected: %s", exc)
        return None, None, "manifest_invalid"
    return latest, release_url, None


_VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$")
_RC_VERSION_RE = re.compile(
    r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:(?:rc|-rc\.?)(\d+))?$",
    re.IGNORECASE,
)


def _parse_rc_version(
    value: str | None,
) -> tuple[tuple[int, int, int], int | None] | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or "+" in text:
        return None
    match = _RC_VERSION_RE.fullmatch(text)
    if match is None:
        return None
    base = int(match.group(1)), int(match.group(2)), int(match.group(3))
    ordinal = int(match.group(4)) if match.group(4) is not None else None
    return base, ordinal


def _version_key(value: str | None) -> tuple[tuple[int, int, int], int] | None:
    """Comparable key for a version string, or None when it should be ignored.

    Returns ((major, minor, patch), release_rank). A final release ranks above
    its own pre-release (rank 1 vs 0). Versions carrying build/local metadata
    (e.g. the ``0.0.0+unknown`` reported by editable/source checkouts) return
    None so dev installs are never nagged with an "update available" notice.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lstrip("vV")
    if not text or "+" in text:
        return None
    match = _VERSION_RE.match(text)
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    patch = int(match.group(3) or 0)
    remainder = (match.group(4) or "").strip()
    # Anything after the numeric core (rc1, a2, .dev3, -beta) marks a pre-release.
    release_rank = 0 if remainder else 1
    return (major, minor, patch), release_rank


def _is_newer(latest: str | None, current: str | None) -> bool:
    latest_key = _version_key(latest)
    current_key = _version_key(current)
    if latest_key is None or current_key is None:
        return False
    return latest_key > current_key


def _is_newer_for_channel(
    latest: str | None,
    current: str,
    channel: _UpdateChannel,
) -> bool:
    if channel.kind != "rc":
        return _is_newer(latest, current)
    latest_parsed = _parse_rc_version(latest)
    current_parsed = _parse_rc_version(current)
    if (
        latest_parsed is None
        or current_parsed is None
        or latest_parsed[0] != channel.base
        or current_parsed[0] != channel.base
        or current_parsed[1] is None
    ):
        return False
    latest_ordinal = latest_parsed[1]
    return latest_ordinal is None or latest_ordinal > current_parsed[1]
