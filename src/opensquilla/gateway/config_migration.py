"""Pre-validation migration for user-owned gateway config files."""

from __future__ import annotations

import copy
import datetime
import json
import logging
import os
import tempfile
import threading
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from opensquilla.paths import default_opensquilla_home
from opensquilla.search.types import MAX_SEARCH_RESULTS

# Schema version stamped into every migrated payload. Bump this together with
# a new ``_MIGRATIONS`` entry whenever a one-time value migration is added.
# ``GatewayConfig.config_version`` (gateway/config.py) defaults to this value.
LATEST_CONFIG_VERSION = 1

DEPRECATED_MEMORY_FIELDS: frozenset[str] = frozenset(
    {
        "memory.profile",
        "memory.cost.embedding_cache",
        "memory.cost.rerank_cache",
        "memory.cost.llm_judge_cache",
        "memory.facts_enabled",
        "memory.facts_top_k",
        "memory.facts_max_chars",
        "memory.multi_hop_enabled",
        "memory.multi_hop_max_depth",
        "memory.multi_hop_score_threshold",
        "memory.recall_frequency",
        "memory.recall_top_k_default",
        "memory.auto_recall_enabled",
        "memory.prefetch_enabled",
        "memory.prefetch_max_results",
        "memory.prefetch_min_score",
        "memory.prefetch_total_max_chars",
        "memory.semantic_chunking_enabled",
        "memory.eviction_policy",
        "memory.summary_model",
        "memory.summary_max_tokens",
    }
)

DEPRECATED_COST_LEAVES: frozenset[str] = frozenset(
    k.removeprefix("memory.cost.")
    for k in DEPRECATED_MEMORY_FIELDS
    if k.startswith("memory.cost.")
)
DEPRECATED_MEMORY_LEAVES: frozenset[str] = frozenset(
    k.removeprefix("memory.")
    for k in DEPRECATED_MEMORY_FIELDS
    if k.startswith("memory.") and not k.startswith("memory.cost.")
)

DEPRECATED_AGENT_TOKEN_SAVING_FIELDS: frozenset[str] = frozenset(
    {
        "agent_token_saving.tool_result_compression_enabled",
        "agent_token_saving.tool_result_compression_mode",
        "agent_token_saving.tool_result_compression_max_share",
        "agent_token_saving.tool_result_compression_summary_model",
        "agent_token_saving.tool_result_compression_summary_max_tokens",
        "agent_token_saving.tool_result_compression_summary_timeout_seconds",
        "agent_token_saving.tool_result_compression_summary_input_max_chars",
    }
)
DEPRECATED_AGENT_TOKEN_SAVING_LEAVES: frozenset[str] = frozenset(
    k.removeprefix("agent_token_saving.")
    for k in DEPRECATED_AGENT_TOKEN_SAVING_FIELDS
)
_LEGACY_LLM_ENSEMBLE_TIMEOUT_SECONDS = frozenset({120.0, 300.0})
_DEFAULT_LLM_ENSEMBLE_TIMEOUT_SECONDS = 3600.0


def _legacy_llm_ensemble_timeout_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_LEGACY_MEMORY_FIELDS_WARN_LOCK = threading.Lock()
_LEGACY_MEMORY_FIELDS_WARNED = False
_LEGACY_MEMORY_FIELDS_SEEN: set[str] = set()
_LEGACY_AGENT_TOKEN_SAVING_FIELDS_WARN_LOCK = threading.Lock()
_LEGACY_AGENT_TOKEN_SAVING_FIELDS_WARNED = False
_LEGACY_AGENT_TOKEN_SAVING_FIELDS_SEEN: set[str] = set()


@dataclass(frozen=True)
class ConfigMigrationResult:
    payload: dict[str, Any]
    changes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    removed_fields: tuple[str, ...] = ()
    changed: bool = False


@dataclass
class _MigrationBuilder:
    payload: dict[str, Any]
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    removed_fields: list[str] = field(default_factory=list)

    def result(self) -> ConfigMigrationResult:
        changed = bool(self.changes or self.removed_fields)
        return ConfigMigrationResult(
            payload=self.payload,
            changes=tuple(self.changes),
            warnings=tuple(self.warnings),
            removed_fields=tuple(self.removed_fields),
            changed=changed,
        )


def handle_deprecated_memory_fields(
    found: dict[str, object],
    source: str,
) -> None:
    """Record and warn once for deprecated memory fields removed from config data."""
    global _LEGACY_MEMORY_FIELDS_WARNED

    if not found:
        return

    with _LEGACY_MEMORY_FIELDS_WARN_LOCK:
        _LEGACY_MEMORY_FIELDS_SEEN.update(found.keys())
        should_warn = not _LEGACY_MEMORY_FIELDS_WARNED
        if should_warn:
            _LEGACY_MEMORY_FIELDS_WARNED = True
            warning_fields = sorted(_LEGACY_MEMORY_FIELDS_SEEN)
        else:
            warning_fields = []

    _write_legacy_field_log(found, source)

    if should_warn:
        n = len(warning_fields)
        first_three = ", ".join(warning_fields[:3])
        try:
            logs_dir = default_opensquilla_home() / "logs"
            log_ref = str(logs_dir)
        except Exception:
            log_ref = "~/.opensquilla/logs"
        warnings.warn(
            f"OpenSquilla: {n} legacy memory.* config field(s) ignored "
            f"(e.g. {first_three}); see {log_ref} for details. "
            f"These fields will be removed in 0.2.0.",
            DeprecationWarning,
            stacklevel=6,
        )
        logging.getLogger(__name__).warning(
            "OpenSquilla: %d legacy memory.* config field(s) ignored (e.g. %s); "
            "see %s for details. These fields will be removed in 0.2.0.",
            n,
            first_three,
            log_ref,
        )


def handle_deprecated_agent_token_saving_fields(
    found: dict[str, object],
    source: str,
) -> None:
    """Record and warn once for deprecated token-saving fields removed from config data."""
    global _LEGACY_AGENT_TOKEN_SAVING_FIELDS_WARNED

    if not found:
        return

    with _LEGACY_AGENT_TOKEN_SAVING_FIELDS_WARN_LOCK:
        _LEGACY_AGENT_TOKEN_SAVING_FIELDS_SEEN.update(found.keys())
        should_warn = not _LEGACY_AGENT_TOKEN_SAVING_FIELDS_WARNED
        if should_warn:
            _LEGACY_AGENT_TOKEN_SAVING_FIELDS_WARNED = True
            warning_fields = sorted(_LEGACY_AGENT_TOKEN_SAVING_FIELDS_SEEN)
        else:
            warning_fields = []

    _write_legacy_field_log(found, source)

    if should_warn:
        n = len(warning_fields)
        first_three = ", ".join(warning_fields[:3])
        try:
            logs_dir = default_opensquilla_home() / "logs"
            log_ref = str(logs_dir)
        except Exception:
            log_ref = "~/.opensquilla/logs"
        warnings.warn(
            f"OpenSquilla: {n} legacy agent_token_saving.tool_result_compression_* "
            f"config field(s) migrated or ignored (e.g. {first_three}); see "
            f"{log_ref} for details. Tokenjuice projection is now the built-in "
            "tool-result path.",
            DeprecationWarning,
            stacklevel=6,
        )
        logging.getLogger(__name__).warning(
            "OpenSquilla: %d legacy agent_token_saving.tool_result_compression_* "
            "config field(s) migrated or ignored (e.g. %s); see %s for details. "
            "Tokenjuice projection is now the built-in tool-result path.",
            n,
            first_three,
            log_ref,
        )


def _write_legacy_field_log(found: dict[str, object], source: str) -> None:
    try:
        logs_dir = default_opensquilla_home() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        iso_now = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        log_path = logs_dir / f"legacy_config_{iso_now}.log"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        with os.fdopen(os.open(log_path, flags, 0o600), "a", encoding="utf-8") as fh:
            _set_owner_only_mode(fh.fileno(), log_path)
            for leaf, value in found.items():
                entry = {
                    "timestamp": datetime.datetime.now(tz=datetime.UTC).isoformat(),
                    "field": leaf,
                    "source": source,
                    **_legacy_value_metadata(value),
                }
                fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _set_owner_only_mode(fd: int, path: Path) -> None:
    """Restrict a log file without assuming descriptor chmod is available."""
    fchmod = getattr(os, "fchmod", None)
    if callable(fchmod):
        try:
            fchmod(fd, 0o600)
            return
        except (AttributeError, NotImplementedError, OSError):
            pass
    os.chmod(path, 0o600)


def _legacy_value_metadata(value: object) -> dict[str, object]:
    """Describe a discarded value without serializing any of its contents."""
    if isinstance(value, str):
        return {"value_type": "string", "value_shape": {"length": len(value)}}
    if isinstance(value, dict):
        return {"value_type": "mapping", "value_shape": {"entries": len(value)}}
    if isinstance(value, (list, tuple)):
        return {"value_type": "sequence", "value_shape": {"items": len(value)}}
    if isinstance(value, (bytes, bytearray)):
        return {"value_type": "bytes", "value_shape": {"length": len(value)}}
    if value is None:
        value_type = "null"
    elif isinstance(value, bool):
        value_type = "boolean"
    elif isinstance(value, int):
        value_type = "integer"
    elif isinstance(value, float):
        value_type = "number"
    elif isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        value_type = "temporal"
    else:
        value_type = "object"
    return {"value_type": value_type, "value_shape": {"kind": "scalar"}}


def migrate_config_payload(
    data: dict[str, Any],
    *,
    emit_diagnostics: bool = True,
) -> ConfigMigrationResult:
    """Return a config payload upgraded for the current strict schema.

    Call this only at user-owned disk-load boundaries, before GatewayConfig
    validates the payload.

    Two classes of transforms run here:

    * Always-run compat normalizations — idempotent, protective coercions
      (deprecated-field strips, renames, range clamps) that must fire on
      every load, even for stamped configs a user later hand-edits with
      stale keys. Skipping them would hard-fail strict validation.
    * Version-gated value migrations (``_MIGRATIONS``) — one-time value
      rewrites gated on the payload's ``config_version`` stamp so they run
      exactly once per config file.

    The returned payload is always stamped with ``LATEST_CONFIG_VERSION``.
    Stamping alone never marks the result as changed, so a file is never
    rewritten solely to receive the stamp.

    Set ``emit_diagnostics=False`` for a side-effect-free dry run. The result
    still reports changes and migration warnings, but no compatibility log,
    process warning, logging record, or process warning sentinel is touched.
    """
    builder = _MigrationBuilder(payload=copy.deepcopy(data))

    _normalize_memory_fields(builder, emit_diagnostics=emit_diagnostics)
    _normalize_agent_token_saving_fields(
        builder,
        emit_diagnostics=emit_diagnostics,
    )
    _clamp_search_max_results(builder)
    _park_unknown_channel_entries(builder, emit_diagnostics=emit_diagnostics)
    _disable_unverifiable_feishu_webhook_entries(builder)
    _clear_mismatched_router_tier_profile(builder)

    stamped_version = _payload_config_version(builder.payload)
    for version, migrate in _MIGRATIONS:
        if stamped_version < version:
            migrate(builder)

    # Stamp the payload so future loads skip completed one-time migrations.
    # Deliberately not recorded in changes/removed_fields: the stamp must not
    # flip ConfigMigrationResult.changed, so call sites only rewrite the
    # on-disk file (and thereby persist the stamp) when a pass made a real
    # change.
    builder.payload["config_version"] = LATEST_CONFIG_VERSION

    return builder.result()


def _payload_config_version(payload: dict[str, Any]) -> int:
    """Return the payload's migration stamp; anything missing or invalid is 0.

    Reads the raw payload (never a constructed model field) so env-provided
    values can never gate migrations, and coerces defensively because this
    runs before strict validation.
    """
    value = payload.get("config_version", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return int(value)


def _normalize_memory_fields(
    builder: _MigrationBuilder,
    *,
    emit_diagnostics: bool,
) -> None:
    """Always-run: strip/rename deprecated ``memory.*`` fields."""
    memory = builder.payload.get("memory")
    if not isinstance(memory, dict):
        return

    if memory.get("capture_mode") == "archive_turn_pair":
        memory["capture_mode"] = "turn_pair"
        builder.changes.append("memory.capture_mode: archive_turn_pair -> turn_pair")

    if "index_captured_turns" in memory:
        value = memory.pop("index_captured_turns")
        builder.removed_fields.append("memory.index_captured_turns")
        if bool(value):
            builder.warnings.append(
                "memory.index_captured_turns was removed; captured turns are no "
                "longer indexed into normal recall"
            )

    deprecated: dict[str, object] = {}
    for leaf in list(memory):
        if leaf in DEPRECATED_MEMORY_LEAVES:
            deprecated[f"memory.{leaf}"] = memory.pop(leaf)

    cost = memory.get("cost")
    if isinstance(cost, dict):
        for leaf in list(cost):
            if leaf in DEPRECATED_COST_LEAVES:
                deprecated[f"memory.cost.{leaf}"] = cost.pop(leaf)
        if not cost:
            memory.pop("cost", None)

    # memory.dream.model_override was a legal, settable field through 0.2.x;
    # 0.3.0 removed it and made DreamConfig extra='forbid' with no strip, so
    # a carried-over config that set it hard-fails validation. Strip it here.
    dream = memory.get("dream")
    if isinstance(dream, dict) and "model_override" in dream:
        deprecated["memory.dream.model_override"] = dream.pop("model_override")
        builder.warnings.append(
            "memory.dream.model_override was removed in 0.3.0; the dream "
            "consolidation model now follows the configured provider"
        )

    if deprecated:
        builder.removed_fields.extend(sorted(deprecated))
        if emit_diagnostics:
            handle_deprecated_memory_fields(deprecated, "config_migration")


def _normalize_agent_token_saving_fields(
    builder: _MigrationBuilder,
    *,
    emit_diagnostics: bool,
) -> None:
    """Always-run: migrate/strip deprecated ``agent_token_saving.*`` fields."""
    token_saving = builder.payload.get("agent_token_saving")
    if not isinstance(token_saving, dict):
        return

    summary_input_leaf = "tool_result_compression_summary_input_max_chars"
    projection_leaf = "tool_result_projection_max_inline_chars"
    if summary_input_leaf in token_saving and projection_leaf not in token_saving:
        token_saving[projection_leaf] = token_saving[summary_input_leaf]
        builder.changes.append(
            "agent_token_saving.tool_result_compression_summary_input_max_chars "
            "-> agent_token_saving.tool_result_projection_max_inline_chars"
        )

    deprecated_token_saving: dict[str, object] = {}
    for leaf in list(token_saving):
        if leaf in DEPRECATED_AGENT_TOKEN_SAVING_LEAVES:
            deprecated_token_saving[f"agent_token_saving.{leaf}"] = token_saving.pop(leaf)

    if deprecated_token_saving:
        builder.removed_fields.extend(sorted(deprecated_token_saving))
        if emit_diagnostics:
            handle_deprecated_agent_token_saving_fields(
                deprecated_token_saving,
                "config_migration",
            )
        if (
            deprecated_token_saving.get(
                "agent_token_saving.tool_result_compression_enabled"
            )
            is False
            or deprecated_token_saving.get(
                "agent_token_saving.tool_result_compression_mode"
            )
            == "off"
        ):
            builder.warnings.append(
                "agent_token_saving.tool_result_compression_* was removed; "
                "tokenjuice projection is now the built-in tool-result path"
            )


def _clamp_search_max_results(builder: _MigrationBuilder) -> None:
    """Always-run: coerce ``search_max_results`` into its bounded range.

    search_max_results gained an upper bound (<= MAX_SEARCH_RESULTS); coerce
    any legacy out-of-range value here so an older config loads instead of
    failing strict validation at the GatewayConfig boundary.
    """
    search_max_results = builder.payload.get("search_max_results")
    if search_max_results is None or isinstance(search_max_results, bool):
        return
    try:
        requested = int(search_max_results)
    except (TypeError, ValueError):
        return
    coerced = min(max(requested, 1), MAX_SEARCH_RESULTS)
    if coerced != search_max_results:
        builder.payload["search_max_results"] = coerced
        builder.changes.append(
            f"search_max_results: {search_max_results} -> {coerced} "
            f"(clamped to [1, {MAX_SEARCH_RESULTS}])"
        )


def _park_unknown_channel_entries(
    builder: _MigrationBuilder,
    *,
    emit_diagnostics: bool,
) -> None:
    """Always-run: drop channel entries whose type is no longer registered.

    ``parse_channel_entry`` raises for unregistered channel types during
    GatewayConfig validation even when the entry is disabled, so one stale
    entry (e.g. ``type = "msteams"``, configurable only in early releases)
    rejects the entire config file. Park such entries instead: remove them
    from the payload with a logged warning — the pre-migration backup written
    beside the file preserves the original entry.
    """
    channels_section = builder.payload.get("channels")
    if not isinstance(channels_section, dict):
        return
    entries = channels_section.get("channels")
    if not isinstance(entries, list):
        return

    try:
        from opensquilla.channels.registry import get_channel_registration
    except Exception:  # pragma: no cover - registry import must not brick loads
        return

    parked: dict[str, object] = {}
    parked_log_fields: dict[str, object] = {}
    kept: list[Any] = []
    for index, entry in enumerate(entries):
        channel_type = entry.get("type") if isinstance(entry, dict) else None
        if (
            isinstance(channel_type, str)
            and channel_type
            and get_channel_registration(channel_type) is None
        ):
            name = entry.get("name") if isinstance(entry, dict) else None
            label = f"channels.channels[type={channel_type}"
            label += f", name={name}]" if isinstance(name, str) and name else "]"
            parked[label] = entry
            parked_log_fields[f"channels.channels[{index}]"] = entry
            continue
        kept.append(entry)

    if not parked:
        return
    channels_section["channels"] = kept
    builder.removed_fields.extend(sorted(parked))
    for label in sorted(parked):
        builder.warnings.append(
            f"{label} references an unregistered channel type and was parked "
            "(kept in the config backup); re-add it when the channel returns"
        )
    if emit_diagnostics:
        _write_legacy_field_log(parked_log_fields, "config_migration")


def _disable_unverifiable_feishu_webhook_entries(builder: _MigrationBuilder) -> None:
    """Always-run: park feishu webhook entries with no ingress credential.

    Feishu webhook entries now require ``verification_token`` or
    ``encrypt_key`` — with neither, every inbound request would be rejected,
    so the shape was legal-but-dead in earlier releases. The strict schema
    rejects it outright, which must not brick an upgraded config file: coerce
    such entries to ``enabled = false`` (validation skips disabled entries)
    with a warning telling the operator what to add before re-enabling.
    """
    channels_section = builder.payload.get("channels")
    if not isinstance(channels_section, dict):
        return
    entries = channels_section.get("channels")
    if not isinstance(entries, list):
        return

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "feishu":
            continue
        if entry.get("connection_mode") != "webhook":
            continue
        if not bool(entry.get("enabled", True)):
            continue
        verification_token = str(entry.get("verification_token") or "").strip()
        encrypt_key = str(entry.get("encrypt_key") or "").strip()
        if verification_token or encrypt_key:
            continue
        entry["enabled"] = False
        name = entry.get("name")
        label = "channels.channels[type=feishu"
        label += f", name={name}]" if isinstance(name, str) and name else "]"
        builder.changes.append(f"{label}: enabled -> false (no webhook credential)")
        builder.warnings.append(
            f"{label} uses webhook mode without verification_token or "
            "encrypt_key and was disabled; add one of them and re-enable the "
            "channel"
        )


def _clear_mismatched_router_tier_profile(builder: _MigrationBuilder) -> None:
    """Always-run: clear ``squilla_router.tier_profile`` on provider mismatch.

    Validation hard-fails when ``tier_profile`` no longer matches
    ``llm.provider`` — the classic hand-edit trap of switching providers
    without clearing the profile pointer. An atomic primary/profile activation
    is the intentional exception: it leaves the router preset untouched and
    demotes the preset's provider into ``llm_profiles``. Preserve that shape so
    a persist/reload cycle keeps the same effective ladder. Otherwise clear the
    profile and let the inline ``tiers`` table govern. Only fires when the
    payload states both values explicitly, so an env-provided provider can
    never trigger a spurious clear.
    """
    router = builder.payload.get("squilla_router")
    if not isinstance(router, dict):
        return
    profile = router.get("tier_profile")
    if not isinstance(profile, str) or not profile.strip():
        return
    llm = builder.payload.get("llm")
    if not isinstance(llm, dict):
        return
    provider = llm.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        return
    if profile.strip().lower() == provider.strip().lower():
        return
    normalized_profile = profile.strip().lower()
    profiles = builder.payload.get("llm_profiles")
    if isinstance(profiles, dict) and any(
        str(key or "").strip().lower() == normalized_profile for key in profiles
    ):
        return

    router.pop("tier_profile", None)
    builder.changes.append(
        f"squilla_router.tier_profile: {profile!r} cleared "
        f"(no longer matches llm.provider {provider!r})"
    )
    builder.warnings.append(
        "squilla_router.tier_profile no longer matches llm.provider and was "
        "cleared; the router keeps using the inline tiers table"
    )


def _migrate_v1_llm_ensemble_legacy_timeouts(builder: _MigrationBuilder) -> None:
    """Version 1: bump matching legacy llm_ensemble timeout defaults to 3600s."""
    llm_ensemble = builder.payload.get("llm_ensemble")
    if not isinstance(llm_ensemble, dict):
        return

    proposer_timeout = _legacy_llm_ensemble_timeout_number(
        llm_ensemble.get("proposer_timeout_seconds")
    )
    aggregator_timeout = _legacy_llm_ensemble_timeout_number(
        llm_ensemble.get("aggregator_timeout_seconds")
    )
    if (
        proposer_timeout is not None
        and aggregator_timeout is not None
        and proposer_timeout == aggregator_timeout
        and proposer_timeout in _LEGACY_LLM_ENSEMBLE_TIMEOUT_SECONDS
    ):
        for leaf in ("proposer_timeout_seconds", "aggregator_timeout_seconds"):
            llm_ensemble[leaf] = _DEFAULT_LLM_ENSEMBLE_TIMEOUT_SECONDS
            builder.changes.append(
                "llm_ensemble."
                f"{leaf}: {proposer_timeout:g} -> "
                f"{_DEFAULT_LLM_ENSEMBLE_TIMEOUT_SECONDS:g}"
            )


# One-time value migrations, walked in ascending version order. An entry with
# version N runs only when the payload's config_version stamp is below N.
# Keep versions strictly increasing and cap them at LATEST_CONFIG_VERSION.
_MIGRATIONS: list[tuple[int, Callable[[_MigrationBuilder], None]]] = [
    (1, _migrate_v1_llm_ensemble_legacy_timeouts),
]


def backup_and_write_migrated_config(
    path: str | Path,
    payload: dict[str, Any],
    result: ConfigMigrationResult,
) -> Path:
    """Back up and atomically replace a migrated user config file."""
    target = Path(path)
    backup = make_config_backup(target)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            tomli_w.dump(payload, fh)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    os.chmod(target, 0o600)
    logging.getLogger(__name__).warning(
        "OpenSquilla config migrated for 0.2.0 schema",
        extra={
            "path": str(target),
            "backup": str(backup),
            "changes": list(result.changes),
            "removed_fields": list(result.removed_fields),
            "warnings": list(result.warnings),
        },
    )
    return backup


def make_config_backup(target: str | Path) -> Path:
    """Create a collision-safe 0600 backup next to a config file."""
    source = Path(target)
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
    data = source.read_bytes()

    for attempt in range(1000):
        suffix = "" if attempt == 0 else f".{attempt}"
        backup = source.with_name(f"{source.name}.backup.{stamp}{suffix}")
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except Exception:
            try:
                os.unlink(backup)
            except OSError:
                pass
            raise
        backup.chmod(0o600)
        return backup

    raise FileExistsError(f"Could not create unique backup for {source}")
