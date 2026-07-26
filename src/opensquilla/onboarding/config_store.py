"""Config persistence: load/validate/atomic sparse write with backup + 0600 mode.

Persistence is diff-based: ``persist_config`` writes only the fields the
caller actually changed, merged onto the current on-disk TOML. Values that
merely reflect environment variables or built-in defaults (for example an
``OPENSQUILLA_AUTH_TOKEN`` picked up by a pydantic-settings default factory)
never leak into the file, small configs stay small, and non-conflicting
concurrent edits made by another writer between load and persist survive.

TOML comments are not preserved across a save: ``tomli_w`` has no comment
support, so a save rewrites the file without them (pre-existing limitation).
"""

from __future__ import annotations

import copy
import os
import tempfile
import types
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Union, cast, get_args, get_origin

import structlog
import tomli_w
from pydantic import BaseModel

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found, no-redef]

from opensquilla.gateway.config import (
    LEGACY_DEFAULT_LLM_PROVIDER,
    GatewayConfig,
    LlmProviderConfig,
)
from opensquilla.gateway.config_migration import (
    backup_and_write_migrated_config,
    make_config_backup,
    migrate_config_payload,
)
from opensquilla.paths import default_opensquilla_home

log = structlog.get_logger(__name__)

# Persist baselines are INSTANCE-scoped: load_config (and persist_config)
# snapshot the model's TOML dump onto the GatewayConfig object itself
# (``_persist_baseline``), so a save diffs exactly what the caller mutated
# since ITS load. A path-keyed global registry is deliberately avoided: two
# live objects for the same file would overwrite each other's snapshot, and
# the second object's save would diff against the first one's post-persist
# state — silently reverting (and baking in) the other writer's changes.

# Top-level fields that record runtime provenance rather than operator
# choices; they are never diffed into the persisted file (whatever the raw
# file already contains for them is left untouched).
_NON_PERSISTED_TOP_LEVEL_FIELDS = frozenset({"config_path"})


class _Removed:
    """Sentinel diff node: the key exists in the baseline but not anymore."""


_REMOVED = _Removed()


@dataclass(frozen=True)
class _SetValue:
    """Diff leaf: replace the raw value wholesale with ``value``."""

    value: Any


@dataclass(frozen=True)
class PersistResult:
    path: Path
    backup_path: Path | None
    restart_required: bool
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CredentialBackupRedaction:
    """Provider-scoped credential fields a clear must remove from backups."""

    provider_id: str


def resolve_config_path(path: str | Path | None = None) -> tuple[Path, str]:
    """Return (resolved_path, source) using gateway-equivalent precedence.

    source is one of: "explicit", "env", "cwd", "home".
    Mirrors GatewayConfig.load (see gateway/config.py) so the CLI never
    silently writes to a different file than the gateway will read.
    """
    if path is not None:
        return Path(path).expanduser(), "explicit"
    explicit = os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH")
    if explicit:
        return Path(explicit).expanduser(), "env"
    cwd_candidate = Path.cwd() / "opensquilla.toml"
    if cwd_candidate.is_file():
        return cwd_candidate, "cwd"
    return default_opensquilla_home() / "config.toml", "home"


def default_config_path() -> Path:
    return resolve_config_path(None)[0]


def _resolve_path(path: str | Path | None) -> Path:
    return resolve_config_path(path)[0]


def _raw_key_present(raw: Any, path: str) -> bool:
    current = raw
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


# Nested credential fields whose sections are pydantic-settings models: the
# environment can absorb a value into them at load (e.g.
# ``OPENSQUILLA_AUDIO_PROVIDERS__ELEVENLABS__API_KEY``). Each entry is marked
# as a runtime secret when the raw TOML did not supply it, mirroring
# ``llm.api_key``/``search_api_key`` — without the mark an explicit user
# entry equal to the env value diffs as unchanged and is silently dropped
# from the file (the mutations' ``clear_runtime_secret_paths`` calls for
# these exact paths are what make an explicit entry stick).
_ENV_ABSORBED_NESTED_SECRET_SECTIONS = (
    ("audio.providers", "api_key"),
    ("image_generation.providers", "api_key"),
)


def _mark_env_absorbed_runtime_secrets(cfg: GatewayConfig, raw: Any) -> None:
    """Mark credentials that exist on the model only because env supplied them.

    ``raw`` is the TOML payload as read from disk (``None``/``{}`` for a
    missing file). A non-empty credential absent from ``raw`` can only have
    come from the environment; marking it keeps it out of the persist
    baseline so explicit entries — even ones equal to the env value — are
    written to the file, while env-only values never leak into it.
    """
    if cfg.llm.api_key and not _raw_key_present(raw, "llm.api_key"):
        cfg.mark_runtime_secret("llm.api_key")
    if cfg.search_api_key and not _raw_key_present(raw, "search_api_key"):
        cfg.mark_runtime_secret("search_api_key")
    # Memory embedding keys are absorbed from OPENSQUILLA_MEMORY_EMBEDDING__
    # [REMOTE__]API_KEY (MemoryConfig is pydantic-settings with a nested
    # delimiter). Unmarked, a full-model persist writes them verbatim into
    # config.toml.
    embedding = getattr(getattr(cfg, "memory", None), "embedding", None)
    if getattr(embedding, "api_key", "") and not _raw_key_present(
        raw, "memory.embedding.api_key"
    ):
        cfg.mark_runtime_secret("memory.embedding.api_key")
    if getattr(getattr(embedding, "remote", None), "api_key", "") and not _raw_key_present(
        raw, "memory.embedding.remote.api_key"
    ):
        cfg.mark_runtime_secret("memory.embedding.remote.api_key")
    # Auth secrets are absorbed from OPENSQUILLA_AUTH_TOKEN / _PASSWORD into
    # the AuthConfig pydantic-settings model. Without marking them, any
    # full-dump persist bakes the env-sourced token into config.toml, where
    # it then silently overrides later env rotation.
    auth = getattr(cfg, "auth", None)
    if getattr(auth, "token", "") and not _raw_key_present(raw, "auth.token"):
        cfg.mark_runtime_secret("auth.token")
    if getattr(auth, "password", "") and not _raw_key_present(raw, "auth.password"):
        cfg.mark_runtime_secret("auth.password")
    for section_path, key in _ENV_ABSORBED_NESTED_SECRET_SECTIONS:
        section: Any = cfg
        for part in section_path.split("."):
            section = getattr(section, part, None)
        if not isinstance(section, BaseModel):
            continue
        for provider_name in type(section).model_fields:
            path = f"{section_path}.{provider_name}.{key}"
            provider_cfg = getattr(section, provider_name, None)
            if getattr(provider_cfg, key, "") and not _raw_key_present(raw, path):
                cfg.mark_runtime_secret(path)


def load_config(
    path: str | Path | None = None,
    *,
    persist_migrations: bool = True,
) -> GatewayConfig:
    target = _resolve_path(path)
    if not target.exists():
        cfg = GatewayConfig()
        _mark_env_absorbed_runtime_secrets(cfg, None)
        cfg.config_path = str(target)
        _remember_load_baseline(cfg)
        return cfg
    with target.open("rb") as fh:
        data = tomllib.load(fh)
    migration = migrate_config_payload(data)
    cfg = GatewayConfig.model_validate(migration.payload)
    if migration.changed and persist_migrations:
        backup_and_write_migrated_config(target, migration.payload, migration)
    _mark_env_absorbed_runtime_secrets(cfg, data)
    cfg.config_path = str(target)
    _remember_load_baseline(cfg, migration.payload)
    return cfg


def validate_config_payload(payload: dict[str, Any]) -> GatewayConfig:
    return GatewayConfig.model_validate(payload)


def _toml_safe(value: Any) -> Any:
    """Recursively coerce model-dump output into TOML-safe primitives."""
    if isinstance(value, dict):
        return {k: _toml_safe(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_toml_safe(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "model_dump"):
        return _toml_safe(value.model_dump(mode="python"))
    return str(value)


def _model_toml_payload(cfg: GatewayConfig) -> dict[str, Any]:
    """Return the model's TOML-oriented dump (pre-coercion python values)."""
    raw = cfg.to_toml_dict() if hasattr(cfg, "to_toml_dict") else cfg.model_dump(
        mode="python", exclude_none=True
    )
    assert isinstance(raw, dict)
    return raw


def _config_to_toml_dict(cfg: GatewayConfig) -> dict[str, Any]:
    """Full TOML-safe dump of a config (diagnostic/round-trip helper)."""
    coerced = _toml_safe(_model_toml_payload(cfg))
    assert isinstance(coerced, dict)
    return coerced


def _redact_backup_credentials(
    payload: Any,
    redaction: CredentialBackupRedaction,
) -> bool:
    """Remove one provider's LLM credential fields from a parsed backup."""

    if not isinstance(payload, dict):
        return False
    provider = str(redaction.provider_id or "").strip().lower()
    if not provider:
        return False

    def clear_section(section: Any) -> bool:
        if not isinstance(section, dict):
            return False
        changed = False
        for key in ("api_key", "api_key_env", "api_key_env_pool"):
            if key in section:
                section.pop(key, None)
                changed = True
        return changed

    changed = False
    llm = payload.get("llm")
    if isinstance(llm, dict):
        stored_provider = str(llm.get("provider") or "").strip().lower()
        if not stored_provider:
            router = payload.get("squilla_router")
            tier_profile = (
                str(router.get("tier_profile") or "").strip().lower()
                if isinstance(router, dict)
                else ""
            )
            legacy_intent = bool(
                {"model", "base_url", "api_key", "api_key_env"} & llm.keys()
            ) or tier_profile == LEGACY_DEFAULT_LLM_PROVIDER
            default_provider = str(
                LlmProviderConfig.model_fields["provider"].default or ""
            ).strip().lower()
            stored_provider = (
                LEGACY_DEFAULT_LLM_PROVIDER if legacy_intent else default_provider
            )
        if stored_provider == provider:
            changed = clear_section(llm) or changed

    profiles = payload.get("llm_profiles")
    if isinstance(profiles, dict):
        for key, profile in profiles.items():
            if str(key or "").strip().lower() == provider:
                changed = clear_section(profile) or changed
    return changed


def _atomic_write_toml(target: Path, payload: dict[str, Any]) -> None:
    """Replace one TOML file atomically with owner-only permissions."""

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            tomli_w.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _sanitize_managed_config_backups(
    target: Path,
    redaction: CredentialBackupRedaction,
) -> None:
    """Remove a cleared credential from every regular managed config backup."""

    prefix = f"{target.name}.backup."
    backup_paths = sorted(
        path for path in target.parent.iterdir() if path.name.startswith(prefix)
    )
    for backup_path in backup_paths:
        if backup_path.is_symlink() or not backup_path.is_file():
            raise OSError(f"Refusing to rewrite non-regular config backup: {backup_path}")
        try:
            with backup_path.open("rb") as fh:
                payload = tomllib.load(fh)
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            # An unparseable backup is useless as a restore source, yet its
            # raw bytes may still hold the credential being cleared in plain
            # text. Deleting it completes the redaction for this file, while
            # aborting would leave the LIVE config holding the secret the
            # user explicitly asked to remove.
            log.warning(
                "onboarding.config_backup_unparseable_deleted",
                path=str(backup_path),
                error=str(exc),
                action="deleting corrupt managed backup so the credential clear proceeds",
            )
            backup_path.unlink(missing_ok=True)
            continue
        if _redact_backup_credentials(payload, redaction):
            _atomic_write_toml(backup_path, payload)


def _remember_load_baseline(
    cfg: GatewayConfig, raw_payload: dict[str, Any] | None = None
) -> None:
    baseline = _model_toml_payload(cfg)
    # Also remember the raw (migrated) TOML payload the file held at load
    # time. If the file vanishes between load and persist (a reset from
    # another session, an operator ``mv``, cleanup during a long wizard run),
    # the sparse diff must be merged onto THIS payload — not onto an empty
    # base, which would silently drop every unchanged loaded section (e.g.
    # the [llm] block and its api_key) from the recreated file. Stored as an
    # instance attribute so mutation clones (``model_copy(deep=True)``)
    # carry it; it never contains env-derived values because it came from
    # disk. ``None`` means the file did not exist at load time.
    cfg.set_persist_snapshot(baseline, raw_payload if raw_payload else None)


def _lock_config_file(fh: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt_mod = cast(Any, msvcrt)
        fh.seek(0)
        msvcrt_mod.locking(fh.fileno(), msvcrt_mod.LK_LOCK, 1)
        return

    import fcntl

    fcntl_mod = cast(Any, fcntl)
    fcntl_mod.flock(fh.fileno(), fcntl_mod.LOCK_EX)


def _unlock_config_file(fh: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt_mod = cast(Any, msvcrt)
        fh.seek(0)
        msvcrt_mod.locking(fh.fileno(), msvcrt_mod.LK_UNLCK, 1)
        return

    import fcntl

    fcntl_mod = cast(Any, fcntl)
    fcntl_mod.flock(fh.fileno(), fcntl_mod.LOCK_UN)


@contextmanager
def _config_write_lock(target: Path) -> Iterator[None]:
    """Serialize read/merge/replace against every shared persister process."""
    lock_path = target.with_name(f".{target.name}.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
    with os.fdopen(fd, "r+b", buffering=0) as fh:
        _lock_config_file(fh)
        try:
            yield
        finally:
            try:
                _unlock_config_file(fh)
            except OSError:
                # Closing the handle releases the OS lock too. An unlock
                # bookkeeping error after replace must not turn a committed
                # config write into an apparent failure.
                pass


def _get_path(obj: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = obj
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _set_path(obj: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    if not path:
        return
    current = obj
    for part in path[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[path[-1]] = value


def _remove_path(obj: dict[str, Any], path: tuple[str, ...]) -> None:
    if not path:
        return
    current: Any = obj
    parents: list[tuple[dict[str, Any], str]] = []
    for part in path[:-1]:
        if not isinstance(current, dict) or not isinstance(current.get(part), dict):
            return
        parents.append((current, part))
        current = current[part]
    if not isinstance(current, dict):
        return
    current.pop(path[-1], None)
    for parent, key in reversed(parents):
        child = parent.get(key)
        if child != {}:
            break
        parent.pop(key, None)


def _get_dotted(obj: dict[str, Any], path: str) -> Any:
    return _get_path(obj, tuple(path.split(".")))


def _set_dotted(obj: dict[str, Any], path: str, value: Any) -> None:
    _set_path(obj, tuple(path.split(".")), value)


def restore_runtime_overrides(dump: dict[str, Any], config: GatewayConfig) -> None:
    """Undo in-place runtime env resolutions before persisting ``dump``.

    ``resolve_llm_runtime_config`` writes provider env overrides (e.g.
    ``OPENAI_BASE_URL`` -> ``llm.base_url``, ``OPENSQUILLA_LLM_PROXY`` ->
    ``llm.proxy``) directly into the live model at boot. Those values must
    never be baked into config.toml by an unrelated save, so each recorded
    override is restored to its stored value — but only while the field
    still equals the applied env value; an operator edit since boot wins.
    Shared by the sparse persister here and the gateway RPC full-dump
    persist (``rpc_config._persist_config``).
    """
    overrides = getattr(config, "runtime_field_overrides", None)
    if overrides is None:
        return
    for path, (stored, applied) in overrides().items():
        if _get_dotted(dump, path) == applied:
            _set_dotted(dump, path, stored)


def _submodel_class(model_cls: type[BaseModel] | None, key: str) -> type[BaseModel] | None:
    """Return the BaseModel subclass behind ``key`` on ``model_cls``, if any.

    Used to decide whether a mapping in the dump is a schema-backed section
    (safe to diff per key: absent keys re-resolve from field defaults/env) or
    a free-form dict value (must be replaced wholesale: a partial write would
    change how the whole value resolves on the next load).
    """
    if model_cls is None:
        return None
    fld = model_cls.model_fields.get(key)
    if fld is None:
        return None
    ann = fld.annotation
    candidates = get_args(ann) if get_origin(ann) in (Union, types.UnionType) else (ann,)
    for candidate in candidates:
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    return None


def _diff_payload(
    current: dict[str, Any],
    baseline: dict[str, Any],
    model_cls: type[BaseModel] | None,
) -> dict[str, Any]:
    """Recursively diff two model dumps.

    Returns a mapping whose values are ``_SetValue`` (write wholesale),
    ``_REMOVED`` (drop the key), or a nested diff mapping for schema-backed
    sections whose dict values changed only partially. Free-form dict values
    (e.g. router tier tables) are treated as leaves and replaced wholesale so
    the persisted value never depends on a default factory filling in the
    untouched part.
    """
    diff: dict[str, Any] = {}
    for key, cur in current.items():
        if key not in baseline:
            diff[key] = _SetValue(cur)
            continue
        base = baseline[key]
        sub_cls = _submodel_class(model_cls, key)
        if sub_cls is not None and isinstance(cur, dict) and isinstance(base, dict):
            sub = _diff_payload(cur, base, sub_cls)
            if sub:
                diff[key] = sub
        elif cur != base:
            diff[key] = _SetValue(cur)
    for key in baseline:
        if key not in current:
            diff[key] = _REMOVED
    return diff


def _merge_diff(base: dict[str, Any], diff: dict[str, Any]) -> None:
    """Apply a ``_diff_payload`` result onto a raw TOML mapping in place.

    Raw keys not named by the diff are left untouched, so values another
    writer put on disk (or hand-edits the model never saw) survive a save.
    """
    for key, node in diff.items():
        if isinstance(node, _SetValue):
            base[key] = _toml_safe(node.value)
        elif isinstance(node, _Removed):
            base.pop(key, None)
        else:  # nested diff mapping
            child = base.get(key)
            if isinstance(child, dict):
                _merge_diff(child, node)
                continue
            fresh: dict[str, Any] = {}
            _merge_diff(fresh, node)
            if fresh:
                base[key] = fresh


def _persist_plan(
    target: Path, config: GatewayConfig, *, use_instance_baseline: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(baseline_dump, merge_base)`` for a sparse persist.

    ``merge_base`` is the current on-disk TOML (migrated, empty if absent or
    unusable); the caller merges the current-vs-baseline diff onto it. The
    baseline is, in order of preference: the snapshot carried by the config
    instance itself (captured at ``load_config`` or the instance's previous
    persist — the diff is exactly what THIS caller changed), a model rebuilt
    from the on-disk TOML the same way ``load_config`` would build it, or a
    fresh env/default model when the on-disk state is missing or unusable.

    ``use_instance_baseline`` is False for a save-as (the config is being
    persisted to a path other than the one it was loaded from): diffing a
    different target against the instance's own load snapshot would erase
    every loaded value from the copy, so those saves fall through to the
    disk/default baselines exactly like a config that was never loaded.
    """
    instance_baseline = (
        getattr(config, "_persist_baseline", None) if use_instance_baseline else None
    )
    raw: dict[str, Any] | None = None
    disk_usable = True
    if target.is_file():
        try:
            with target.open("rb") as fh:
                raw = tomllib.load(fh)
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            disk_usable = False
            log.warning(
                "onboarding.config_persist_unreadable_toml",
                path=str(target),
                error=str(exc),
                action="rewriting from the in-memory config",
            )
    if raw is not None:
        migration = migrate_config_payload(raw)
        try:
            disk_model = GatewayConfig.model_validate(migration.payload)
        except Exception as exc:
            disk_usable = False
            log.warning(
                "onboarding.config_persist_invalid_existing",
                path=str(target),
                error=type(exc).__name__,
                action="rewriting from the in-memory config",
            )
        else:
            if instance_baseline is not None:
                return copy.deepcopy(instance_baseline), migration.payload
            return _model_toml_payload(disk_model), migration.payload

    merge_base = migrate_config_payload({}).payload
    if disk_usable and instance_baseline is not None:
        raw_base = getattr(config, "_persist_raw_base", None)
        if isinstance(raw_base, dict):
            # The file EXISTED at load time but is gone now (reset from
            # another session, operator ``mv``, cleanup during a long wizard
            # run). Merging the sparse diff onto an empty base would silently
            # drop every unchanged loaded section — including the [llm] block
            # and its api_key — from the recreated file, so rebuild the base
            # from the raw payload the load saw: the recreated file carries
            # the loaded state plus exactly this caller's changes.
            log.warning(
                "onboarding.config_persist_target_vanished",
                path=str(target),
                action="recreating from the load-time contents plus this save's changes",
            )
            return copy.deepcopy(instance_baseline), copy.deepcopy(raw_base)
        # target file simply does not exist yet
        return copy.deepcopy(instance_baseline), merge_base
    return _model_toml_payload(GatewayConfig()), merge_base


def persist_config(
    config: GatewayConfig,
    *,
    path: str | Path | None = None,
    backup: bool = True,
    restart_required: bool = False,
    backup_credential_redaction: CredentialBackupRedaction | None = None,
) -> PersistResult:
    resolved = _resolve_path(path)
    # The instance baseline only describes the file the config was loaded
    # from; a save-as to a different path must not diff against it. A config
    # with no associated path is different: its first successful save adopts
    # the resolved target and the committed snapshot becomes its baseline.
    establish_path = not bool(config.config_path)
    same_path = establish_path or config.config_path == str(resolved)
    target = resolved
    if target.is_symlink():
        # Write through the symlink: update the real file in place so the
        # link (and anything else resolving through it) survives the swap.
        target = target.resolve()

    target.parent.mkdir(parents=True, exist_ok=True)
    with _config_write_lock(target):
        baseline_dump, merged = _persist_plan(
            target, config, use_instance_baseline=same_path
        )
        current_dump = _model_toml_payload(config)
        restore_runtime_overrides(current_dump, config)
        diff = _diff_payload(current_dump, baseline_dump, GatewayConfig)
        for provenance_key in _NON_PERSISTED_TOP_LEVEL_FIELDS:
            diff.pop(provenance_key, None)
        _merge_diff(merged, diff)
        # Force-persisted paths are one-shot explicit mutations. They survive
        # failed writes, but a successful commit consumes them so a later
        # unrelated save cannot overwrite a newer on-disk edit.
        force_paths = config.force_persist_path_segments()
        for force_path in sorted(force_paths):
            forced_value = _get_path(current_dump, force_path)
            if forced_value is None:
                _remove_path(merged, force_path)
            else:
                _set_path(merged, force_path, _toml_safe(forced_value))

        # Re-validate to catch any invariant breakage that survived model_dump.
        GatewayConfig.model_validate(copy.deepcopy(merged))
        next_baseline = copy.deepcopy(current_dump) if same_path else None
        next_raw_base = copy.deepcopy(merged) if same_path else None

        # Credential removal is intentionally stronger than an ordinary config
        # save: do not create a fresh backup containing the just-cleared secret,
        # and scrub matching credential fields from every managed backup while
        # holding the same cross-process config lock.  This runs before the
        # current-file commit, so a sanitization failure leaves live/disk config
        # aligned and the RPC can fail closed.
        if backup_credential_redaction is not None:
            _sanitize_managed_config_backups(target, backup_credential_redaction)
            backup = False

        backup_path: Path | None = None
        if backup and target.exists():
            backup_path = make_config_backup(target)

        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                tomli_w.dump(merged, fh)
                # Flush user-space buffers and force the temp file to stable
                # storage before the rename, so a power loss cannot leave a
                # truncated config behind the atomic swap.
                fh.flush()
                os.fsync(fh.fileno())
            # The temp file already carries the final restrictive mode. Rename
            # is the commit point; no fallible chmod follows it and turns a
            # successful disk commit into an apparent rollback.
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

        config.consume_force_persist_path_segments(force_paths)

        # The file now reflects this instance's state: refresh the instance's
        # baseline so a later save diffs against this committed model.
        if establish_path:
            config.config_path = str(resolved)
        if next_baseline is not None and next_raw_base is not None:
            config._persist_baseline = next_baseline
            config._persist_raw_base = next_raw_base

        log.debug(
            "onboarding.config_persisted",
            path=str(target),
            backup=str(backup_path) if backup_path else None,
            restart_required=restart_required,
        )

        return PersistResult(
            path=target,
            backup_path=backup_path,
            restart_required=restart_required,
            warnings=[],
        )
