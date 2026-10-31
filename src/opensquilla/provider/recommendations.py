"""Instance-scoped, offline-first recommended router and ensemble lineups.

Only the two published OpenRouter/TokenRhythm recommendation documents are
fetched. Reads never perform network I/O or mutate user configuration. Callers
resolve the returned rows into their own settings view or accepted-task snapshot.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from opensquilla.env import trust_env
from opensquilla.observability.network_policy import network_observability_disabled
from opensquilla.paths import default_opensquilla_home, native_io_path
from opensquilla.router_tiers import STATIC_B5_PROFILES, TEXT_TIERS

from .preset_registry import get_preset

log = logging.getLogger(__name__)

RECOMMENDATION_URLS = {
    "router": (
        "https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/"
        "releases/config/tr-harness/routing.json"
    ),
    "ensemble": (
        "https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/"
        "releases/config/tr-harness/ensemble.json"
    ),
}
TTL_SECONDS = 24 * 60 * 60
TIMEOUT_SECONDS = 3.0
MAX_PAYLOAD_BYTES = 256 * 1024
STATE_FILENAME = "recommendations.json"
_PROVIDERS = ("openrouter", "tokenrhythm")
_THINKING_LEVELS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "adaptive"})
_TRUE_VALUES = {"1", "true", "yes", "on"}
_MAX_PROPOSERS = 6
_MAX_NOTIFIED_REVISIONS = 32


@dataclass
class _Instance:
    path: Path
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    notified: dict[str, list[str]] = field(default_factory=dict)
    last_attempt: float | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    refresh_lock: threading.Lock = field(default_factory=threading.Lock)
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    config: Any = None


_INSTANCES: dict[str, _Instance] = {}
_INSTANCES_LOCK = threading.Lock()


def get_recommendation(config: Any, provider_id: str, kind: str) -> dict[str, Any]:
    """Return a detached remote-cache or bundled envelope; never fetch or save.

    The envelope has ``providerId``, ``kind``, content-hash ``revision``,
    ``source`` (remote/bundled), and ordered ``rows``. Each row contains only
    ``role``, ``provider``, ``model``, and nullable ``thinkingLevel``. Unsupported
    provider/kind pairs raise ValueError; they never acquire a recommendation.
    """
    key = _key(provider_id, kind)
    instance = _instance(config)
    with instance.lock:
        cached = instance.entries.get(key)
        if cached is not None:
            return copy.deepcopy(cached)
    return _bundled(provider_id, kind)


def recommendation_notified(
    config: Any, provider_id: str, kind: str, revision: str,
) -> bool:
    """Whether this instance acknowledged this recommendation content."""
    key = _key(provider_id, kind)
    instance = _instance(config)
    with instance.lock:
        return revision in instance.notified.get(key, ())


def mark_recommendation_notified(
    config: Any, provider_id: str, kind: str, revision: str,
) -> None:
    """Persist acknowledgement independently of user settings and other profiles."""
    key = _key(provider_id, kind)
    if not _valid_revision(revision):
        raise ValueError("invalid recommendation revision")
    instance = _instance(config)
    with instance.lock:
        revisions = instance.notified.setdefault(key, [])
        if revision not in revisions:
            previous = list(revisions)
            revisions.append(revision)
            del revisions[:-_MAX_NOTIFIED_REVISIONS]
            if not _save(instance):
                instance.notified[key] = previous
                raise OSError("recommendation acknowledgement could not be saved")


def refresh_recommendations(*, config: Any, force: bool = False) -> bool:
    """Synchronously refresh due documents; return whether cached content changed.

    This is the explicit test/worker boundary, not a user-facing update action.
    ``force`` bypasses the attempt TTL only, never privacy or test suppression.
    Concurrent refreshes return immediately. Failed/missing individual provider
    entries retain their last valid recommendation, independently of other entries.
    """
    if _network_disabled(config):
        return False
    instance = _instance(config)
    if not instance.refresh_lock.acquire(blocking=False):
        return False
    try:
        with instance.lock:
            if not force and _remaining_ttl(instance) > 0:
                return False
        updates: dict[str, dict[str, Any]] = {}
        for kind, url in RECOMMENDATION_URLS.items():
            if _network_disabled(config):
                return False
            try:
                payload = _fetch_document(url)
            except (httpx.HTTPError, OSError, ValueError, RecursionError):
                log.debug("Recommendation document unavailable: %s", kind)
                continue
            if not isinstance(payload, dict):
                continue
            for provider_id in _PROVIDERS:
                try:
                    rows = _parse_rows(payload.get(provider_id), provider_id, kind)
                except ValueError:
                    continue
                updates[_key(provider_id, kind)] = _envelope(
                    provider_id, kind, rows, source="remote",
                )
        if _network_disabled(config):
            return False
        with instance.lock:
            changed = any(
                instance.entries.get(key, {}).get("revision") != entry["revision"]
                for key, entry in updates.items()
            )
            instance.entries.update(updates)
            instance.last_attempt = time.time()
            _save(instance)
        return changed
    finally:
        instance.refresh_lock.release()


def start_background_recommendation_refresh(*, config: Any) -> threading.Thread | None:
    """Start one daemon worker per state directory, honoring the persisted 24h TTL.

    Returns immediately; a warm cache waits for the remaining TTL. Repeated calls
    reuse the same worker and update the config reference used for privacy checks.
    No worker is started under pytest/CI or when passive networking is disabled.
    """
    if _network_disabled(config) or not _uses_recommended_provider(config):
        return None
    instance = _instance(config)
    with instance.lock:
        instance.config = config
        if instance.thread is not None and instance.thread.is_alive():
            return instance.thread
        instance.stop.clear()
        thread = threading.Thread(
            target=_background_worker, args=(instance,),
            name="opensquilla-recommendations", daemon=True,
        )
        instance.thread = thread
        try:
            thread.start()
        except RuntimeError:
            instance.thread = None
            return None
        return thread


def _background_worker(instance: _Instance) -> None:
    try:
        while not instance.stop.is_set():
            with instance.lock:
                delay = _remaining_ttl(instance)
            if instance.stop.wait(delay):
                return
            with instance.lock:
                config = instance.config
            if _network_disabled(config) or not _uses_recommended_provider(config):
                return
            refresh_recommendations(config=config)
            # A concurrently running explicit refresh may own the attempt. Do
            # not spin while it is in flight or if a clock moved backwards.
            if instance.stop.wait(1.0):
                return
    except Exception:  # pragma: no cover - background work must never affect startup
        log.debug("Recommendation background refresh stopped", exc_info=True)


def _key(provider_id: str, kind: str) -> str:
    if provider_id not in _PROVIDERS or kind not in RECOMMENDATION_URLS:
        raise ValueError("recommendations support only OpenRouter/TokenRhythm router/ensemble")
    return f"{provider_id}:{kind}"


def _network_disabled(config: Any) -> bool:
    if network_observability_disabled(config=config):
        return True
    if os.environ.get("PYTEST_CURRENT_TEST", "").strip():
        return True
    return any(
        os.environ.get(name, "").strip().lower() in _TRUE_VALUES
        for name in ("CI", "GITHUB_ACTIONS", "OPENSQUILLA_TESTING")
    )


def _uses_recommended_provider(config: Any) -> bool:
    if getattr(getattr(config, "llm", None), "provider", None) in _PROVIDERS:
        return True
    profiles = getattr(config, "llm_profiles", {})
    if isinstance(profiles, dict) and any(
        str(key).split(":", 1)[0] in _PROVIDERS for key in profiles
    ):
        return True
    ensemble = getattr(config, "llm_ensemble", None)
    return bool(
        getattr(ensemble, "enabled", False)
        and getattr(ensemble, "selection_mode", None) in STATIC_B5_PROFILES
    )


def _remaining_ttl(instance: _Instance) -> float:
    if instance.last_attempt is None:
        return 0.0
    elapsed = time.time() - instance.last_attempt
    return max(0.0, TTL_SECONDS - elapsed) if elapsed >= 0 else 0.0


def _instance(config: Any) -> _Instance:
    state_dir = getattr(config, "state_dir", None)
    root = Path(state_dir).expanduser() if state_dir else default_opensquilla_home() / "state"
    path = (root / STATE_FILENAME).resolve()
    key = os.path.normcase(str(path))
    with _INSTANCES_LOCK:
        instance = _INSTANCES.get(key)
        if instance is None:
            instance = _Instance(path)
            _load(instance)
            _INSTANCES[key] = instance
        return instance


def _parse_rows(payload: Any, provider_id: str, kind: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("missing recommendation")
    if kind == "router":
        return [_row(payload.get(role), provider_id, role) for role in TEXT_TIERS]
    proposers = payload.get("proposers")
    if not isinstance(proposers, list) or not 2 <= len(proposers) <= _MAX_PROPOSERS:
        raise ValueError("invalid proposer count")
    rows = [
        *[_row(value, provider_id, f"proposer_{i}") for i, value in enumerate(proposers, 1)],
        _row(payload.get("aggregator"), provider_id, "aggregator"),
    ]
    if len({row["model"] for row in rows[:-1]}) != len(proposers):
        raise ValueError("recommendation proposer models must be distinct")
    return rows


def _row(value: Any, provider_id: str, role: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("invalid recommendation row")
    if value.get("provider", provider_id) != provider_id:
        raise ValueError("cross-provider recommendations are not supported")
    model = value.get("model")
    if (
        not isinstance(model, str) or not model.strip() or len(model) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in model)
    ):
        raise ValueError("invalid model")
    thinking = value.get("thinking_level")
    if thinking is not None and (
        not isinstance(thinking, str) or thinking not in _THINKING_LEVELS
    ):
        raise ValueError("invalid thinking level")
    return {
        "role": role, "provider": provider_id, "model": model.strip(), "thinkingLevel": thinking,
    }


def _envelope(
    provider_id: str, kind: str, rows: list[dict[str, Any]], *, source: str,
) -> dict[str, Any]:
    content = {"providerId": provider_id, "kind": kind, "rows": rows}
    revision = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**content, "revision": revision, "source": source}


def _bundled(provider_id: str, kind: str) -> dict[str, Any]:
    payload: dict[str, Any]
    if kind == "router":
        preset = get_preset(provider_id)
        payload = preset.tier_defaults() if preset else {}
    else:
        profile = next(p for p in STATIC_B5_PROFILES.values() if p.provider_id == provider_id)
        payload = {
            "proposers": [
                {"model": model, "thinking_level": profile.thinking_level}
                for model in profile.proposer_models
            ],
            "aggregator": {
                "model": profile.aggregator_model, "thinking_level": profile.thinking_level,
            },
        }
    return _envelope(provider_id, kind, _parse_rows(payload, provider_id, kind), source="bundled")


def _fetch_document(url: str) -> Any:
    with httpx.Client(
        timeout=TIMEOUT_SECONDS, trust_env=trust_env(), follow_redirects=False,
    ) as client:
        with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
            response.raise_for_status()
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > MAX_PAYLOAD_BYTES:
                raise ValueError("recommendation document is too large")
            chunks = bytearray()
            for chunk in response.iter_bytes(chunk_size=16 * 1024):
                chunks.extend(chunk)
                if len(chunks) > MAX_PAYLOAD_BYTES:
                    raise ValueError("recommendation document is too large")
            return json.loads(chunks)


def _valid_revision(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _load(instance: _Instance) -> None:
    try:
        with native_io_path(instance.path).open("rb") as handle:
            raw = handle.read(MAX_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_PAYLOAD_BYTES:
            return
        state = json.loads(raw)
        if not isinstance(state, dict) or state.get("schemaVersion") != 1:
            return
        entries = state.get("entries")
        if not isinstance(entries, dict):
            return
        for provider_id in _PROVIDERS:
            for kind in RECOMMENDATION_URLS:
                key = _key(provider_id, kind)
                entry = entries.get(key)
                if not isinstance(entry, dict) or not isinstance(entry.get("rows"), list):
                    continue
                rows = entry["rows"]
                try:
                    parsed = [
                        _row(
                            {**row, "thinking_level": row.get("thinkingLevel")},
                            provider_id, row["role"],
                        ) for row in rows if isinstance(row, dict)
                    ]
                    roles = [row["role"] for row in parsed]
                    expected = list(TEXT_TIERS) if kind == "router" else [
                        *[f"proposer_{i}" for i in range(1, len(parsed))], "aggregator",
                    ]
                    if roles != expected or not 3 <= len(parsed) <= _MAX_PROPOSERS + 1:
                        continue
                    if kind == "ensemble" and (
                        len({row["model"] for row in parsed[:-1]}) != len(parsed) - 1
                    ):
                        continue
                    validated = _envelope(provider_id, kind, parsed, source="remote")
                    if entry.get("revision") != validated["revision"]:
                        continue
                    instance.entries[key] = validated
                except (ValueError, KeyError, TypeError):
                    continue
        attempted = state.get("lastAttempt")
        if (
            isinstance(attempted, (int, float)) and not isinstance(attempted, bool)
            and math.isfinite(attempted)
        ):
            instance.last_attempt = float(attempted)
        notified = state.get("notified")
        if isinstance(notified, dict):
            for provider_id in _PROVIDERS:
                for kind in RECOMMENDATION_URLS:
                    key = _key(provider_id, kind)
                    revisions = notified.get(key)
                    if isinstance(revisions, list):
                        instance.notified[key] = [
                            rev for rev in revisions if _valid_revision(rev)
                        ][-_MAX_NOTIFIED_REVISIONS:]
    except (OSError, ValueError, OverflowError, RecursionError):
        return


def _save(instance: _Instance) -> bool:
    """Called under instance.lock; a failed cache write retains memory state."""
    temporary: str | None = None
    try:
        path = native_io_path(instance.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{STATE_FILENAME}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = handle.name
            json.dump({
                "schemaVersion": 1, "lastAttempt": instance.last_attempt,
                "entries": instance.entries, "notified": instance.notified,
            }, handle, sort_keys=True)
        os.replace(temporary, path)
        return True
    except OSError:
        log.debug("Recommendation cache could not be saved")
        return False
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def _reset_after_fork() -> None:  # pragma: no cover - platform-specific process lifecycle
    global _INSTANCES, _INSTANCES_LOCK
    _INSTANCES = {}
    _INSTANCES_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)
