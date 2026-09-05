"""Settings policy and commit coordination independent of Gateway requests."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NotRequired, Protocol, Self, TypedDict, cast

from opensquilla.application.config_secrets import (
    REDACTED_PUBLIC_VALUE as _REDACTED_PUBLIC_VALUE,
)
from opensquilla.application.config_secrets import (
    collect_paths as _collect_paths,
)
from opensquilla.application.config_secrets import (
    inherit_then_clear_explicit,
    is_sensitive_config_key,
    redact_public_config,
)
from opensquilla.application.config_secrets import (
    is_sensitive_redacted_path as _is_sensitive_redacted_path,
)
from opensquilla.application.config_secrets import (
    restore_redacted_values as _restore_redacted_values,
)

type SettingsValue = (
    None | bool | int | float | str | list["SettingsValue"] | dict[str, "SettingsValue"]
)
type SettingsObject = dict[str, SettingsValue]


class EffectiveSetting(TypedDict):
    value: SettingsValue
    source: str


class EffectiveSettings(TypedDict):
    fields: dict[str, EffectiveSetting]


class SettingsMutation(TypedDict):
    patched: NotRequired[list[str]]
    restartRequired: bool
    restartSections: NotRequired[list[str]]
    liveApplied: NotRequired[list[str]]
    linked: NotRequired[list[str]]
    linkedLive: NotRequired[bool]
    model_routing: NotRequired[SettingsObject]


_PUBLIC_DERIVED_CONFIG_PATHS = frozenset(
    {
        "llm_ensemble.selection_configured",
        "llm_ensemble.activation_preview",
        "privacy.network_observability_disabled_effective",
    }
)

_READONLY_PATHS = frozenset({"auth.token", "auth.password", "config_version"})

_READONLY_PATH_SEGMENTS = frozenset(tuple(path.split(".")) for path in _READONLY_PATHS)

_SAFE_WRITE_PATCH_PATHS = frozenset(
    {
        "skills.filter_enabled",
        "skills.filter_lexical_top_n",
        "skills.filter_semantic_top_n",
        "skills.filter_rrf_k",
        "skills.disabled",
        "skills.coding_mode",
        "llm_ensemble.enabled",
        "llm_ensemble.selection_mode",
        "llm_ensemble.candidates",
        "naming.enabled",
        "privacy.disable_network_observability",
        "control_ui.default_locale",
        "prompt_cache.mode",
        "squilla_router.enabled",
        "squilla_router.rollout_phase",
        "squilla_router.strategy",
        "squilla_router.visual_mode",
        "squilla_router.default_tier",
        "squilla_router.confidence_threshold",
        # Settings > Advanced "memory & self-learning" group. Boolean opt-ins
        # only -- thresholds and schedules stay admin-scoped. Patching
        # self_learning.enabled through the safe path still runs the dream
        # linkage (safe delegates to the full patch handler).
        "squilla_router.self_learning.enabled",
        "memory.auto_capture_enabled",
        "memory.dream.enabled",
        "memory.dream.auto_schedule",
    }
)


class SettingsConfig(Protocol):
    config_path: str | None
    _runtime_secret_paths: set[str]

    def model_copy(self, *, deep: bool = False) -> Self: ...
    def model_dump(self, *, mode: Literal["python", "json"] = "python") -> SettingsObject: ...
    def inherit_persist_provenance(self, other: Self) -> None: ...
    def _mark_env_absorbed_secrets(self, raw: SettingsObject) -> None: ...
    def mark_runtime_secret(self, path: str) -> None: ...


class SettingsRuntime[Config: SettingsConfig, PreparedProvider](Protocol):
    """Model, storage and live-runtime capabilities used by the settings owner."""

    config: Config | None
    source: str
    profile_ids: frozenset[str]

    def read_public_settings(self) -> SettingsObject: ...
    def read_effective_fields(self) -> Mapping[str, EffectiveSetting]: ...
    def build(self, payload: SettingsObject) -> Config: ...
    def validate_embedding(self, config: Config) -> None: ...
    def profile_defaults(self, profile: str) -> SettingsObject: ...
    def persist(self, config: Config) -> None: ...
    def resolve_path(self) -> Path: ...
    def load(self, path: Path) -> Config: ...
    def replace(self, old: Config, new: Config) -> None: ...
    def reconcile_routing(
        self, candidate: Config, paths: set[str], *, previous: Config
    ) -> Mapping[str, object]: ...
    def routing_snapshot(self, config: Config | None) -> SettingsObject: ...
    def catalog_fingerprint(self, config: Config | None) -> tuple[str, str, str]: ...
    def resolve_provider(self, config: Config) -> PreparedProvider: ...
    def sync_provider(self, provider: PreparedProvider) -> None: ...
    async def notify_goal(self, previous: Config | None) -> None: ...
    async def sync_runtime(self, previous: Config | None, candidate: Config) -> None: ...
    async def refresh_catalog(
        self, previous: tuple[str, str, str], candidate: Config, *, force: bool = False
    ) -> None: ...
    async def publish_routing(self, previous: SettingsObject, candidate: Config) -> None: ...
    async def reconcile_dream(self) -> bool | None: ...


@dataclass(frozen=True, slots=True)
class _SettingsBefore[Config: SettingsConfig]:
    config: Config | None
    previous: Config | None
    payload: SettingsObject
    routing: SettingsObject
    catalog: tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class SettingChange:
    path: str
    value: SettingsValue


class AppSettings[Config: SettingsConfig, PreparedProvider]:
    """Own settings policy and the durable-write/live-apply ordering."""

    def __init__(self, runtime: SettingsRuntime[Config, PreparedProvider]) -> None:
        self._runtime = runtime

    async def read_all(self) -> SettingsObject:
        return dict(self._runtime.read_public_settings())

    async def read(self, path: str) -> SettingsValue | None:
        normalized = self._normalize_path(path)
        value: SettingsValue = await self.read_all()
        for part in normalized.split("."):
            if not isinstance(value, Mapping):
                return None
            value = value.get(part)
        return value

    async def read_effective(self) -> EffectiveSettings:
        if self._runtime.config is None:
            raise ValueError("No config available")
        fields: dict[str, EffectiveSetting] = {}
        for path, field in self._runtime.read_effective_fields().items():
            if any(is_sensitive_config_key(segment) for segment in path.split(".")):
                continue
            fields[path] = {
                "value": redact_public_config(field["value"]), "source": field["source"]
            }
        return {"fields": fields}

    def _before(self, *, required: bool = True) -> _SettingsBefore[Config]:
        config = self._runtime.config
        if required and config is None:
            raise ValueError("No config available")
        return _SettingsBefore(
            config=config,
            previous=config.model_copy(deep=True) if config is not None else None,
            payload=_config_dump(config),
            routing=self._runtime.routing_snapshot(config),
            catalog=self._runtime.catalog_fingerprint(config),
        )

    def _candidate(
        self,
        before: _SettingsBefore[Config],
        payload: SettingsObject,
        explicit: set[str],
        redacted: set[str],
        force: set[tuple[str, ...]] | None = None,
        *,
        memory_paths: set[str] | None = None,
    ) -> Config:
        candidate = self._runtime.build(payload)
        if force is not None:
            assert before.config is not None
            routing = self._runtime.reconcile_routing(candidate, explicit, previous=before.config)
            explicit.update(routing)
            force.update(tuple(path.split(".")) for path in routing)
        if memory_paths is None or _memory_restart_required_for_paths(memory_paths):
            self._runtime.validate_embedding(candidate)
        inherit_then_clear_explicit(before.config, candidate, explicit - redacted)
        candidate._mark_env_absorbed_secrets(payload)
        if before.config is not None:
            candidate.inherit_persist_provenance(before.config)
        _mark_explicit_provider_resolution(candidate, explicit)
        if force is not None:
            writable = force - {tuple(path.split(".")) for path in redacted}
            _clear_runtime_override_paths(candidate, writable)
            _mark_force_persist_paths(candidate, writable)
        return candidate

    async def _publish_routing(
        self, before: _SettingsBefore[Config], candidate: Config, result: SettingsMutation
    ) -> None:
        current = self._runtime.routing_snapshot(candidate)
        if current != before.routing:
            await self._runtime.publish_routing(before.routing, candidate)
            result["model_routing"] = {**current, "source": self._runtime.source}

    async def _write(
        self,
        before: _SettingsBefore[Config],
        candidate: Config,
        *,
        patched: list[str] | None = None,
        linked: Sequence[str] = (),
    ) -> SettingsMutation:
        runtime = self._runtime
        provider = runtime.resolve_provider(candidate)
        # Persistence is the commit point. Later runtime failures do not undo it.
        runtime.persist(candidate)
        if before.config is not None:
            runtime.replace(before.config, candidate)
            await runtime.notify_goal(before.previous)
        runtime.sync_provider(provider)
        await runtime.sync_runtime(before.previous, candidate)
        await runtime.refresh_catalog(before.catalog, candidate)
        result = _change_meta(before.payload, _config_dump(candidate))
        if patched is not None:
            result["patched"] = patched
        if linked:
            result["linked"] = list(linked)
            reconciled = await runtime.reconcile_dream()
            result["linkedLive"] = reconciled is True
            if reconciled is not True:
                result["restartRequired"] = True
                sections = result.get("restartSections")
                if reconciled is None and sections is not None and "memory.dream" not in sections:
                    sections.append("memory.dream")
        await self._publish_routing(before, candidate, result)
        return result

    async def set(self, path: str, value: SettingsValue) -> SettingsMutation:
        if _path_is_or_contains_readonly(path):
            raise ValueError(f"Path is read-only: {path}")
        before = self._before()
        payload = copy.deepcopy(before.payload)
        source = _resolve_path(payload, path)
        if value == _REDACTED_PUBLIC_VALUE and _is_sensitive_redacted_path(path):
            raise ValueError(
                f"Cannot set redacted secret marker directly at {path}; "
                "submit the containing public config object to preserve it"
            )
        restored, redacted = _restore_redacted_values(value, source, path)
        _set_path(payload, path, restored)
        payload = _strip_public_derived_config_fields(payload)
        explicit = {
            item
            for item in ({path} | _collect_paths(value, path))
            if not _is_public_derived_config_path(item)
        }
        force = {
            item
            for item in _collect_explicit_leaf_paths(
                value, tuple(path.split(".")), empty_mapping_is_leaf=True
            )
            if not _is_public_derived_config_path(".".join(item))
        }
        linked = _link_dream_for_self_learning_patch(before.config, payload, explicit)
        explicit.update(linked)
        force.update(tuple(item.split(".")) for item in linked)
        candidate = self._candidate(before, payload, explicit, redacted, force, memory_paths={path})
        return await self._write(before, candidate, linked=linked)

    async def patch(self, changes: Sequence[SettingChange]) -> SettingsMutation:
        return await self._mutate({}, self._normalized_changes(changes))

    async def patch_safe(self, changes: Sequence[SettingChange]) -> SettingsMutation:
        unsafe = sorted({change.path for change in changes} - _SAFE_WRITE_PATCH_PATHS)
        if unsafe:
            raise ValueError(f"Path is not safe for operator.write: {unsafe[0]}")
        return await self.patch(changes)

    async def merge(self, patch: Mapping[str, SettingsValue]) -> SettingsMutation:
        if not patch:
            raise ValueError("settings patch must not be empty")
        return await self._mutate(dict(patch), {})

    async def patch_combined(
        self, patch: Mapping[str, SettingsValue], changes: Mapping[str, SettingsValue]
    ) -> SettingsMutation:
        # The legacy combined form applies raw dotted changes before the merge.
        return await self._mutate(dict(patch), dict(changes))

    async def _mutate(self, patch: SettingsObject, changes: SettingsObject) -> SettingsMutation:
        if not patch and not changes:
            raise ValueError("params.patch or params.patches is required")
        before = self._before()
        payload = copy.deepcopy(before.payload)
        redacted: set[str] = set()
        force: set[tuple[str, ...]] = set()
        for path, value in changes.items():
            if _path_is_or_contains_readonly(path):
                continue
            if value == _REDACTED_PUBLIC_VALUE and _is_sensitive_redacted_path(path):
                raise ValueError(
                    f"Cannot patch redacted secret marker directly at {path}; "
                    "submit the containing public config object to preserve it"
                )
            try:
                source = _resolve_path(before.payload, path)
            except KeyError:
                source = None
            restored, paths = _restore_redacted_values(value, source, path)
            redacted.update(paths)
            _set_path(payload, path, restored)
            force.update(
                _collect_explicit_leaf_paths(
                    value, tuple(path.split(".")), empty_mapping_is_leaf=True
                )
            )
        if patch:
            patch = _prune_readonly_paths(patch)
            patch, paths = _restore_redacted_values(patch, before.payload)
            redacted.update(paths)
            payload = _deep_merge(payload, patch)
            force.update(_collect_explicit_leaf_paths(patch))
        payload = _strip_public_derived_config_fields(payload)
        explicit = set(changes) | _collect_paths(patch)
        for path, value in changes.items():
            explicit.update(_collect_paths(value, path))
        explicit = {
            path for path in explicit - _READONLY_PATHS if not _is_public_derived_config_path(path)
        }
        force = {path for path in force if not _is_public_derived_config_path(".".join(path))}
        _align_auto_router_profile_for_provider_patch(
            before.config,
            payload,
            explicit,
            profile_defaults=self._runtime.profile_defaults,
            profile_ids=self._runtime.profile_ids,
        )
        linked = _link_dream_for_self_learning_patch(before.config, payload, explicit)
        explicit.update(linked)
        force.update(tuple(path.split(".")) for path in linked)
        candidate = self._candidate(
            before, payload, explicit, redacted, force, memory_paths=explicit
        )
        return await self._write(
            before, candidate, patched=list(changes) + (["(merge)"] if patch else []), linked=linked
        )

    async def apply(self, payload: Mapping[str, SettingsValue]) -> SettingsMutation:
        before = self._before(required=False)
        replacement = dict(payload)
        if before.config is not None and not replacement.get("config_path"):
            replacement["config_path"] = before.config.config_path
        replacement, redacted = _restore_redacted_values(replacement, before.payload)
        replacement = _strip_public_derived_config_fields(replacement)
        candidate = self._candidate(before, replacement, _collect_paths(replacement), redacted)
        return await self._write(before, candidate)

    async def reload(self) -> SettingsObject:
        before = self._before()
        assert before.config is not None
        runtime = self._runtime
        target = runtime.resolve_path()
        try:
            candidate = runtime.load(target)
            runtime.validate_embedding(candidate)
        except Exception as exc:
            return {"ok": False, "path": str(target), "error": str(exc)}
        # Disk/env provenance is newly loaded. Only non-reconstructible,
        # boot-generated read-only secrets survive from the live configuration.
        for path in sorted(before.config._runtime_secret_paths & _READONLY_PATHS):
            if _get_config_attr(candidate, path):
                continue
            value = _get_config_attr(before.config, path)
            if value:
                _set_config_attr(candidate, path, value)
                candidate.mark_runtime_secret(path)
        # Reload synchronizes the candidate before replacing live state and never persists.
        runtime.sync_provider(runtime.resolve_provider(candidate))
        result = _change_meta(before.payload, _config_dump(candidate))
        runtime.replace(before.config, candidate)
        await runtime.notify_goal(before.previous)
        await runtime.sync_runtime(before.previous, candidate)
        await runtime.refresh_catalog(before.catalog, candidate, force=True)
        await self._publish_routing(before, candidate, result)
        return cast(SettingsObject, {"ok": True, "path": str(target), **result})

    @classmethod
    def _normalized_changes(cls, changes: Sequence[SettingChange]) -> SettingsObject:
        if not changes:
            raise ValueError("settings changes must not be empty")
        result: SettingsObject = {}
        for change in changes:
            path = cls._normalize_path(change.path)
            if path in result:
                raise ValueError(f"duplicate settings path: {path}")
            result[path] = change.value
        return result

    @staticmethod
    def _normalize_path(path: str) -> str:
        normalized = str(path or "").strip()
        if not normalized or any(not part for part in normalized.split(".")):
            raise ValueError("settings path must be a non-empty dotted path")
        return normalized


def _memory_fingerprint(payload: SettingsObject) -> SettingsObject:
    memory = payload.get("memory")
    if not isinstance(memory, dict):
        return {}
    return {"retrieval_mode": memory.get("retrieval_mode"), "embedding": memory.get("embedding")}


def _channel_fingerprint(payload: SettingsObject) -> list[SettingsObject] | None:
    channels = payload.get("channels")
    if not isinstance(channels, dict):
        return None
    entries = channels.get("channels") or []
    if not isinstance(entries, list):
        return None
    return sorted(
        [entry for entry in entries if isinstance(entry, dict)],
        key=lambda entry: (entry.get("name") or "", entry.get("type") or ""),
    )


def _change_meta(before: SettingsObject, after: SettingsObject) -> SettingsMutation:
    # Preserve the established memory/channels/sandbox restart fingerprints.
    # Other boot-only fields keep the existing liveApplied reporting semantics.
    sections: list[str] = []
    if _memory_fingerprint(before) != _memory_fingerprint(after):
        sections.append("memory")
    if _channel_fingerprint(before) != _channel_fingerprint(after):
        sections.append("channels")
    for key in ("permissions", "sandbox"):
        if before.get(key) != after.get(key):
            sections.append(key)
    excluded = set(sections) | {"config_path"}
    return {
        "restartRequired": bool(sections),
        "restartSections": sections,
        "liveApplied": sorted(
            key
            for key in set(before) | set(after)
            if key not in excluded and before.get(key) != after.get(key)
        ),
    }


def _is_public_derived_config_path(path: str) -> bool:
    return any(
        path == derived or path.startswith(f"{derived}.")
        for derived in _PUBLIC_DERIVED_CONFIG_PATHS
    )


def _strip_public_derived_config_fields(payload: dict[str, Any]) -> dict[str, Any]:
    ensemble = payload.get("llm_ensemble")
    if isinstance(ensemble, dict) and (
        "selection_configured" in ensemble or "activation_preview" in ensemble
    ):
        payload = dict(payload)
        ensemble = dict(ensemble)
        ensemble.pop("selection_configured", None)
        ensemble.pop("activation_preview", None)
        payload["llm_ensemble"] = ensemble
    privacy = payload.get("privacy")
    if isinstance(privacy, dict) and "network_observability_disabled_effective" in privacy:
        payload = dict(payload)
        privacy = dict(privacy)
        privacy.pop("network_observability_disabled_effective", None)
        payload["privacy"] = privacy
    return payload


def _link_dream_for_self_learning_patch(
    source_config: Any,
    cfg_dict: dict[str, Any],
    explicit_paths: set[str],
) -> list[str]:
    """Atomically enable the dream chain when self-learning is switched on.

    Router self-learning's training trigger rides the post-dream hook; enabling
    it while dream is off (the default) captures samples that never train. When
    an edit flips ``squilla_router.self_learning.enabled`` to true, pull
    ``memory.dream.enabled`` and ``memory.dream.auto_schedule`` up with it —
    unless the same edit also touches those keys explicitly (the operator's
    word wins). Deliberately one-directional: disabling self-learning never
    touches dream, which the operator may rely on independently. Returns the
    linked dot-paths for the response so clients can show what changed.
    """

    sl_paths = {
        "squilla_router.self_learning.enabled",
        "squilla_router.self_learning",
        "squilla_router",
    }
    if not (explicit_paths & sl_paths):
        return []

    router = cfg_dict.get("squilla_router")
    sl = router.get("self_learning") if isinstance(router, dict) else None
    if not isinstance(sl, dict) or not bool(sl.get("enabled")):
        return []

    was_enabled = bool(
        getattr(
            getattr(getattr(source_config, "squilla_router", None), "self_learning", None),
            "enabled",
            False,
        )
    )
    if was_enabled:  # only the off -> on transition links
        return []

    memory = cfg_dict.setdefault("memory", {})
    if not isinstance(memory, dict):
        return []
    dream = memory.setdefault("dream", {})
    if not isinstance(dream, dict):
        return []

    linked: list[str] = []
    for key in ("enabled", "auto_schedule"):
        path = f"memory.dream.{key}"
        if path in explicit_paths or "memory.dream" in explicit_paths:
            continue  # explicit operator value wins over linkage
        if not bool(dream.get(key)):
            dream[key] = True
            linked.append(path)
    return linked


def _align_auto_router_profile_for_provider_patch(
    source_config: Any,
    cfg_dict: dict[str, Any],
    explicit_paths: set[str],
    *,
    profile_defaults: Callable[[str], SettingsObject],
    profile_ids: frozenset[str],
) -> None:
    if "llm.provider" not in explicit_paths:
        return
    if any(
        path == "squilla_router" or path.startswith("squilla_router.") for path in explicit_paths
    ):
        return

    llm = cfg_dict.get("llm")
    router = cfg_dict.get("squilla_router")
    if not isinstance(llm, dict) or not isinstance(router, dict):
        return

    old_provider = str(getattr(getattr(source_config, "llm", None), "provider", "") or "")
    old_provider = old_provider.strip().lower()
    new_provider = str(llm.get("provider") or "").strip().lower()
    if not old_provider or not new_provider or old_provider == new_provider:
        return

    profile = str(router.get("tier_profile") or "").strip().lower()
    if profile != old_provider:
        return

    try:
        old_defaults = profile_defaults(old_provider)
    except ValueError:
        return
    if router.get("tiers") != old_defaults:
        return

    if new_provider in profile_ids and new_provider != "openrouter":
        router["tier_profile"] = new_provider
        router["tiers"] = profile_defaults(new_provider)
        return

    router.pop("tier_profile", None)
    router.pop("tiers", None)


def _memory_restart_required_for_paths(paths: set[str]) -> bool:
    for path in paths:
        if path == "memory":
            return True
        if path == "memory.retrieval_mode":
            return True
        if path.startswith("memory.embedding"):
            return True
    return False


def _config_dump(config: Any) -> dict[str, Any]:
    if config is None or not hasattr(config, "model_dump"):
        return {}
    data = config.model_dump(mode="python")
    return data if isinstance(data, dict) else {}


def _path_is_or_contains_readonly(path: str) -> bool:
    """Return whether setting ``path`` could replace a read-only descendant."""

    return any(readonly == path or readonly.startswith(f"{path}.") for readonly in _READONLY_PATHS)


def _path_segments_is_or_contains_readonly(path: tuple[str, ...]) -> bool:
    return any(
        readonly == path or readonly[: len(path)] == path for readonly in _READONLY_PATH_SEGMENTS
    )


def _prune_readonly_paths(patch: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``patch`` with every read-only path protected.

    Mirrors the ``continue`` guard the dot-path form applies to
    ``_READONLY_PATHS`` (auth.token, auth.password, config_version), so the
    dict-merge form cannot smuggle a write to those paths past the guard. A
    non-mapping replacement of a read-only ancestor is dropped because it
    would otherwise replace or delete the protected descendants wholesale.
    """

    def _walk(node: dict[str, Any], prefix: str) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if path in _READONLY_PATHS:
                continue
            if isinstance(value, dict):
                nested = _walk(value, path)
                # Drop a section that became empty only because everything in
                # it was read-only; keep genuinely-empty client sections.
                if nested or not value:
                    cleaned[key] = nested
            elif _path_is_or_contains_readonly(path):
                continue
            else:
                cleaned[key] = value
        return cleaned

    return _walk(patch, "")


def _resolve_path(obj: dict, path: str) -> Any:
    """Walk a dot-separated path into a nested dict."""
    parts = path.split(".")
    val: Any = obj
    for part in parts:
        if isinstance(val, dict):
            if part not in val:
                raise KeyError(f"Path not found: {path}")
            val = val[part]
        else:
            raise KeyError(f"Path not found: {path}")
    return val


def _set_path(obj: dict, path: str, value: Any) -> None:
    """Set a value at a dot-separated path in a nested dict."""
    parts = path.split(".")
    current = obj
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value


def _deep_merge(base: dict, patch: dict) -> dict:
    """Deep-merge *patch* into *base*. Keys set to None delete the target key."""
    result = dict(base)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _collect_explicit_leaf_paths(
    payload: Any,
    prefix: tuple[str, ...] = (),
    *,
    empty_mapping_is_leaf: bool = False,
) -> set[tuple[str, ...]]:
    """Collect submitted leaf paths without force-writing parent sections."""
    if not isinstance(payload, dict):
        return {prefix} if prefix else set()
    if not payload:
        return {prefix} if prefix and empty_mapping_is_leaf else set()

    paths: set[tuple[str, ...]] = set()
    for key, value in payload.items():
        path = (*prefix, str(key))
        paths.update(
            _collect_explicit_leaf_paths(
                value,
                path,
                empty_mapping_is_leaf=empty_mapping_is_leaf,
            )
        )
    return paths


def _mark_force_persist_paths(config: Any, paths: set[tuple[str, ...]]) -> None:
    """Make explicit writable values win over post-load disk drift once."""
    if not hasattr(config, "mark_force_persist_segments"):
        return
    for path in sorted(paths):
        if path == ("config_path",) or _path_segments_is_or_contains_readonly(path):
            continue
        config.mark_force_persist_segments(path)


def _clear_runtime_override_paths(config: Any, paths: set[tuple[str, ...]]) -> None:
    """Make explicitly submitted runtime values authoritative for persistence."""
    if not hasattr(config, "clear_runtime_override"):
        return
    for path in sorted(paths):
        if path == ("config_path",) or _path_segments_is_or_contains_readonly(path):
            continue
        config.clear_runtime_override(".".join(path))


def _mark_explicit_provider_resolution(config: Any, explicit_paths: set[str]) -> None:
    """Make an operator-authored provider identity replace inference metadata."""

    if "llm.provider" not in explicit_paths:
        return
    marker = getattr(config, "mark_force_persist", None)
    if callable(marker):
        marker("llm.provider")
    setter = getattr(config, "set_provider_resolution", None)
    if callable(setter):
        provider = str(getattr(getattr(config, "llm", None), "provider", "") or "")
        setter(
            status="explicit",
            effective_provider=provider,
            source="operator",
            reason_code="provider_explicit",
        )


def _get_config_attr(config: Any, path: str) -> Any:
    """Walk a dot-separated attribute path on a config model."""
    obj: Any = config
    for part in path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def _set_config_attr(config: Any, path: str, value: Any) -> None:
    """Set a dot-separated attribute path on a config model."""
    parts = path.split(".")
    obj: Any = config
    for part in parts[:-1]:
        obj = getattr(obj, part, None)
        if obj is None:
            return
    setattr(obj, parts[-1], value)
