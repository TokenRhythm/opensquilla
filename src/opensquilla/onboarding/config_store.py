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
import io
import os
import stat
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
from opensquilla.paths import default_opensquilla_home, native_io_path

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


@dataclass(frozen=True)
class _StagedFileMutation:
    """One filesystem change with a staged replacement and in-memory rollback."""

    target: Path
    replacement_path: Path | None
    original_bytes: bytes | None
    original_mode: int | None


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
    if native_io_path(cwd_candidate).is_file():
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
    target_io = native_io_path(target)
    if not target_io.exists():
        cfg = GatewayConfig()
        _mark_env_absorbed_runtime_secrets(cfg, None)
        cfg.config_path = str(target)
        _remember_load_baseline(cfg)
        return cfg
    with target_io.open("rb") as fh:
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


def _logical_path_from_io(path: str | Path) -> Path:
    """Strip an internal Windows extended-length prefix from a public path."""

    value = os.fspath(path)
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[len("\\\\?\\UNC\\") :]
        elif value.startswith("\\\\?\\"):
            value = value[len("\\\\?\\") :]
    return Path(value)


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


def _remove_backup_paths(payload: Any, remove_paths: tuple[tuple[str, ...], ...]) -> bool:
    """Remove exact config paths from a parsed managed backup."""

    if not isinstance(payload, dict):
        return False
    changed = False
    for path in remove_paths:
        if _get_path(payload, path) is None:
            continue
        _remove_path(payload, path)
        changed = True
    return changed


def _atomic_write_toml(target: Path, payload: dict[str, Any]) -> None:
    """Replace one TOML file atomically with owner-only permissions."""

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=os.fspath(native_io_path(target.parent)),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            tomli_w.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, native_io_path(target))
    except Exception:
        _remove_staged_path(Path(tmp_name))
        raise


def _toml_bytes(payload: dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    tomli_w.dump(payload, buffer)
    return buffer.getvalue()


def _stage_bytes(target: Path, payload: bytes, *, mode: int, suffix: str) -> Path:
    """Write and fsync one same-directory temporary file without committing it."""

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=suffix,
        dir=os.fspath(native_io_path(target.parent)),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
    except Exception:
        _remove_staged_path(Path(tmp_name))
        raise
    return Path(tmp_name)


def _stage_file_mutation(
    target: Path,
    *,
    original_bytes: bytes | None,
    original_mode: int | None,
    replacement_bytes: bytes | None,
) -> _StagedFileMutation:
    """Stage desired contents while keeping the exact rollback bytes in memory."""

    replacement_path: Path | None = None
    try:
        if replacement_bytes is not None:
            replacement_path = _stage_bytes(
                target,
                replacement_bytes,
                mode=0o600,
                suffix=".replacement.tmp",
            )
    except Exception:
        if replacement_path is not None:
            _remove_staged_path(replacement_path)
        raise
    return _StagedFileMutation(
        target=target,
        replacement_path=replacement_path,
        original_bytes=original_bytes,
        original_mode=original_mode,
    )


def _remove_staged_path(path: Path) -> None:
    """Remove one staged file, retrying once and never claiming false success."""

    last_error: OSError | None = None
    for _attempt in range(2):
        try:
            os.unlink(native_io_path(path))
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def _cleanup_staged_mutations(mutations: list[_StagedFileMutation]) -> None:
    errors: list[OSError] = []
    for mutation in mutations:
        staged = mutation.replacement_path
        if staged is None:
            continue
        try:
            _remove_staged_path(staged)
        except OSError as exc:
            errors.append(exc)
            log.error(
                "onboarding.config_transaction_temp_cleanup_failed",
                path=str(staged),
                error_type=type(exc).__name__,
            )
    if errors:
        raise OSError("One or more staged config files could not be removed") from errors[0]


def _restore_file_mutation(mutation: _StagedFileMutation) -> None:
    target_io = native_io_path(mutation.target)
    if mutation.original_bytes is None:
        target_io.unlink(missing_ok=True)
        return
    restore_path: Path | None = None
    for attempt in range(2):
        try:
            restore_path = _stage_bytes(
                mutation.target,
                mutation.original_bytes,
                mode=(
                    mutation.original_mode
                    if mutation.original_mode is not None
                    else 0o600
                ),
                suffix=".restore.tmp",
            )
            break
        except OSError:
            if attempt:
                raise
    assert restore_path is not None
    last_error: OSError | None = None
    for _attempt in range(2):
        try:
            os.replace(native_io_path(restore_path), target_io)
            return
        except OSError as exc:
            last_error = exc
    _remove_staged_path(restore_path)
    assert last_error is not None
    raise last_error


def _rollback_file_mutations(mutations: list[_StagedFileMutation]) -> list[OSError]:
    errors: list[OSError] = []
    for mutation in reversed(mutations):
        try:
            _restore_file_mutation(mutation)
        except OSError as exc:
            errors.append(exc)
            log.error(
                "onboarding.config_transaction_rollback_failed",
                path=str(mutation.target),
                error_type=type(exc).__name__,
            )
    return errors


def _commit_file_mutations(mutations: list[_StagedFileMutation]) -> None:
    """Commit all staged files, rolling back every earlier target on failure."""

    committed: list[_StagedFileMutation] = []
    try:
        for mutation in mutations:
            target_io = native_io_path(mutation.target)
            if mutation.replacement_path is None:
                target_io.unlink()
            else:
                os.replace(native_io_path(mutation.replacement_path), target_io)
            committed.append(mutation)
    except Exception as exc:
        rollback_errors = _rollback_file_mutations(committed)
        try:
            _cleanup_staged_mutations(mutations)
        except OSError as cleanup_error:
            rollback_errors.append(cleanup_error)
        if rollback_errors:
            raise OSError(
                "Config transaction failed and rollback or staged-file cleanup was incomplete"
            ) from exc
        raise


def _stage_managed_config_backup_mutations(
    target: Path,
    *,
    credential_redaction: CredentialBackupRedaction | None = None,
    remove_paths: tuple[tuple[str, ...], ...] = (),
) -> list[_StagedFileMutation]:
    """Prepare backup rewrites/deletions without changing any managed file."""

    prefix = f"{target.name}.backup."
    backup_paths = sorted(
        target.parent / candidate.name
        for candidate in native_io_path(target.parent).iterdir()
        if candidate.name.startswith(prefix)
    )
    staged: list[_StagedFileMutation] = []
    try:
        for backup_path in backup_paths:
            backup_io = native_io_path(backup_path)
            if backup_io.is_symlink() or not backup_io.is_file():
                raise OSError(
                    f"Refusing to rewrite non-regular config backup: {backup_path}"
                )
            original_bytes = backup_io.read_bytes()
            original_mode = stat.S_IMODE(backup_io.stat().st_mode)
            try:
                payload = tomllib.loads(original_bytes.decode("utf-8"))
            except (tomllib.TOMLDecodeError, UnicodeDecodeError, ValueError) as exc:
                # The raw bytes are staged as the rollback copy before the
                # corrupt backup is deleted during commit.
                log.warning(
                    "onboarding.config_backup_unparseable_staged_for_deletion",
                    path=str(backup_path),
                    error=str(exc),
                    action="deleting corrupt managed backup if the transaction commits",
                )
                staged.append(
                    _stage_file_mutation(
                        backup_path,
                        original_bytes=original_bytes,
                        original_mode=original_mode,
                        replacement_bytes=None,
                    )
                )
                continue
            changed = bool(
                credential_redaction is not None
                and _redact_backup_credentials(payload, credential_redaction)
            )
            changed = _remove_backup_paths(payload, remove_paths) or changed
            if changed:
                staged.append(
                    _stage_file_mutation(
                        backup_path,
                        original_bytes=original_bytes,
                        original_mode=original_mode,
                        replacement_bytes=_toml_bytes(payload),
                    )
                )
    except Exception:
        _cleanup_staged_mutations(staged)
        raise
    return staged


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
    fd = os.open(native_io_path(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
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
    still equals the applied env value. A missing serialized field also
    represents an applied empty string because the TOML payload drops empty
    values; an operator edit since boot still wins.
    Shared by the sparse persister here and the gateway RPC full-dump
    persist (``rpc_config._persist_config``).
    """
    overrides = getattr(config, "runtime_field_overrides", None)
    if overrides is None:
        return
    for path, (stored, applied) in overrides().items():
        current = _get_dotted(dump, path)
        if current == applied or (
            current is None and applied == "" and stored not in (None, "")
        ):
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
    target_io = native_io_path(target)
    if target_io.is_file():
        try:
            with target_io.open("rb") as fh:
                raw = tomllib.load(fh)
        # UnicodeDecodeError is a ValueError subclass, but list it explicitly:
        # a config corrupted with non-UTF-8 bytes (seen when an agent edited
        # the file through a shell with a legacy codepage) must take this
        # recovery branch — back up the corrupt bytes, then rewrite valid
        # UTF-8 from the in-memory config — and never propagate as a crash.
        except (tomllib.TOMLDecodeError, UnicodeDecodeError, ValueError) as exc:
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
    remove_paths: tuple[str, ...] = (),
) -> PersistResult:
    resolved = _resolve_path(path)
    # The instance baseline only describes the file the config was loaded
    # from; a save-as to a different path must not diff against it. A config
    # with no associated path is different: its first successful save adopts
    # the resolved target and the committed snapshot becomes its baseline.
    establish_path = not bool(config.config_path)
    same_path = establish_path or config.config_path == str(resolved)
    target = resolved
    target_io = native_io_path(target)
    if target_io.is_symlink():
        # Write through the symlink: update the real file in place so the
        # link (and anything else resolving through it) survives the swap.
        target = _logical_path_from_io(target_io.resolve())
        target_io = native_io_path(target)

    target_io.parent.mkdir(parents=True, exist_ok=True)
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
        exact_remove_paths = tuple(
            tuple(part for part in dotted.split(".") if part)
            for dotted in remove_paths
            if dotted
        )
        # A capability reset removes the stale section first, then writes the
        # small explicit intent fields below (for example ``enabled=false``).
        # Hidden advanced values and credentials therefore cannot survive.
        for remove_path in exact_remove_paths:
            _remove_path(merged, remove_path)

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

        backup_path: Path | None = None
        scrub_managed_backups = (
            backup_credential_redaction is not None or bool(exact_remove_paths)
        )
        if scrub_managed_backups:
            # A reset/credential clear is one multi-file transaction: every
            # backup rewrite/deletion and the final current config are fully
            # serialized and fsynced before the first managed path changes.
            # If any commit step fails, earlier paths are restored byte-for-byte
            # from in-memory originals, so removed credentials never sit in a
            # plaintext rollback temp during the successful path.
            staged = _stage_managed_config_backup_mutations(
                target,
                credential_redaction=backup_credential_redaction,
                remove_paths=exact_remove_paths,
            )
            try:
                current_original = target_io.read_bytes() if target_io.exists() else None
                current_mode = (
                    stat.S_IMODE(target_io.stat().st_mode)
                    if current_original is not None
                    else None
                )
                staged.append(
                    _stage_file_mutation(
                        target,
                        original_bytes=current_original,
                        original_mode=current_mode,
                        replacement_bytes=_toml_bytes(merged),
                    )
                )
            except Exception:
                _cleanup_staged_mutations(staged)
                raise
            _commit_file_mutations(staged)
            backup = False
        else:
            if backup and target_io.exists():
                backup_path = make_config_backup(target)
            _atomic_write_toml(target, merged)

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
