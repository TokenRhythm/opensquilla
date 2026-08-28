"""Host-gated request correlation for the official TokenRhythm API."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from opensquilla.env import trust_env
from opensquilla.observability.network_policy import provider_install_id_disabled
from opensquilla.paths import default_opensquilla_home

from .environment import environment_value
from .types import ProviderRequestCorrelation

TOKENRHYTHM_INSTALL_ID_HEADER = "X-OpenSquilla-Install-Id"
TOKENRHYTHM_SESSION_ID_HEADER = "X-OpenSquilla-Session-Id"
TOKENRHYTHM_TURN_ID_HEADER = "X-OpenSquilla-Turn-Id"
TOKENRHYTHM_EXECUTION_ID_HEADER = "X-OpenSquilla-Execution-Id"
TOKENRHYTHM_CALL_KIND_HEADER = "X-OpenSquilla-Call-Kind"

_TOKENRHYTHM_CORRELATION_HOSTS = frozenset(
    {
        "tokenrhythm.studio",
        "api.tokenrhythm.studio",
    }
)
_CORRELATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ABSENT_CORRELATION_IDS = frozenset({"none", "null", "unknown"})
_AUXILIARY_CALL_ROLES = frozenset(
    {
        "meta",
        "vision_gate",
        "session_flush",
        "media",
        "naming",
        "compaction",
        "image_generation",
        "other",
    }
)
_ENSEMBLE_CALL_PHASES = frozenset(
    {
        "proposer",
        "aggregator",
        "fallback_single",
    }
)
_PROVIDER_FALLBACK_SEGMENT = "provider_fallback"
_NETWORK_OBSERVABILITY_DISABLED_ENV = (
    "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY"
)
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_CALL_KIND_MAX_LENGTH = 96
_INSTALL_ID_RETRY_SECONDS = 60.0
_INSTALL_TELEMETRY_STATE_FILE = "install_telemetry.json"
_INSTALL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_RAW_MAC_HEX_RE = re.compile(r"[0-9A-Fa-f]{12}")
_PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


@dataclass(frozen=True)
class _InstallIdContext:
    config: Any | None
    state_path: Path
    cache_key: str


@dataclass
class _InstallIdCacheEntry:
    install_id: str | None = field(default=None, repr=False)
    loading: bool = False
    retry_after: float = 0.0
    thread: threading.Thread | None = None


_INSTALL_ID_CACHE_LOCK = threading.RLock()
_INSTALL_ID_CACHE: dict[str, _InstallIdCacheEntry] = {}
_ACTIVE_INSTALL_ID_CONTEXT: _InstallIdContext | None = None


def _safe_correlation_id(value: str | None) -> str:
    candidate = str(value or "").strip()
    if (
        not candidate
        or candidate.lower() in _ABSENT_CORRELATION_IDS
        or _CORRELATION_ID_RE.fullmatch(candidate) is None
    ):
        return ""
    return candidate


def _safe_call_kind(value: str | None) -> str:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > _CALL_KIND_MAX_LENGTH:
        return ""
    parts = candidate.split(".")
    if parts[-1:] == [_PROVIDER_FALLBACK_SEGMENT]:
        parts = parts[:-1]
    if parts in (["agent", "chat"], ["subagent", "chat"]):
        return candidate
    if (
        len(parts) == 2
        and parts[0] == "auxiliary"
        and parts[1] in _AUXILIARY_CALL_ROLES
    ):
        return candidate
    if (
        len(parts) == 3
        and parts[0] in {"agent", "subagent"}
        and parts[1] == "ensemble"
        and parts[2] in _ENSEMBLE_CALL_PHASES
    ):
        return candidate
    return ""


def is_tokenrhythm_correlation_target(
    provider_kind: str | None,
    base_url: str | None,
) -> bool:
    """Return whether correlation metadata may be sent to this provider origin."""

    if str(provider_kind or "").strip().lower() != "tokenrhythm":
        return False
    try:
        candidate = str(base_url or "").strip()
        if not candidate or any(ord(character) <= 0x20 for character in candidate):
            return False
        parsed = urlparse(candidate)
        if (
            parsed.scheme.lower() != "https"
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False
        host = (parsed.hostname or "").lower()
        canonical_netlocs = {host, f"{host}:443"}
        return (
            host in _TOKENRHYTHM_CORRELATION_HOSTS
            and parsed.netloc.lower() in canonical_netlocs
            and parsed.port in {None, 443}
        )
    except ValueError:
        return False


def prewarm_tokenrhythm_install_id(
    *,
    config: Any | None = None,
    state_path: str | Path | None = None,
) -> threading.Thread | None:
    """Best-effort wrapper that never lets install-id prewarming affect startup."""

    try:
        return _prewarm_tokenrhythm_install_id(config=config, state_path=state_path)
    except Exception:
        return None


def _prewarm_tokenrhythm_install_id(
    *,
    config: Any | None = None,
    state_path: str | Path | None = None,
) -> threading.Thread | None:
    """Resolve the active install id in a best-effort daemon worker.

    The active context is registered synchronously so transport helpers that do
    not own the gateway configuration still select the correct profile and
    observe live changes to its privacy object.  Disk, address discovery, and
    state locking remain confined to the daemon worker.
    """

    context = _register_install_id_context(config=config, state_path=state_path)
    if provider_install_id_disabled(config=context.config):
        return None

    with _INSTALL_ID_CACHE_LOCK:
        entry = _INSTALL_ID_CACHE.setdefault(context.cache_key, _InstallIdCacheEntry())
        if _safe_install_id(entry.install_id):
            return None
        if entry.loading:
            return entry.thread
        if time.monotonic() < entry.retry_after:
            return None

        def _resolve() -> None:
            try:
                # Re-check immediately before any state access. A privacy
                # setting may have changed after the worker was scheduled.
                if provider_install_id_disabled(config=context.config):
                    with _INSTALL_ID_CACHE_LOCK:
                        entry.loading = False
                        entry.thread = None
                    return

                # ``ensure_install_telemetry_id`` normalizes telemetry state as
                # it persists it.  Do not invoke it when an existing file has
                # an explicitly unsafe id: transport validation must never
                # repair or otherwise rewrite the user's telemetry state.
                if _existing_state_blocks_install_id_resolution(context.state_path):
                    install_id = ""
                else:
                    from opensquilla.observability.install_telemetry import (
                        ensure_install_telemetry_id,
                    )

                    candidate = ensure_install_telemetry_id(
                        config=context.config,
                        state_path=context.state_path,
                    )
                    install_id = _safe_install_id(candidate)
            except Exception:
                install_id = ""

            with _INSTALL_ID_CACHE_LOCK:
                entry.loading = False
                entry.thread = None
                if install_id:
                    entry.install_id = install_id
                    entry.retry_after = 0.0
                else:
                    entry.install_id = None
                    entry.retry_after = time.monotonic() + _INSTALL_ID_RETRY_SECONDS

        try:
            thread = threading.Thread(
                target=_resolve,
                name="opensquilla-tokenrhythm-install-id",
                daemon=True,
            )
        except Exception:
            entry.retry_after = time.monotonic() + _INSTALL_ID_RETRY_SECONDS
            return None
        entry.loading = True
        entry.thread = thread
        try:
            thread.start()
        except Exception:
            entry.loading = False
            entry.thread = None
            entry.retry_after = time.monotonic() + _INSTALL_ID_RETRY_SECONDS
            return None
        return thread


def tokenrhythm_install_id_headers(
    provider_kind: str | None,
    base_url: str | None,
    *,
    config: Any | None = None,
    state_path: str | Path | None = None,
    proxy: str | None = None,
) -> dict[str, str]:
    """Best-effort install-id headers that can never fail a provider request."""

    try:
        return _tokenrhythm_install_id_headers(
            provider_kind,
            base_url,
            config=config,
            state_path=state_path,
            proxy=proxy,
        )
    except Exception:
        return {}


def _tokenrhythm_install_id_headers(
    provider_kind: str | None,
    base_url: str | None,
    *,
    config: Any | None = None,
    state_path: str | Path | None = None,
    proxy: str | None = None,
) -> dict[str, str]:
    """Return the cached install-id header for an official TokenRhythm origin.

    This function never performs state-file I/O.  A cold cache schedules a
    daemon resolver and omits the header from the current request; subsequent
    requests use the cached value after resolution completes.
    """

    if (
        str(proxy or "").strip()
        or _trusted_environment_proxy_configured()
        or not is_tokenrhythm_correlation_target(
            provider_kind,
            base_url,
        )
    ):
        return {}

    context = _current_or_register_install_id_context(
        config=config,
        state_path=state_path,
    )
    # This check deliberately happens for every physical request so a live
    # privacy change stops transmission without invalidating persisted state.
    if provider_install_id_disabled(config=context.config):
        return {}

    with _INSTALL_ID_CACHE_LOCK:
        entry = _INSTALL_ID_CACHE.get(context.cache_key)
        install_id = _safe_install_id(entry.install_id if entry is not None else None)
    if install_id:
        return {TOKENRHYTHM_INSTALL_ID_HEADER: install_id}

    prewarm_tokenrhythm_install_id(
        config=context.config,
        state_path=context.state_path,
    )
    return {}


def _current_or_register_install_id_context(
    *,
    config: Any | None,
    state_path: str | Path | None,
) -> _InstallIdContext:
    if config is None and state_path is None:
        with _INSTALL_ID_CACHE_LOCK:
            active = _ACTIVE_INSTALL_ID_CONTEXT
        if active is not None:
            return active
    return _register_install_id_context(config=config, state_path=state_path)


def _register_install_id_context(
    *,
    config: Any | None,
    state_path: str | Path | None,
) -> _InstallIdContext:
    path = _install_id_state_path(config=config, explicit=state_path)
    normalized_path = Path(os.path.abspath(os.fspath(path)))
    context = _InstallIdContext(
        config=config,
        state_path=normalized_path,
        cache_key=os.path.normcase(os.fspath(normalized_path)),
    )
    global _ACTIVE_INSTALL_ID_CONTEXT
    with _INSTALL_ID_CACHE_LOCK:
        _ACTIVE_INSTALL_ID_CONTEXT = context
    return context


def _install_id_state_path(
    *,
    config: Any | None,
    explicit: str | Path | None,
) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    configured_state_dir = getattr(config, "state_dir", None)
    if isinstance(configured_state_dir, str) and configured_state_dir.strip():
        root = Path(configured_state_dir.strip()).expanduser()
    else:
        root = default_opensquilla_home() / "state"
    return root / _INSTALL_TELEMETRY_STATE_FILE


def _safe_install_id(value: object) -> str:
    if not isinstance(value, str) or _INSTALL_ID_RE.fullmatch(value) is None:
        return ""
    # Persisted ids are hashes or UUIDs. Fail closed if a damaged or hand-edited
    # state file contains an address itself, even though that text would be a
    # syntactically valid HTTP header value.
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        return ""
    compact_mac = value.replace(":", "").replace("-", "").replace(".", "")
    if _RAW_MAC_HEX_RE.fullmatch(compact_mac) is not None:
        return ""
    return value


def redact_tokenrhythm_install_ids(text: str) -> str:
    """Mask cached install ids in diagnostic text without ever raising."""

    try:
        with _INSTALL_ID_CACHE_LOCK:
            install_ids = {
                install_id
                for entry in _INSTALL_ID_CACHE.values()
                if (install_id := _safe_install_id(entry.install_id))
            }
        redacted = text
        for install_id in sorted(install_ids, key=len, reverse=True):
            # HTTP libraries normalize header names to lowercase. Treat case
            # variants as the same secret so an upstream echo in a header name
            # cannot evade exact cached-id redaction.
            redacted = re.sub(re.escape(install_id), "***", redacted, flags=re.IGNORECASE)
        return redacted
    except Exception:
        return "***"


def _trusted_environment_proxy_configured() -> bool:
    if not trust_env():
        return False
    return any(environment_value(name).strip() for name in _PROXY_ENV_VARS)


def _existing_state_blocks_install_id_resolution(path: Path) -> bool:
    """Return whether existing state must be left untouched by this feature."""

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return True
    if not isinstance(data, dict) or "install_id" not in data:
        return not isinstance(data, dict)
    return not bool(_safe_install_id(data.get("install_id")))


def _reset_tokenrhythm_install_id_cache_for_tests() -> None:
    """Clear process cache state for deterministic unit tests."""

    global _ACTIVE_INSTALL_ID_CONTEXT
    with _INSTALL_ID_CACHE_LOCK:
        _INSTALL_ID_CACHE.clear()
        _ACTIVE_INSTALL_ID_CONTEXT = None


def tokenrhythm_correlation_headers(
    provider_kind: str | None,
    base_url: str | None,
    correlation: ProviderRequestCorrelation | None,
) -> dict[str, str]:
    """Build passive correlation headers for a trusted TokenRhythm request."""

    privacy_disabled = (
        os.environ.get(_NETWORK_OBSERVABILITY_DISABLED_ENV, "").strip().lower()
        in _TRUE_VALUES
    )
    if (
        privacy_disabled
        or correlation is None
        or not is_tokenrhythm_correlation_target(provider_kind, base_url)
    ):
        return {}

    candidates = (
        (
            TOKENRHYTHM_SESSION_ID_HEADER,
            _safe_correlation_id(correlation.session_id),
        ),
        (
            TOKENRHYTHM_TURN_ID_HEADER,
            _safe_correlation_id(correlation.turn_id),
        ),
        (
            TOKENRHYTHM_EXECUTION_ID_HEADER,
            _safe_correlation_id(correlation.execution_id),
        ),
        (
            TOKENRHYTHM_CALL_KIND_HEADER,
            _safe_call_kind(correlation.call_kind),
        ),
    )
    if any(not value for _header, value in candidates):
        return {}
    return dict(candidates)
