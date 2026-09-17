"""Gateway-owned refresh boundary for provider model catalogs.

TokenRhythm exposes two independent documents: a keyless public catalog and
an authenticated entitlement list.  This module keeps those documents
separate, publishes one atomic in-memory view, and owns the small persisted
last-good snapshot used across gateway restarts.  Ordinary turns and
``models.list`` only read that snapshot; network refreshes are limited to
boot warmup, configuration lifecycle events, and admin discovery.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import stat
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from opensquilla.paths import default_opensquilla_home, native_io_path
from opensquilla.provider.failures import ProviderFailureKind, classify_provider_error
from opensquilla.provider.live_catalog import (
    LIVE_CATALOG_TIMEOUT_SECONDS,
    warm_live_provider_catalogs,
)
from opensquilla.provider.model_catalog import ModelCatalog, shared_catalog
from opensquilla.provider.registry import UnknownProviderError, get_provider_spec
from opensquilla.provider.tokenrhythm_catalog import (
    TokenRhythmDeclaredModel,
    TokenRhythmModelMetadata,
    TokenRhythmPublishedModel,
    canonical_tokenrhythm_base_url,
    fetch_tokenrhythm_declared,
    fetch_tokenrhythm_published,
    is_official_tokenrhythm_endpoint,
    merge_tokenrhythm_catalog,
    tokenrhythm_authority_identity,
    tokenrhythm_declared_from_wire,
    tokenrhythm_declared_to_wire,
    tokenrhythm_public_transport_fingerprint,
    tokenrhythm_published_catalog_entries,
    tokenrhythm_published_from_wire,
    tokenrhythm_published_to_wire,
    tokenrhythm_transport_fingerprint,
)
from opensquilla.provider.types import ModelInfo

if TYPE_CHECKING:
    from opensquilla.onboarding.probe import ProviderModelsDiscoverResult

log = structlog.get_logger(__name__)

TOKENRHYTHM_SNAPSHOT_SCHEMA_VERSION = 1
TOKENRHYTHM_SUCCESS_TTL_SECONDS = 3600.0
TOKENRHYTHM_FAILURE_BACKOFF_SECONDS = 300.0
TOKENRHYTHM_PUBLIC_TIMEOUT_SECONDS = 5.0
TOKENRHYTHM_AUTH_TIMEOUT_SECONDS = 10.0
TOKENRHYTHM_REFRESH_DEADLINE_SECONDS = 10.0

LiveCatalogRefreshFingerprint = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class _TokenRhythmRequest:
    provider: str
    base_url: str
    api_key: str = field(repr=False)
    proxy: str = field(repr=False)
    authority_identity: str
    transport_fingerprint: str
    public_transport_fingerprint: str


@dataclass(slots=True)
class _PublishedSnapshot:
    models: dict[str, TokenRhythmPublishedModel] = field(default_factory=dict)
    success_at: float | None = None
    transport_fingerprint: str = ""


@dataclass(slots=True)
class _EntitlementSnapshot:
    authority_identity: str
    models: dict[str, TokenRhythmDeclaredModel] = field(default_factory=dict)
    success_at: float | None = None
    transport_fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class _RefreshOutcome:
    completed_at: float
    published: dict[str, TokenRhythmPublishedModel] | None = None
    published_error: BaseException | None = None
    declared: dict[str, TokenRhythmDeclaredModel] | None = None
    declared_error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _CatalogView:
    published: dict[str, TokenRhythmPublishedModel]
    declared: dict[str, TokenRhythmDeclaredModel]
    catalog: dict[str, object]
    declared_available: bool
    declared_error: BaseException | None = None


def _runtime_config(config: Any) -> Any:
    """Resolve current provider values without mutating the live config graph."""

    from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config

    scratch = (
        config.model_copy(deep=True)
        if hasattr(config, "model_copy")
        else copy.deepcopy(config)
    )
    return resolve_llm_runtime_config(scratch)


def _canonical_base_url(value: str) -> str:
    """Compatibility alias for the shared provider-boundary normalizer."""

    return canonical_tokenrhythm_base_url(value)


def _digest(domain: str, *parts: str) -> str:
    digest = hashlib.sha256()
    digest.update(f"opensquilla:{domain}:v1".encode())
    for part in parts:
        digest.update(b"\0")
        digest.update(part.encode())
    return digest.hexdigest()


def _tokenrhythm_request(
    *, provider: str, base_url: str, api_key: str, proxy: str
) -> _TokenRhythmRequest | None:
    provider_l = str(provider or "").strip().lower()
    key = str(api_key or "").strip()
    canonical_base = _canonical_base_url(base_url)
    authority = tokenrhythm_authority_identity(
        provider=provider_l,
        base_url=canonical_base,
        api_key=key,
    )
    if authority is None:
        return None
    proxy_value = str(proxy or "").strip()
    return _TokenRhythmRequest(
        provider=provider_l,
        base_url=canonical_base,
        api_key=key,
        proxy=proxy_value,
        authority_identity=authority,
        transport_fingerprint=tokenrhythm_transport_fingerprint(
            authority,
            proxy=proxy_value,
        ),
        public_transport_fingerprint=tokenrhythm_public_transport_fingerprint(
            proxy=proxy_value,
        ),
    )


def _request_from_config(config: Any) -> _TokenRhythmRequest | None:
    if config is None:
        return None
    runtime = _runtime_config(config)
    return _tokenrhythm_request(
        provider=runtime.provider,
        base_url=runtime.base_url,
        api_key=runtime.api_key,
        proxy=runtime.proxy,
    )


def _config_clears_tokenrhythm_credential(config: Any) -> bool:
    """Return whether a transition leaves no usable active TokenRhythm authority."""

    if config is None:
        return False
    runtime = _runtime_config(config)
    return _tokenrhythm_request(
        provider=runtime.provider,
        base_url=runtime.base_url,
        api_key=runtime.api_key,
        proxy=runtime.proxy,
    ) is None


def _allows_tokenrhythm_published_projection(config: Any) -> bool:
    """Keep official website facts out of a custom TokenRhythm endpoint."""

    if config is None:
        return True
    runtime = _runtime_config(config)
    if str(runtime.provider or "").strip().lower() == "tokenrhythm":
        base_url = runtime.base_url
    else:
        profile_parts = _profile_connection_parts(config, "tokenrhythm")
        if profile_parts is None:
            return True
        _profile, base_url, _proxy = profile_parts
    canonical_base = _canonical_base_url(base_url)
    return bool(
        canonical_base and is_official_tokenrhythm_endpoint(canonical_base)
    )


def _profile_for(config: Any, provider_id: str) -> Any | None:
    profiles = getattr(config, "llm_profiles", None) or {}
    provider = str(provider_id or "").strip().lower()
    for key, profile in profiles.items():
        if str(key or "").strip().lower() == provider:
            return profile
    return None


def _profile_connection_parts(
    config: Any,
    provider_id: str,
) -> tuple[Any, str, str] | None:
    """Return one profile plus its effective endpoint/proxy, without resolving a key."""

    provider = str(provider_id or "").strip().lower()
    if provider != "tokenrhythm":
        return None
    profile = _profile_for(config, provider)
    if profile is None:
        return None
    try:
        spec = get_provider_spec(provider)
    except UnknownProviderError:
        return None
    base_url = str(getattr(profile, "base_url", "") or spec.default_base_url or "").strip()
    profile_proxy = str(getattr(profile, "proxy", "") or "").strip()
    global_proxy = str(
        getattr(getattr(config, "llm", None), "proxy", "") or ""
    ).strip()
    return profile, base_url, profile_proxy or global_proxy


def _profile_identity_fingerprint(config: Any, provider_id: str) -> str:
    """Hash the authored credential/base identity for lifecycle comparison."""

    parts = _profile_connection_parts(config, provider_id)
    if parts is None:
        return ""
    profile, base_url, _proxy = parts
    pool = tuple(
        str(value or "").strip()
        for value in (getattr(profile, "api_key_env_pool", None) or [])
        if str(value or "").strip()
    )
    return _digest(
        "tokenrhythm-profile-identity",
        str(getattr(profile, "api_key", "") or "").strip(),
        str(getattr(profile, "api_key_env", "") or "").strip(),
        "\0".join(pool),
        _canonical_base_url(base_url),
    )


def _profile_proxy(config: Any, provider_id: str) -> str:
    parts = _profile_connection_parts(config, provider_id)
    return parts[2] if parts is not None else ""


def _profile_requests(
    config: Any,
    provider_id: str,
) -> dict[str, _TokenRhythmRequest]:
    """Resolve every key a profile pool can currently select, without mutating it."""

    from opensquilla.provider.environment import environment_value

    parts = _profile_connection_parts(config, provider_id)
    if parts is None:
        return {}
    profile, base_url, proxy = parts
    keys: list[str] = []
    direct = str(getattr(profile, "api_key", "") or "").strip()
    if direct:
        keys.append(direct)
    else:
        pool_names = [
            str(value or "").strip()
            for value in (getattr(profile, "api_key_env_pool", None) or [])
            if str(value or "").strip()
        ]
        for env_name in pool_names:
            value = environment_value(env_name).strip()
            if value and value not in keys:
                keys.append(value)
        if not keys:
            env_name = str(getattr(profile, "api_key_env", "") or "").strip()
            value = environment_value(env_name).strip() if env_name else ""
            if value:
                keys.append(value)
        if not keys:
            try:
                registry_env = str(get_provider_spec("tokenrhythm").env_key or "")
            except UnknownProviderError:
                registry_env = ""
            value = environment_value(registry_env).strip() if registry_env else ""
            if value:
                keys.append(value)

    requests: dict[str, _TokenRhythmRequest] = {}
    for key in keys:
        request = _tokenrhythm_request(
            provider="tokenrhythm",
            base_url=base_url,
            api_key=key,
            proxy=proxy,
        )
        if request is not None:
            requests[request.authority_identity] = request
    return requests


def _configured_tokenrhythm_requests(
    config: Any,
) -> dict[str, _TokenRhythmRequest]:
    """Return every official authority still reachable from durable config."""

    requests = _profile_requests(config, "tokenrhythm")
    active = _request_from_config(config)
    if active is not None:
        requests[active.authority_identity] = active
    return requests


def live_catalog_refresh_fingerprint(config: Any) -> LiveCatalogRefreshFingerprint:
    """Return a secret-free fingerprint of catalog authority and transport."""

    if config is None:
        return ("", "", "")
    runtime = _runtime_config(config)
    try:
        spec = get_provider_spec(runtime.provider)
    except UnknownProviderError:
        return ("", "", "")
    if not (spec.live_catalog_url and spec.live_catalog_shape):
        return ("", "", "")
    if spec.live_catalog_shape == "tokenrhythm":
        request = _tokenrhythm_request(
            provider=runtime.provider,
            base_url=runtime.base_url,
            api_key=runtime.api_key,
            proxy=runtime.proxy,
        )
        if request is None:
            return (runtime.provider, "", "")
        return (
            request.provider,
            request.authority_identity,
            request.transport_fingerprint,
        )
    # Other live catalogs are public and only need a configured credential as
    # their lifecycle gate.  Do not hash or retain their key value here.
    return (
        runtime.provider,
        "configured" if runtime.api_key else "",
        _digest("live-catalog-transport", str(runtime.proxy or "")),
    )


def _state_path(config: Any) -> Path:
    configured = str(getattr(config, "state_dir", "") or "").strip()
    root = Path(configured).expanduser() if configured else default_opensquilla_home() / "state"
    return root / "model_catalog" / "tokenrhythm-v1.json"


def _valid_timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _wire_values_match(raw: object, normalized: object) -> bool:
    """Compare normalized JSON values without Python's bool/int/float coercions."""

    if isinstance(normalized, dict):
        if not isinstance(raw, Mapping) or set(raw) != set(normalized):
            return False
        return all(
            _wire_values_match(raw[key], value)
            for key, value in normalized.items()
        )
    if isinstance(normalized, list):
        if not isinstance(raw, list) or len(raw) != len(normalized):
            return False
        return all(
            _wire_values_match(raw_value, normalized_value)
            for raw_value, normalized_value in zip(raw, normalized, strict=True)
        )
    return type(raw) is type(normalized) and raw == normalized


def _valid_snapshot_model_id(value: object) -> bool:
    """Accept only the normalized, non-empty model ids emitted by the parser."""

    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _valid_published_model_wire(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    try:
        normalized = TokenRhythmPublishedModel.from_wire(value)
    except Exception:  # noqa: BLE001 - a corrupt local snapshot must degrade safely
        return False
    return bool(normalized.name) and _wire_values_match(value, normalized.to_wire())


def _valid_declared_model_wire(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    try:
        normalized = TokenRhythmDeclaredModel.from_wire(value)
    except Exception:  # noqa: BLE001 - a corrupt local snapshot must degrade safely
        return False
    return bool(normalized.display_name) and _wire_values_match(
        value, normalized.to_wire()
    )


def _valid_model_table(value: object, *, published: bool) -> bool:
    if not isinstance(value, Mapping):
        return False
    validator = (
        _valid_published_model_wire if published else _valid_declared_model_wire
    )
    return all(
        _valid_snapshot_model_id(model_id) and validator(model)
        for model_id, model in value.items()
    )


def _valid_snapshot_digest(value: object, *, allow_empty: bool = False) -> bool:
    if allow_empty and value == "":
        return True
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _valid_snapshot_payload(payload: Mapping[str, Any]) -> bool:
    """Validate the complete schema-v1 document before publishing any of it."""

    if set(payload) != {
        "schemaVersion",
        "published",
        "entitlements",
        "lastAlignedAt",
    }:
        return False

    published = payload.get("published")
    if not isinstance(published, Mapping) or set(published) != {
        "successAt",
        "transportFingerprint",
        "models",
    }:
        return False
    published_success = published.get("successAt")
    if published_success is not None and _valid_timestamp(published_success) is None:
        return False
    if not _valid_snapshot_digest(
        published.get("transportFingerprint"), allow_empty=True
    ) or not _valid_model_table(published.get("models"), published=True):
        return False

    entitlements = payload.get("entitlements")
    if not isinstance(entitlements, Mapping):
        return False
    for authority, raw_entitlement in entitlements.items():
        if not _valid_snapshot_digest(authority) or not isinstance(
            raw_entitlement, Mapping
        ):
            return False
        if set(raw_entitlement) != {
            "successAt",
            "transportFingerprint",
            "models",
        }:
            return False
        entitlement_success = raw_entitlement.get("successAt")
        if (
            entitlement_success is not None
            and _valid_timestamp(entitlement_success) is None
        ):
            return False
        if not _valid_snapshot_digest(
            raw_entitlement.get("transportFingerprint")
        ) or not _valid_model_table(
            raw_entitlement.get("models"), published=False
        ):
            return False

    aligned = payload.get("lastAlignedAt")
    if not isinstance(aligned, Mapping):
        return False
    for authority, timestamp in aligned.items():
        if (
            authority not in entitlements
            or not _valid_snapshot_digest(authority)
            or _valid_timestamp(timestamp) is None
        ):
            return False
    return True


def _read_snapshot_file(path: Path) -> dict[str, Any] | None:
    """Read a snapshot only when the target is a regular, non-symlink file."""

    target = native_io_path(path)
    parent = native_io_path(path.parent)
    try:
        parent_mode = parent.lstat().st_mode
        if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
            return None
        mode = target.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            return None
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (
        FileNotFoundError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
    ):
        return None
    if not isinstance(payload, dict):
        return None
    schema_version = payload.get("schemaVersion")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != TOKENRHYTHM_SNAPSHOT_SCHEMA_VERSION
    ):
        return None
    if not _valid_snapshot_payload(payload):
        return None
    return payload


def _write_snapshot_file(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically write a private normalized snapshot (POSIX hardening best effort)."""

    target = native_io_path(path)
    parent = native_io_path(path.parent)
    try:
        parent_mode = parent.lstat().st_mode
    except FileNotFoundError:
        parent.mkdir(mode=0o700, parents=True, exist_ok=False)
        parent_mode = parent.lstat().st_mode
    if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
        raise OSError("refusing model catalog snapshot through unsafe parent")
    if os.name != "nt":
        os.chmod(parent, 0o700)
    try:
        mode = target.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise OSError("refusing to replace non-regular model catalog snapshot")

    fd, temporary = tempfile.mkstemp(prefix=".tokenrhythm-v1-", dir=os.fspath(parent))
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        if os.name != "nt":
            os.chmod(target, 0o600)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _is_fresh(success_at: float | None, now: float) -> bool:
    if success_at is None:
        return False
    age = now - success_at
    return 0 <= age < TOKENRHYTHM_SUCCESS_TTL_SECONDS


def _iso_utc(timestamp: float | None, now: float) -> str | None:
    if timestamp is None or timestamp > now:
        return None
    try:
        return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _error_failure_kind(error: BaseException | None) -> str:
    if error is None:
        return ""
    if isinstance(error, httpx.HTTPStatusError):
        return classify_provider_error(
            "tokenrhythm", error.response.status_code, message=""
        ).value
    if isinstance(error, (httpx.TransportError, TimeoutError, asyncio.TimeoutError)):
        return ProviderFailureKind.TRANSPORT_TRANSIENT.value
    if isinstance(error, (ValueError, TypeError, json.JSONDecodeError)):
        return ProviderFailureKind.MALFORMED_RESPONSE.value
    return ProviderFailureKind.UNKNOWN.value


def _first_capability(
    declared: TokenRhythmDeclaredModel,
    published: TokenRhythmPublishedModel | None,
    name: str,
    default: bool | None = None,
) -> bool | None:
    declared_value = getattr(declared.capabilities, name)
    if isinstance(declared_value, bool):
        return declared_value
    if published is not None:
        published_value = getattr(published.capabilities, name)
        if isinstance(published_value, bool):
            return published_value
    return default


def _model_infos(
    published: Mapping[str, TokenRhythmPublishedModel],
    declared: Mapping[str, TokenRhythmDeclaredModel],
    *,
    catalog: ModelCatalog | None = None,
    request: _TokenRhythmRequest | None = None,
) -> list[ModelInfo]:
    model_catalog = catalog if catalog is not None else shared_catalog()
    merged = merge_tokenrhythm_catalog(published, declared)
    compatibility_entries = tokenrhythm_published_catalog_entries(published)
    entry_by_id = {model_id.lower(): fields for model_id, fields in compatibility_entries.items()}
    infos: list[ModelInfo] = []
    for model_id, model in merged.items():
        declared_model = model.metadata.declared
        if declared_model is None:  # merge is entitlement-led; defensive only.
            continue
        published_model = model.metadata.published
        fields = entry_by_id.get(model_id.lower(), {})
        fallback_limits = model_catalog.resolve_deployment_limits(
            model_id,
            provider="tokenrhythm",
            api_key=request.api_key if request is not None else "",
            base_url=(
                request.base_url
                if request is not None
                else "https://tokenrhythm.studio/v1"
            ),
            proxy=request.proxy if request is not None else "",
        )
        safe_capabilities = model_catalog.resolve_deployment_capabilities(
            model_id,
            provider="tokenrhythm",
            api_key=request.api_key if request is not None else "",
            base_url=(
                request.base_url
                if request is not None
                else "https://tokenrhythm.studio/v1"
            ),
        )

        def positive(value: object) -> int | None:
            return (
                value
                if isinstance(value, int) and not isinstance(value, bool) and value > 0
                else None
            )

        context_window = (
            positive(declared_model.context_window)
            or positive(published_model.context_window if published_model else None)
            or fallback_limits.context_window
        )
        max_output_tokens = (
            positive(declared_model.max_output_tokens)
            or positive(published_model.max_output_tokens if published_model else None)
            or fallback_limits.max_output_tokens
        )
        declared_reasoning = _first_capability(
            declared_model, published_model, "reasoning"
        )
        declared_tools = _first_capability(declared_model, published_model, "tools")
        declared_streaming = _first_capability(
            declared_model, published_model, "streaming"
        )
        declared_vision = _first_capability(declared_model, published_model, "vision")
        infos.append(
            ModelInfo(
                provider="tokenrhythm",
                model_id=model_id,
                display_name=model.display_name,
                context_window=context_window,
                max_output_tokens=max_output_tokens,
                # A provider declaration can disable reasoning, but enabling a
                # request dialect remains a local executable-capability decision.
                supports_reasoning=(
                    False
                    if declared_reasoning is False
                    else safe_capabilities.supports_reasoning
                ),
                supports_tools=(
                    declared_tools
                    if declared_tools is not None
                    else safe_capabilities.supports_tools
                ),
                supports_streaming=(
                    declared_streaming
                    if declared_streaming is not None
                    else safe_capabilities.supports_streaming
                ),
                supports_vision=(
                    declared_vision
                    if declared_vision is not None
                    else safe_capabilities.supports_vision
                ),
                input_cost_per_1k=float(fields.get("input_cost_per_mtok") or 0.0)
                / 1000.0,
                output_cost_per_1k=float(fields.get("output_cost_per_mtok") or 0.0)
                / 1000.0,
                metadata=model.metadata.to_wire(),
            )
        )
    return infos


def _discovery_rows(infos: list[ModelInfo]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for info in infos:
        capabilities = ["chat"]
        if info.supports_tools:
            capabilities.append("tools")
        if info.supports_reasoning:
            capabilities.append("reasoning")
        if info.supports_vision:
            capabilities.append("vision")
        pricing: dict[str, float] | None = None
        if info.input_cost_per_1k > 0 or info.output_cost_per_1k > 0:
            pricing = {
                "inputPer1k": info.input_cost_per_1k,
                "outputPer1k": info.output_cost_per_1k,
            }
        rows.append(
            {
                "id": info.model_id,
                "name": info.display_name or info.model_id,
                "contextWindow": info.context_window,
                "maxOutputTokens": info.max_output_tokens,
                "capabilities": capabilities,
                "pricing": pricing,
                "capabilitySource": "live",
                "metadata": dict(info.metadata) if isinstance(info.metadata, dict) else None,
            }
        )
    return rows


class TokenRhythmCatalogCoordinator:
    """Coordinate TokenRhythm TTL, singleflight, LKG, persistence, and publication."""

    def __init__(
        self,
        catalog: ModelCatalog,
        *,
        clock: Any = time.time,
    ) -> None:
        self._catalog = catalog
        self._clock = clock
        self._published = _PublishedSnapshot()
        self._entitlements: dict[str, _EntitlementSnapshot] = {}
        self._ephemeral_entitlements: dict[str, _EntitlementSnapshot] = {}
        self._aligned_at: dict[str, float] = {}
        self._ephemeral_aligned_at: dict[str, float] = {}
        self._failures: dict[tuple[str, str], float] = {}
        self._last_declared_errors: dict[str, BaseException] = {}
        self._inflight: dict[tuple[str, str], asyncio.Task[_RefreshOutcome]] = {}
        # Source flights may outlive the operation that first awaited them
        # (callers use shield for singleflight). Keep a drain reference even
        # after an identity fence removes a task from ``_inflight`` so shutdown
        # can still await cancellation-resistant transports.
        self._source_drains: set[asyncio.Task[_RefreshOutcome]] = set()
        self._declared_transport_authorities: dict[str, str] = {}
        self._declared_operation_counts: dict[str, int] = {}
        self._transport_generations: dict[str, int] = {}
        self._operations: set[asyncio.Task[_CatalogView]] = set()
        self._operation_drains: set[asyncio.Future[None]] = set()
        self._write_tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._persist_lock = asyncio.Lock()
        self._active_authority = ""
        self._active_transport = ""
        self._published_projection_allowed = True
        self._generation = 0
        self._cleanup_epoch = 0
        self._hydrated_path: Path | None = None
        self._closed = False
        self._pending_persist = False
        self._persist_revision = 0

    async def hydrate(self, config: Any, *, activate: bool = True) -> None:
        """Load one normalized snapshot and expose only the active authority."""

        path = _state_path(config)
        already_hydrated = False
        should_persist = False
        async with self._lock:
            if self._closed:
                raise RuntimeError("TokenRhythm catalog coordinator is closed")
            if self._hydrated_path == path:
                if activate:
                    self._published_projection_allowed = (
                        _allows_tokenrhythm_published_projection(config)
                    )
                    request = _request_from_config(config)
                    self._activate_locked(
                        request,
                        clear_previous=_config_clears_tokenrhythm_credential(config),
                    )
                    self._prune_persisted_unreachable_locked(config)
                self._publish_active_locked()
                should_persist = self._pending_persist
                already_hydrated = True
            if not activate and self._hydrated_path is not None:
                # A draft is not allowed to switch the gateway's state root.
                return
        if already_hydrated:
            if should_persist:
                await self._persist_current()
            return
        payload = await asyncio.to_thread(_read_snapshot_file, path)
        async with self._lock:
            if self._closed:
                raise RuntimeError("TokenRhythm catalog coordinator is closed")
            if self._hydrated_path != path:
                self._generation += 1
                self._cancel_inflight_locked()
                self._active_authority = ""
                self._active_transport = ""
                self._published = _PublishedSnapshot()
                self._entitlements = {}
                self._ephemeral_entitlements = {}
                self._aligned_at = {}
                self._ephemeral_aligned_at = {}
                self._failures = {}
                self._last_declared_errors = {}
                self._declared_transport_authorities = {}
                self._transport_generations = {}
                self._pending_persist = False
                self._hydrated_path = path
                if payload is not None:
                    self._hydrate_payload_locked(payload)
            if activate:
                self._published_projection_allowed = (
                    _allows_tokenrhythm_published_projection(config)
                )
                request = _request_from_config(config)
                self._activate_locked(
                    request,
                    clear_previous=_config_clears_tokenrhythm_credential(config),
                )
                self._prune_persisted_unreachable_locked(config)
            self._publish_active_locked()
            should_persist = self._pending_persist
        if should_persist:
            await self._persist_current()

    def _hydrate_payload_locked(self, payload: Mapping[str, Any]) -> None:
        published = payload.get("published")
        if isinstance(published, Mapping):
            models = published.get("models")
            if isinstance(models, Mapping):
                self._published = _PublishedSnapshot(
                    models=tokenrhythm_published_from_wire(models),
                    success_at=_valid_timestamp(published.get("successAt")),
                    transport_fingerprint=str(
                        published.get("transportFingerprint") or ""
                    ),
                )
        entitlements = payload.get("entitlements")
        if isinstance(entitlements, Mapping):
            for authority, raw in entitlements.items():
                authority_s = str(authority)
                if len(authority_s) != 64 or any(
                    char not in "0123456789abcdef" for char in authority_s
                ):
                    continue
                if not isinstance(raw, Mapping) or not isinstance(
                    raw.get("models"), Mapping
                ):
                    continue
                self._entitlements[authority_s] = _EntitlementSnapshot(
                    authority_identity=authority_s,
                    models=tokenrhythm_declared_from_wire(raw["models"]),
                    success_at=_valid_timestamp(raw.get("successAt")),
                    transport_fingerprint=str(raw.get("transportFingerprint") or ""),
                )
                transport = self._entitlements[authority_s].transport_fingerprint
                if transport:
                    self._declared_transport_authorities[transport] = authority_s
        aligned = payload.get("lastAlignedAt")
        if isinstance(aligned, Mapping):
            for authority, raw_timestamp in aligned.items():
                timestamp = _valid_timestamp(raw_timestamp)
                if timestamp is not None and str(authority) in self._entitlements:
                    self._aligned_at[str(authority)] = timestamp

    def _state_payload_locked(self) -> dict[str, Any]:
        return {
            "schemaVersion": TOKENRHYTHM_SNAPSHOT_SCHEMA_VERSION,
            "published": {
                "successAt": self._published.success_at,
                "transportFingerprint": self._published.transport_fingerprint,
                "models": tokenrhythm_published_to_wire(self._published.models),
            },
            "entitlements": {
                authority: {
                    "successAt": snapshot.success_at,
                    "transportFingerprint": snapshot.transport_fingerprint,
                    "models": tokenrhythm_declared_to_wire(snapshot.models),
                }
                for authority, snapshot in self._entitlements.items()
            },
            "lastAlignedAt": dict(self._aligned_at),
        }

    def _mark_persist_pending_locked(self) -> None:
        """Record a newer snapshot revision that must reach durable storage."""

        self._persist_revision += 1
        self._pending_persist = True

    async def _persist_current(self) -> None:
        if self._hydrated_path is None or self._closed:
            return
        async with self._persist_lock:
            async with self._lock:
                if self._closed:
                    return
                path = self._hydrated_path
                payload = self._state_payload_locked()
                persist_revision = self._persist_revision
                if path is None:
                    return
                write_task = asyncio.create_task(
                    asyncio.to_thread(_write_snapshot_file, path, payload)
                )
                self._write_tasks.add(write_task)
                write_task.add_done_callback(self._write_tasks.discard)
            try:
                try:
                    await asyncio.shield(write_task)
                except asyncio.CancelledError:
                    # A worker thread cannot be cancelled safely.  Keep the
                    # persistence lock until the atomic replace has either
                    # completed or failed, then preserve caller cancellation.
                    await asyncio.gather(write_task, return_exceptions=True)
                    raise
                async with self._lock:
                    if self._persist_revision == persist_revision:
                        self._pending_persist = False
            except Exception:  # noqa: BLE001 - persistence degrades to memory state
                log.warning("tokenrhythm_catalog.snapshot_write_failed", exc_info=True)

    async def _flush_pending_snapshot_on_close(self) -> None:
        """Flush the final dirty snapshot after new mutations have been fenced."""

        async with self._persist_lock:
            async with self._lock:
                path = self._hydrated_path
                if not self._pending_persist or path is None:
                    return
                payload = self._state_payload_locked()
                persist_revision = self._persist_revision
                write_task = asyncio.create_task(
                    asyncio.to_thread(_write_snapshot_file, path, payload)
                )
            try:
                try:
                    await asyncio.shield(write_task)
                except asyncio.CancelledError:
                    # Preserve the same cancellation-safe atomic-write contract
                    # as ordinary persistence: the worker must finish before
                    # shutdown cancellation can escape.
                    await asyncio.gather(write_task, return_exceptions=True)
                    raise
                async with self._lock:
                    if self._persist_revision == persist_revision:
                        self._pending_persist = False
            except Exception:  # noqa: BLE001 - persistence degrades to memory state
                log.warning("tokenrhythm_catalog.snapshot_write_failed", exc_info=True)

    def _cancel_inflight_locked(self) -> None:
        for task in self._inflight.values():
            task.cancel()
        self._inflight.clear()

    def _fence_declared_transports_locked(self, transports: set[str]) -> None:
        """Fence selected authenticated flights without disturbing public refreshes."""

        for transport in transports:
            if not transport:
                continue
            self._transport_generations[transport] = (
                self._transport_generations.get(transport, 0) + 1
            )
            task = self._inflight.pop(("declared", transport), None)
            if task is not None:
                task.cancel()

    def _prune_persisted_unreachable_locked(self, config: Any) -> bool:
        """Drop authorities no longer reachable from durable config and fence flights."""

        reachable = set(_configured_tokenrhythm_requests(config))
        known = (
            set(self._entitlements)
            | set(self._ephemeral_entitlements)
            | set(self._declared_transport_authorities.values())
        )
        removed = known - reachable
        if not removed:
            return False
        self._cleanup_epoch += 1
        transports: set[str] = set()
        persisted_changed = False
        for authority in removed:
            persisted = self._entitlements.pop(authority, None)
            ephemeral = self._ephemeral_entitlements.pop(authority, None)
            aligned = self._aligned_at.pop(authority, None)
            self._ephemeral_aligned_at.pop(authority, None)
            self._last_declared_errors.pop(authority, None)
            persisted_changed = persisted_changed or persisted is not None or aligned is not None
            for snapshot in (persisted, ephemeral):
                if snapshot is not None and snapshot.transport_fingerprint:
                    transports.add(snapshot.transport_fingerprint)
            transports.update(
                transport
                for transport, mapped_authority in self._declared_transport_authorities.items()
                if mapped_authority == authority
            )
        self._fence_declared_transports_locked(transports)
        for transport in transports:
            self._failures.pop((transport, "declared"), None)
            self._declared_transport_authorities.pop(transport, None)
        if persisted_changed:
            self._mark_persist_pending_locked()
        return True

    def _activate_locked(
        self,
        request: _TokenRhythmRequest | None,
        *,
        clear_previous: bool = False,
    ) -> bool:
        authority = request.authority_identity if request is not None else ""
        transport = request.transport_fingerprint if request is not None else ""
        changed = authority != self._active_authority or transport != self._active_transport
        if not changed:
            return False
        previous_authority = self._active_authority
        self._generation += 1
        self._active_authority = authority
        self._active_transport = transport
        self._cancel_inflight_locked()
        if clear_previous and not authority and previous_authority:
            # Explicit credential clear removes that authority's entitlement;
            # the keyless public snapshot remains reusable.
            self._cleanup_epoch += 1
            self._entitlements.pop(previous_authority, None)
            self._ephemeral_entitlements.pop(previous_authority, None)
            self._aligned_at.pop(previous_authority, None)
            self._ephemeral_aligned_at.pop(previous_authority, None)
            self._last_declared_errors.pop(previous_authority, None)
            self._mark_persist_pending_locked()
        return True

    def _publish_active_locked(self) -> None:
        self._sync_catalog_sidecars_locked()
        if not self._published_projection_allowed:
            # A provider id alone is not an endpoint identity. The official
            # website snapshot may remain as LKG for a later switch back, but
            # it must not influence runtime limits/capabilities for an
            # operator-supplied origin using the TokenRhythm adapter.
            self._catalog.set_live_provider_entries("tokenrhythm", {})
            self._catalog.set_provider_model_metadata("tokenrhythm", {})
            return
        declared: Mapping[str, TokenRhythmDeclaredModel] = {}
        if self._active_authority:
            entitlement = self._entitlements.get(self._active_authority)
            if entitlement is not None:
                declared = entitlement.models
        entries = tokenrhythm_published_catalog_entries(self._published.models)
        self._catalog.set_live_provider_entries("tokenrhythm", entries)
        metadata: dict[str, TokenRhythmModelMetadata] = {
            model_id: TokenRhythmModelMetadata(published=model, declared=None)
            for model_id, model in self._published.models.items()
        }
        by_lower = {model_id.lower(): model_id for model_id in metadata}
        for model_id, declared_model in declared.items():
            published_key = by_lower.get(model_id.lower())
            published_model = (
                self._published.models.get(published_key) if published_key is not None else None
            )
            metadata[model_id] = TokenRhythmModelMetadata(
                published=published_model,
                declared=declared_model,
            )
        self._catalog.set_provider_model_metadata("tokenrhythm", metadata)

    def _sync_catalog_sidecars_locked(self) -> None:
        """Publish one atomic normalized snapshot for deployment-aware reads."""

        self._catalog.set_tokenrhythm_snapshot_sidecars(
            published=self._published.models,
            declared_by_authority={
                authority: snapshot.models
                for authority, snapshot in self._entitlements.items()
            },
        )

    def _entitlement_locked(
        self, authority: str
    ) -> _EntitlementSnapshot | None:
        return self._entitlements.get(authority) or self._ephemeral_entitlements.get(
            authority
        )

    def _source_needs_refresh_locked(
        self,
        *,
        source: str,
        success_at: float | None,
        expected_transport: str,
        actual_transport: str,
        force: bool,
        now: float,
    ) -> bool:
        if force:
            return True
        # A wall-clock rollback (or a persisted timestamp from the future) is
        # stale, not a success newer than the current failed attempt.  Treat it
        # as absent so the normal five-minute retry backoff still applies.
        comparable_success_at = (
            success_at if success_at is not None and success_at <= now else None
        )
        failure_key = (expected_transport, source)
        failed_at = self._failures.get(failure_key)
        if failed_at is not None and failed_at > now:
            # A wall-clock rollback invalidates an in-memory failure age just
            # like a future persisted success timestamp. It must trigger an
            # immediate retry, not extend the five-minute backoff until the
            # clock catches up with the old value.
            self._failures.pop(failure_key, None)
            failed_at = None
        failure_is_newer = failed_at is not None and (
            comparable_success_at is None or failed_at >= comparable_success_at
        )
        if failure_is_newer and failed_at is not None:
            return now - failed_at >= TOKENRHYTHM_FAILURE_BACKOFF_SECONDS
        if _is_fresh(success_at, now) and expected_transport == actual_transport:
            return False
        return True

    async def _fetch_source(
        self,
        request: _TokenRhythmRequest,
        source: str,
    ) -> _RefreshOutcome:
        """Fetch one independently singleflighted source with its hard timeout."""

        try:
            if source == "published":
                published = await asyncio.wait_for(
                    fetch_tokenrhythm_published(
                        proxy=request.proxy,
                        timeout=TOKENRHYTHM_PUBLIC_TIMEOUT_SECONDS,
                    ),
                    timeout=TOKENRHYTHM_PUBLIC_TIMEOUT_SECONDS,
                )
                return _RefreshOutcome(
                    completed_at=float(self._clock()),
                    published=dict(published),
                )
            declared = await asyncio.wait_for(
                fetch_tokenrhythm_declared(
                    request.base_url,
                    api_key=request.api_key,
                    proxy=request.proxy,
                    timeout=TOKENRHYTHM_AUTH_TIMEOUT_SECONDS,
                ),
                timeout=TOKENRHYTHM_AUTH_TIMEOUT_SECONDS,
            )
            return _RefreshOutcome(
                completed_at=float(self._clock()),
                declared=dict(declared),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - normalized below, never logged raw
            if source == "published":
                return _RefreshOutcome(
                    completed_at=float(self._clock()),
                    published_error=error,
                )
            return _RefreshOutcome(
                completed_at=float(self._clock()),
                declared_error=error,
            )

    def _source_task_locked(
        self,
        request: _TokenRhythmRequest,
        source: str,
    ) -> tuple[tuple[str, str], asyncio.Task[_RefreshOutcome]]:
        transport = (
            request.public_transport_fingerprint
            if source == "published"
            else request.transport_fingerprint
        )
        key = (source, transport)
        if source == "declared":
            self._declared_transport_authorities[transport] = (
                request.authority_identity
            )
        task = self._inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._fetch_source(request, source))
            self._inflight[key] = task
            self._source_drains.add(task)
            task.add_done_callback(self._source_drains.discard)
        return key, task

    async def _await_sources(
        self,
        published_task: asyncio.Task[_RefreshOutcome] | None,
        declared_task: asyncio.Task[_RefreshOutcome] | None,
    ) -> _RefreshOutcome:
        """Await source flights concurrently under the shared ten-second deadline."""

        tasks = [
            task for task in (published_task, declared_task) if task is not None
        ]
        waiters = [asyncio.shield(task) for task in tasks]
        try:
            await asyncio.wait_for(
                asyncio.gather(*waiters, return_exceptions=True),
                timeout=TOKENRHYTHM_REFRESH_DEADLINE_SECONDS,
            )
        except TimeoutError:
            # Source tasks are shared by all callers. The request itself has
            # reached its hard deadline, so cancel unfinished network work for
            # every waiter and normalize it as a timeout outcome below.  Do
            # not await cancellation here: an HTTP transport may delay or
            # suppress cancellation, and the request deadline must remain a
            # real upper bound. ``_source_drains`` retains those tasks so
            # service shutdown can still cancel and await them safely.
            for task in tasks:
                if not task.done():
                    task.cancel()

        deadline_error = TimeoutError("TokenRhythm catalog deadline exceeded")

        def source_outcome(
            task: asyncio.Task[_RefreshOutcome] | None,
        ) -> _RefreshOutcome | None:
            if task is None:
                return None
            if not task.done() or task.cancelled():
                return _RefreshOutcome(
                    completed_at=float(self._clock()),
                    published_error=deadline_error,
                    declared_error=deadline_error,
                )
            try:
                return task.result()
            except BaseException as error:  # cancelled/failed task, normalized by source
                return _RefreshOutcome(
                    completed_at=float(self._clock()),
                    published_error=error,
                    declared_error=error,
                )

        published_outcome = source_outcome(published_task)
        declared_outcome = source_outcome(declared_task)
        return _RefreshOutcome(
            completed_at=float(self._clock()),
            published=(
                published_outcome.published
                if published_outcome is not None
                else None
            ),
            published_error=(
                published_outcome.published_error
                if published_outcome is not None
                else None
            ),
            declared=(
                declared_outcome.declared if declared_outcome is not None else None
            ),
            declared_error=(
                declared_outcome.declared_error
                if declared_outcome is not None
                else None
            ),
        )

    def _catalog_status_locked(
        self, request: _TokenRhythmRequest, *, now: float
    ) -> dict[str, object]:
        entitlement = self._entitlement_locked(request.authority_identity)
        public_fresh = _is_fresh(self._published.success_at, now) and (
            self._published.transport_fingerprint
            == request.public_transport_fingerprint
        )
        public_failure_at = self._failures.get(
            (request.public_transport_fingerprint, "published")
        )
        if (
            public_failure_at is not None
            and (
                self._published.success_at is None
                or public_failure_at >= self._published.success_at
            )
        ):
            public_fresh = False
        auth_fresh = entitlement is not None and _is_fresh(
            entitlement.success_at, now
        ) and entitlement.transport_fingerprint == request.transport_fingerprint
        auth_failure_at = self._failures.get(
            (request.transport_fingerprint, "declared")
        )
        if (
            auth_failure_at is not None
            and (
                entitlement is None
                or entitlement.success_at is None
                or auth_failure_at >= entitlement.success_at
            )
        ):
            auth_fresh = False
        aligned_at = self._aligned_at.get(
            request.authority_identity,
            self._ephemeral_aligned_at.get(request.authority_identity),
        )
        return {
            "lastSyncedAt": _iso_utc(aligned_at, now),
            "stale": not (public_fresh and auth_fresh),
        }

    def _view_locked(
        self,
        request: _TokenRhythmRequest,
        *,
        now: float,
        transient_declared: Mapping[str, TokenRhythmDeclaredModel] | None = None,
        declared_error: BaseException | None = None,
    ) -> _CatalogView:
        entitlement = self._entitlement_locked(request.authority_identity)
        declared = (
            dict(transient_declared)
            if transient_declared is not None
            else dict(entitlement.models) if entitlement is not None else {}
        )
        declared_available = transient_declared is not None or entitlement is not None
        error = declared_error or self._last_declared_errors.get(
            request.authority_identity
        )
        return _CatalogView(
            published=dict(self._published.models),
            declared=declared,
            catalog=self._catalog_status_locked(request, now=now),
            declared_available=declared_available,
            declared_error=error,
        )

    async def refresh(
        self,
        request: _TokenRhythmRequest,
        *,
        force: bool = False,
        persist_entitlement: bool = True,
        activate: bool = True,
    ) -> _CatalogView:
        """Run one tracked refresh operation owned by this coordinator."""

        async with self._lock:
            if self._closed:
                raise RuntimeError("TokenRhythm catalog coordinator is closed")
            operation_generation = self._generation
            operation_cleanup_epoch = self._cleanup_epoch
            operation_transport_generation = self._transport_generations.get(
                request.transport_fingerprint, 0
            )
            # Register the authority before the child has a chance to enter
            # ``_refresh_impl``. A concurrent credential clear can therefore
            # fence even an operation that has not created its source task yet.
            self._declared_transport_authorities[
                request.transport_fingerprint
            ] = request.authority_identity
            self._declared_operation_counts[request.transport_fingerprint] = (
                self._declared_operation_counts.get(
                    request.transport_fingerprint, 0
                )
                + 1
            )
            # Create the child only after the closed check and register it in
            # the same critical section.  Cancellation while waiting for this
            # lock therefore cannot leave an untracked refresh behind.
            operation = asyncio.create_task(
                self._refresh_impl(
                    request,
                    force=force,
                    persist_entitlement=persist_entitlement,
                    activate=activate,
                    operation_generation=operation_generation,
                    operation_cleanup_epoch=operation_cleanup_epoch,
                    operation_transport_generation=(
                        operation_transport_generation
                    ),
                )
            )
            drained: asyncio.Future[None] = (
                asyncio.get_running_loop().create_future()
            )
            self._operations.add(operation)
            self._operation_drains.add(drained)
        try:
            return await operation
        finally:
            async with self._lock:
                self._operations.discard(operation)
                self._operation_drains.discard(drained)
                remaining = (
                    self._declared_operation_counts.get(
                        request.transport_fingerprint, 1
                    )
                    - 1
                )
                if remaining > 0:
                    self._declared_operation_counts[
                        request.transport_fingerprint
                    ] = remaining
                else:
                    self._declared_operation_counts.pop(
                        request.transport_fingerprint, None
                    )
                    has_authority_state = (
                        request.authority_identity in self._entitlements
                        or request.authority_identity in self._ephemeral_entitlements
                        or request.authority_identity in self._last_declared_errors
                        or (
                            request.transport_fingerprint,
                            "declared",
                        )
                        in self._failures
                    )
                    if (
                        not has_authority_state
                        and self._declared_transport_authorities.get(
                            request.transport_fingerprint
                        )
                        == request.authority_identity
                    ):
                        source_task = self._inflight.pop(
                            ("declared", request.transport_fingerprint),
                            None,
                        )
                        if source_task is not None and not source_task.done():
                            source_task.cancel()
                        self._declared_transport_authorities.pop(
                            request.transport_fingerprint, None
                        )
                if not drained.done():
                    drained.set_result(None)

    async def _refresh_impl(
        self,
        request: _TokenRhythmRequest,
        *,
        force: bool = False,
        persist_entitlement: bool = True,
        activate: bool = True,
        operation_generation: int,
        operation_cleanup_epoch: int,
        operation_transport_generation: int,
    ) -> _CatalogView:
        """Refresh stale sources and return an authority-scoped last-good view."""

        if self._closed:
            raise RuntimeError("TokenRhythm catalog coordinator is closed")
        async with self._lock:
            if self._closed:
                raise RuntimeError("TokenRhythm catalog coordinator is closed")
            if (
                operation_generation != self._generation
                or operation_cleanup_epoch != self._cleanup_epoch
                or operation_transport_generation
                != self._transport_generations.get(
                    request.transport_fingerprint, 0
                )
            ):
                return self._view_locked(request, now=float(self._clock()))
            if activate:
                self._activate_locked(request)
                self._publish_active_locked()
            generation = self._generation
            cleanup_epoch = self._cleanup_epoch
            transport_generation = self._transport_generations.get(
                request.transport_fingerprint, 0
            )
            now = float(self._clock())
            if (
                self._published.success_at is not None
                and self._published.success_at > now
            ):
                # Once observed in the future, a wall-clock timestamp is not
                # allowed to become fresh merely because time later catches
                # up. Keep the normalized LKG but discard its invalid age.
                self._published.success_at = None
            promoted_ephemeral = False
            if persist_entitlement and request.authority_identity not in self._entitlements:
                ephemeral = self._ephemeral_entitlements.pop(
                    request.authority_identity, None
                )
                if ephemeral is not None:
                    self._entitlements[request.authority_identity] = ephemeral
                    ephemeral_aligned = self._ephemeral_aligned_at.pop(
                        request.authority_identity, None
                    )
                    if ephemeral_aligned is not None:
                        self._aligned_at[request.authority_identity] = ephemeral_aligned
                    promoted_ephemeral = True
                    self._mark_persist_pending_locked()
                    if activate:
                        self._publish_active_locked()
            if promoted_ephemeral and not activate:
                self._sync_catalog_sidecars_locked()
            entitlement = self._entitlement_locked(request.authority_identity)
            if (
                entitlement is not None
                and entitlement.success_at is not None
                and entitlement.success_at > now
            ):
                entitlement.success_at = None
            aligned_at = self._aligned_at.get(request.authority_identity)
            if aligned_at is not None and aligned_at > now:
                self._aligned_at.pop(request.authority_identity, None)
            ephemeral_aligned_at = self._ephemeral_aligned_at.get(
                request.authority_identity
            )
            if ephemeral_aligned_at is not None and ephemeral_aligned_at > now:
                self._ephemeral_aligned_at.pop(request.authority_identity, None)
            fetch_published = self._source_needs_refresh_locked(
                source="published",
                success_at=self._published.success_at,
                expected_transport=request.public_transport_fingerprint,
                actual_transport=self._published.transport_fingerprint,
                force=force,
                now=now,
            )
            fetch_declared = self._source_needs_refresh_locked(
                source="declared",
                success_at=entitlement.success_at if entitlement is not None else None,
                expected_transport=request.transport_fingerprint,
                actual_transport=(
                    entitlement.transport_fingerprint if entitlement is not None else ""
                ),
                force=force,
                now=now,
            )
            if not fetch_published and not fetch_declared:
                immediate_view = self._view_locked(request, now=now)
                immediate_persist = promoted_ephemeral
            else:
                immediate_view = None
                immediate_persist = False
            if immediate_view is not None:
                published_key = None
                published_task = None
                declared_key = None
                declared_task = None
            else:
                if fetch_published:
                    published_key, published_task = self._source_task_locked(
                        request,
                        "published",
                    )
                else:
                    published_key = None
                    published_task = None
                if fetch_declared:
                    declared_key, declared_task = self._source_task_locked(
                        request,
                        "declared",
                    )
                else:
                    declared_key = None
                    declared_task = None
        if immediate_view is not None:
            if immediate_persist:
                await self._persist_current()
            return immediate_view
        assert published_task is not None or declared_task is not None
        try:
            outcome = await self._await_sources(published_task, declared_task)
        except asyncio.CancelledError:
            # Identity changes deliberately cancel the superseded generation.
            # Preserve external cancellation semantics when this task itself
            # was not fenced by a newer active request.
            async with self._lock:
                fenced = generation != self._generation or (
                    cleanup_epoch != self._cleanup_epoch
                ) or (
                    transport_generation
                    != self._transport_generations.get(
                        request.transport_fingerprint, 0
                    )
                ) or (
                    activate
                    and (
                        request.authority_identity != self._active_authority
                        or request.transport_fingerprint != self._active_transport
                    )
                )
                if fenced:
                    return self._view_locked(
                        request,
                        now=float(self._clock()),
                    )
            raise

        should_persist = promoted_ephemeral
        transient_declared: Mapping[str, TokenRhythmDeclaredModel] | None = None
        async with self._lock:
            for key, task in (
                (published_key, published_task),
                (declared_key, declared_task),
            ):
                if (
                    key is not None
                    and task is not None
                    and task.done()
                    and self._inflight.get(key) is task
                ):
                    self._inflight.pop(key, None)
            if generation != self._generation or (
                cleanup_epoch != self._cleanup_epoch
            ) or (
                transport_generation
                != self._transport_generations.get(
                    request.transport_fingerprint, 0
                )
            ) or (
                activate
                and (
                    request.authority_identity != self._active_authority
                    or request.transport_fingerprint != self._active_transport
                )
            ):
                # A key/base/proxy change fenced this request while it was in flight.
                return self._view_locked(request, now=float(self._clock()))

            if fetch_published:
                if outcome.published is not None:
                    self._published = _PublishedSnapshot(
                        models=outcome.published,
                        success_at=outcome.completed_at,
                        transport_fingerprint=request.public_transport_fingerprint,
                    )
                    self._failures.pop(
                        (request.public_transport_fingerprint, "published"), None
                    )
                    should_persist = True
                elif outcome.published_error is not None:
                    self._failures[
                        (request.public_transport_fingerprint, "published")
                    ] = outcome.completed_at
                    log.warning(
                        "tokenrhythm_catalog.refresh_failed",
                        source="published",
                        failure_kind=_error_failure_kind(outcome.published_error),
                    )

            if fetch_declared:
                if outcome.declared is not None:
                    transient_declared = outcome.declared
                    self._last_declared_errors.pop(request.authority_identity, None)
                    self._failures.pop(
                        (request.transport_fingerprint, "declared"), None
                    )
                    if persist_entitlement:
                        self._entitlements[request.authority_identity] = (
                            _EntitlementSnapshot(
                                authority_identity=request.authority_identity,
                                models=outcome.declared,
                                success_at=outcome.completed_at,
                                transport_fingerprint=request.transport_fingerprint,
                            )
                        )
                        self._ephemeral_entitlements.pop(
                            request.authority_identity, None
                        )
                        self._ephemeral_aligned_at.pop(
                            request.authority_identity, None
                        )
                        should_persist = True
                    else:
                        self._ephemeral_entitlements[request.authority_identity] = (
                            _EntitlementSnapshot(
                                authority_identity=request.authority_identity,
                                models=outcome.declared,
                                success_at=outcome.completed_at,
                                transport_fingerprint=request.transport_fingerprint,
                            )
                        )
                elif outcome.declared_error is not None:
                    self._last_declared_errors[
                        request.authority_identity
                    ] = outcome.declared_error
                    self._failures[
                        (request.transport_fingerprint, "declared")
                    ] = outcome.completed_at
                    log.warning(
                        "tokenrhythm_catalog.refresh_failed",
                        source="declared",
                        failure_kind=_error_failure_kind(outcome.declared_error),
                    )

            current_entitlement = self._entitlement_locked(
                request.authority_identity
            )
            public_fresh = _is_fresh(
                self._published.success_at, outcome.completed_at
            ) and (
                self._published.transport_fingerprint
                == request.public_transport_fingerprint
            ) and outcome.published_error is None
            public_failure_at = self._failures.get(
                (request.public_transport_fingerprint, "published")
            )
            if (
                public_failure_at is not None
                and (
                    self._published.success_at is None
                    or public_failure_at >= self._published.success_at
                )
            ):
                public_fresh = False
            auth_fresh = current_entitlement is not None and _is_fresh(
                current_entitlement.success_at, outcome.completed_at
            ) and (
                current_entitlement.transport_fingerprint
                == request.transport_fingerprint
            ) and outcome.declared_error is None
            auth_failure_at = self._failures.get(
                (request.transport_fingerprint, "declared")
            )
            if (
                auth_failure_at is not None
                and (
                    current_entitlement is None
                    or current_entitlement.success_at is None
                    or auth_failure_at >= current_entitlement.success_at
                )
            ):
                auth_fresh = False
            if public_fresh and auth_fresh:
                if persist_entitlement:
                    self._aligned_at[request.authority_identity] = outcome.completed_at
                    should_persist = True
                else:
                    self._ephemeral_aligned_at[
                        request.authority_identity
                    ] = outcome.completed_at
            # Public facts are key-independent and should update the current
            # active compatibility projection even when this refresh belongs
            # to a saved/draft non-active profile. _publish_active_locked uses
            # only the persisted active authority; ephemeral declarations are
            # never projected or installed in the runtime sidecar.
            self._publish_active_locked()
            view = self._view_locked(
                request,
                now=outcome.completed_at,
                transient_declared=(
                    transient_declared if not persist_entitlement else None
                ),
                declared_error=outcome.declared_error,
            )
            if (
                not persist_entitlement
                and transient_declared is not None
                and public_fresh
            ):
                view = _CatalogView(
                    published=view.published,
                    declared=view.declared,
                    catalog={
                        "lastSyncedAt": _iso_utc(
                            outcome.completed_at, outcome.completed_at
                        ),
                        "stale": False,
                    },
                    declared_available=True,
                    declared_error=view.declared_error,
                )
            if should_persist:
                self._mark_persist_pending_locked()

        if should_persist:
            await self._persist_current()
        return view

    async def refresh_active(
        self, config: Any, *, force: bool = False
    ) -> dict[str, int]:
        """Apply an active config transition and refresh without blocking callers forever."""

        await self.hydrate(config)
        request = _request_from_config(config)
        if request is None:
            async with self._lock:
                if self._closed:
                    raise RuntimeError("TokenRhythm catalog coordinator is closed")
                self._activate_locked(
                    None,
                    clear_previous=_config_clears_tokenrhythm_credential(config),
                )
                self._publish_active_locked()
                should_persist = self._pending_persist
            if should_persist:
                await self._persist_current()
            return {}
        view = await self.refresh(
            request,
            force=force,
            persist_entitlement=True,
            activate=True,
        )
        return {"tokenrhythm": len(view.declared)}

    async def reconcile_profile_transition(
        self,
        previous_config: Any,
        current_config: Any,
        *,
        provider_id: str,
    ) -> None:
        """Apply a durable active/profile authority transition without network.

        TokenRhythm has at most one stored profile (the provider id is the
        profile key), so an identity-changing save or active-provider removal
        can safely discard every authority not reachable from the new config.
        This also handles an environment variable removed before cleanup: the
        old persisted authority is intentionally opaque and no longer reversible.
        """

        if str(provider_id or "").strip().lower() != "tokenrhythm":
            return
        await self.hydrate(previous_config, activate=False)
        previous_identity = _profile_identity_fingerprint(
            previous_config, provider_id
        )
        current_identity = _profile_identity_fingerprint(current_config, provider_id)
        previous_active = _request_from_config(previous_config)
        current_active = _request_from_config(current_config)
        previous_active_authority = (
            previous_active.authority_identity if previous_active is not None else ""
        )
        current_active_authority = (
            current_active.authority_identity if current_active is not None else ""
        )
        identity_changed = (
            previous_identity != current_identity
            or previous_active_authority != current_active_authority
        )
        previous_proxy = _profile_proxy(previous_config, provider_id)
        current_proxy = _profile_proxy(current_config, provider_id)
        proxy_changed = previous_proxy != current_proxy
        if not identity_changed and not proxy_changed:
            return

        current_requests = _configured_tokenrhythm_requests(current_config)
        active_request = current_active
        protected_authorities = {
            request.authority_identity
            for request in (active_request,)
            if request is not None
        }
        protected_transports = {
            request.transport_fingerprint
            for request in (active_request,)
            if request is not None
        }
        should_persist = False
        async with self._lock:
            if self._closed:
                return
            self._published_projection_allowed = (
                _allows_tokenrhythm_published_projection(current_config)
            )
            transports_to_fence: set[str] = set()
            if identity_changed:
                keep_authorities = set(current_requests) | protected_authorities
                known_authorities = (
                    set(self._entitlements)
                    | set(self._ephemeral_entitlements)
                    | set(self._declared_transport_authorities.values())
                )
                removed_authorities = known_authorities - keep_authorities
                if removed_authorities:
                    self._cleanup_epoch += 1
                for authority in removed_authorities:
                    persisted = self._entitlements.pop(authority, None)
                    ephemeral = self._ephemeral_entitlements.pop(authority, None)
                    self._aligned_at.pop(authority, None)
                    self._ephemeral_aligned_at.pop(authority, None)
                    self._last_declared_errors.pop(authority, None)
                    should_persist = should_persist or persisted is not None
                    for snapshot in (persisted, ephemeral):
                        if snapshot is not None and snapshot.transport_fingerprint:
                            transports_to_fence.add(snapshot.transport_fingerprint)
                    transports_to_fence.update(
                        transport
                        for transport, mapped_authority in (
                            self._declared_transport_authorities.items()
                        )
                        if mapped_authority == authority
                    )
            elif proxy_changed:
                known_authorities = (
                    set(self._entitlements)
                    | set(self._ephemeral_entitlements)
                    | set(self._declared_transport_authorities.values())
                    | set(_profile_requests(previous_config, provider_id))
                )
                transports_to_fence.update(
                    tokenrhythm_transport_fingerprint(
                        authority,
                        proxy=previous_proxy,
                    )
                    for authority in known_authorities
                )

            transports_to_fence.difference_update(protected_transports)
            self._fence_declared_transports_locked(transports_to_fence)
            for transport in transports_to_fence:
                self._failures.pop((transport, "declared"), None)
                self._declared_transport_authorities.pop(transport, None)
            if should_persist:
                self._mark_persist_pending_locked()
            self._publish_active_locked()

        if should_persist:
            await self._persist_current()

    def cached(self, config: Any) -> list[ModelInfo]:
        """Return the active authority's LKG without scheduling any network work."""

        request = _request_from_config(config)
        if request is None:
            return []
        entitlement = self._entitlements.get(request.authority_identity)
        if entitlement is None:
            return []
        return _model_infos(
            self._published.models,
            entitlement.models,
            catalog=self._catalog,
            request=request,
        )

    async def discover(
        self,
        request: _TokenRhythmRequest,
        *,
        force: bool,
        persist_entitlement: bool,
        activate: bool,
    ) -> _CatalogView:
        return await self.refresh(
            request,
            force=force,
            persist_entitlement=persist_entitlement,
            activate=activate,
        )

    async def close(self) -> None:
        """Cancel and await all refresh work; safe to call more than once."""

        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            source_drains = list(self._source_drains)
            operations = list(self._operations)
            drains = list(self._operation_drains)
            writes = list(self._write_tasks)
            self._cancel_inflight_locked()
        if source_drains:
            await asyncio.gather(*source_drains, return_exceptions=True)
        if operations:
            await asyncio.gather(*operations, return_exceptions=True)
        if drains:
            await asyncio.gather(*drains, return_exceptions=True)
        if writes:
            await asyncio.gather(*writes, return_exceptions=True)
        # A cleanup may have committed its in-memory deletion and then been
        # cancelled while waiting for an older atomic write. Once closed, no
        # new mutation can race this final revision, so flush it before return.
        await self._flush_pending_snapshot_on_close()


_coordinator: TokenRhythmCatalogCoordinator | None = None


def install_tokenrhythm_catalog_coordinator(
    coordinator: TokenRhythmCatalogCoordinator | None,
) -> None:
    """Install the coordinator owned by the current gateway service container."""

    global _coordinator
    _coordinator = coordinator


def _current_coordinator(
    catalog: ModelCatalog | None = None,
) -> TokenRhythmCatalogCoordinator:
    global _coordinator
    if _coordinator is None or (
        catalog is not None and _coordinator._catalog is not catalog
    ):
        _coordinator = TokenRhythmCatalogCoordinator(
            catalog if catalog is not None else shared_catalog()
        )
    return _coordinator


async def refresh_live_model_catalog(
    config: Any,
    *,
    catalog: ModelCatalog | None = None,
    force: bool = False,
) -> dict[str, int]:
    """Best-effort refresh of the active provider's model metadata."""

    if config is None:
        return {}
    try:
        runtime = _runtime_config(config)
        if _coordinator is not None and runtime.provider != "tokenrhythm":
            # Provider switches must immediately stop exposing the previous
            # TokenRhythm entitlement even though the next provider has an
            # unrelated refresh mechanism.
            await _coordinator.refresh_active(config)
        try:
            spec = get_provider_spec(runtime.provider)
        except UnknownProviderError:
            return {}
        if not (spec.live_catalog_url and spec.live_catalog_shape):
            return {}
        target = catalog if catalog is not None else shared_catalog()
        if spec.live_catalog_shape == "tokenrhythm":
            coordinator = _current_coordinator(target)
            return await coordinator.refresh_active(config, force=force)
        if not runtime.api_key:
            return {}
        return await asyncio.wait_for(
            warm_live_provider_catalogs(target, [runtime.provider], proxy=runtime.proxy),
            timeout=LIVE_CATALOG_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - live metadata is always best-effort
        log.warning(
            "gateway.live_catalog_refresh_failed",
            provider=str(getattr(getattr(config, "llm", None), "provider", "") or ""),
            exc_info=True,
        )
        return {}


async def refresh_live_model_catalog_if_changed(
    previous: LiveCatalogRefreshFingerprint,
    config: Any,
    *,
    catalog: ModelCatalog | None = None,
) -> dict[str, int]:
    """Refresh when authority or transport changed; proxy changes bypass TTL."""

    current = live_catalog_refresh_fingerprint(config)
    if previous == current:
        return {}
    # A credential/base authority change naturally has no matching auth LKG,
    # while the key-independent public snapshot may remain fresh. Only a
    # transport change within the same authority (normally the proxy) bypasses
    # both source TTLs.
    force = bool(
        previous[1]
        and previous[1] == current[1]
        and previous[2]
        and current[2]
        and previous[2] != current[2]
    )
    return await refresh_live_model_catalog(config, catalog=catalog, force=force)


async def reconcile_tokenrhythm_profile_transition(
    previous_config: Any,
    current_config: Any,
    *,
    provider_id: str,
) -> None:
    """Best-effort local cleanup for one durably saved profile transition."""

    if str(provider_id or "").strip().lower() != "tokenrhythm":
        return
    try:
        coordinator = _current_coordinator()
        await coordinator.reconcile_profile_transition(
            previous_config,
            current_config,
            provider_id=provider_id,
        )
    except Exception:  # noqa: BLE001 - config commit remains authoritative
        log.warning(
            "tokenrhythm_catalog.profile_reconcile_failed",
            provider="tokenrhythm",
            exc_info=True,
        )


async def discover_tokenrhythm_models(
    *,
    provider_id: str,
    api_key: str,
    base_url: str,
    proxy: str = "",
    force: bool = False,
    persist_entitlement: bool = False,
    config: object | None = None,
) -> ProviderModelsDiscoverResult:
    """Admin discovery entry point returning the additive onboarding contract."""

    from opensquilla.onboarding.probe import ProviderModelsDiscoverResult

    request = _tokenrhythm_request(
        provider=provider_id,
        base_url=base_url,
        api_key=api_key,
        proxy=proxy,
    )
    if request is None:
        return ProviderModelsDiscoverResult(ok=True, provider_id=provider_id)
    coordinator = _current_coordinator()
    if config is not None:
        await coordinator.hydrate(config, activate=persist_entitlement)
    active_request = _request_from_config(config) if config is not None else None
    activate = bool(
        persist_entitlement
        and active_request is not None
        and active_request.authority_identity == request.authority_identity
    )
    try:
        view = await coordinator.discover(
            request,
            force=force,
            persist_entitlement=persist_entitlement,
            activate=activate,
        )
    except Exception as error:  # noqa: BLE001 - discovery returns typed failure
        return ProviderModelsDiscoverResult(
            ok=False,
            provider_id=provider_id,
            failure_kind=_error_failure_kind(error),
            detail="TokenRhythm model catalog refresh failed.",
            catalog={"lastSyncedAt": None, "stale": True},
        )
    infos = _model_infos(
        view.published,
        view.declared,
        catalog=coordinator._catalog,
        request=request,
    )
    if not view.declared_available and view.declared_error is not None:
        return ProviderModelsDiscoverResult(
            ok=False,
            provider_id=provider_id,
            failure_kind=_error_failure_kind(view.declared_error),
            detail="TokenRhythm authenticated model catalog is unavailable.",
            catalog=view.catalog,
        )
    return ProviderModelsDiscoverResult(
        ok=True,
        provider_id=provider_id,
        source="live" if infos else "none",
        models=_discovery_rows(infos),
        catalog=view.catalog,
    )


def cached_tokenrhythm_models(config: Any) -> list[ModelInfo]:
    """Return TokenRhythm models from the hydrated snapshot, never the network."""

    if _coordinator is None or config is None:
        return []
    return _coordinator.cached(config)


__all__ = [
    "LiveCatalogRefreshFingerprint",
    "TOKENRHYTHM_AUTH_TIMEOUT_SECONDS",
    "TOKENRHYTHM_FAILURE_BACKOFF_SECONDS",
    "TOKENRHYTHM_PUBLIC_TIMEOUT_SECONDS",
    "TOKENRHYTHM_REFRESH_DEADLINE_SECONDS",
    "TOKENRHYTHM_SNAPSHOT_SCHEMA_VERSION",
    "TOKENRHYTHM_SUCCESS_TTL_SECONDS",
    "TokenRhythmCatalogCoordinator",
    "cached_tokenrhythm_models",
    "discover_tokenrhythm_models",
    "install_tokenrhythm_catalog_coordinator",
    "live_catalog_refresh_fingerprint",
    "reconcile_tokenrhythm_profile_transition",
    "refresh_live_model_catalog",
    "refresh_live_model_catalog_if_changed",
]
