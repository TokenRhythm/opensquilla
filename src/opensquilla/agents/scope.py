"""Agent scope — per-agent data directory resolution."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import structlog

from opensquilla.session.keys import normalize_agent_id

log = structlog.get_logger(__name__)


def default_opensquilla_home() -> Path:
    """Return the user-level OpenSquilla home directory.

    Delegates to :func:`opensquilla.paths.default_opensquilla_home` so the
    ``OPENSQUILLA_STATE_DIR`` env override is honored uniformly.
    """
    from opensquilla.paths import default_opensquilla_home as _impl

    return _impl()


def default_state_dir() -> Path:
    """Return the default internal state directory."""
    return default_opensquilla_home() / "state"


def default_workspace_dir() -> Path:
    """Return the default visible agent workspace directory."""
    return default_opensquilla_home() / "workspace"


def _config_value(config: object | None, name: str) -> str | None:
    value = getattr(config, name, None) if config is not None else None
    return value if isinstance(value, str) and value else None


def _field_value(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _string_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _configured_agent_field(
    config: object | None,
    normalized_agent_id: str,
    field_name: str,
) -> object | None:
    if config is None or normalized_agent_id == "main":
        return None
    agents = getattr(config, "agents", None)
    if isinstance(agents, Mapping):
        entry = agents.get(normalized_agent_id)
        if entry is None or not bool(_field_value(entry, "enabled", True)):
            return None
        return _field_value(entry, field_name)
    if not isinstance(agents, list | tuple):
        return None
    for entry in agents:
        if normalize_agent_id(str(_field_value(entry, "id", ""))) != normalized_agent_id:
            continue
        if not bool(_field_value(entry, "enabled", True)):
            return None
        return _field_value(entry, field_name)
    return None


def _configured_agent_workspace(config: object | None, normalized_agent_id: str) -> Path | None:
    workspace = _configured_agent_field(config, normalized_agent_id, "workspace")
    return Path(str(workspace)).expanduser() if workspace else None


def _configured_agent_model(config: object | None, normalized_agent_id: str) -> str | None:
    return _string_value(_configured_agent_field(config, normalized_agent_id, "model"))


def resolve_agent_state_dir(
    agent_id: str,
    config: object | None = None,
    *,
    state_dir: str | Path | None = None,
) -> Path:
    """Return the internal state directory for an agent."""
    base = state_dir or _config_value(config, "state_dir") or default_state_dir()
    return Path(base) / "agents" / normalize_agent_id(agent_id)


def resolve_agent_workspace_dir(
    agent_id: str,
    config: object | None = None,
    *,
    state_dir: str | Path | None = None,
) -> Path:
    """Return the visible workspace directory for an agent.

    When no workspace is configured, fall back to OpenSquilla's user-level
    workspace directory instead of internal state.
    """
    workspace = _config_value(config, "workspace_dir")
    normalized = normalize_agent_id(agent_id)
    configured = _configured_agent_workspace(config, normalized)
    if configured is not None:
        return configured
    if workspace:
        root = Path(workspace)
        return root if normalized == "main" else root / "agents" / normalized
    root = default_workspace_dir()
    return root if normalized == "main" else root / "agents" / normalized


def resolve_agent_model(
    agent_id: str,
    config: object | None = None,
    *,
    explicit_model: object | None = None,
    session_model: object | None = None,
) -> str | None:
    """Resolve the model for an agent turn.

    Precedence is explicit call override, persisted session model, then the
    durable agent registry default from ``config.agents``.
    """
    return (
        _string_value(explicit_model)
        or _string_value(session_model)
        or _configured_agent_model(config, normalize_agent_id(agent_id))
    )


def resolve_agent_memory_source_dir(
    agent_id: str,
    config: object | None = None,
    *,
    source: str = "state",
    state_dir: str | Path | None = None,
) -> Path:
    """Return the Markdown memory source root for an agent."""
    return resolve_agent_source_paths(
        agent_id, config, source=source, state_dir=state_dir,
    ).memory_root


@dataclass(frozen=True)
class AgentSourcePaths:
    """Existing independent bootstrap, MEMORY.md, and note sources; no relocation."""

    bootstrap_dir: Path
    memory_root: Path
    notes_dir: Path


def resolve_agent_source_paths(
    agent_id: str,
    config: object | None = None,
    *,
    source: str | None = None,
    state_dir: str | Path | None = None,
) -> AgentSourcePaths:
    normalized = normalize_agent_id(agent_id)
    bootstrap = resolve_agent_workspace_dir(normalized, config)
    selected = source or getattr(getattr(config, "memory", None), "source", "state")
    if selected == "workspace":
        return AgentSourcePaths(bootstrap, bootstrap, bootstrap / "memory")
    if selected != "state":
        raise ValueError("memory source must be 'state' or 'workspace'")
    external_notes = os.environ.get("OPENSQUILLA_MEMORY_DIR") if normalized == "main" else None
    if external_notes:
        # Preserve the existing manager's ENV branch: only notes move, and
        # MEMORY.md uses the default Agent state root rather than config.state_dir.
        memory_root = resolve_agent_data_dir(normalized)
        notes = Path(external_notes).expanduser()
    else:
        state_base = state_dir or _config_value(config, "state_dir")
        memory_root = resolve_agent_data_dir(normalized, state_base)
        notes = resolve_agent_memory_dir(normalized, state_base)
    return AgentSourcePaths(bootstrap, memory_root, notes)


def resolve_agent_data_dir(agent_id: str, base: str | Path | None = None) -> Path:
    """Return the data directory for an agent: {base}/agents/{normalized_id}/"""
    return resolve_agent_state_dir(agent_id, state_dir=base)


def resolve_agent_memory_db(agent_id: str, base: str | Path | None = None) -> Path:
    """Return the memory DB path for an agent."""
    return resolve_agent_data_dir(agent_id, base) / "memory.db"


def resolve_agent_memory_dir(agent_id: str, base: str | Path | None = None) -> Path:
    """Return the memory files directory for an agent."""
    return resolve_agent_data_dir(agent_id, base) / "memory"


def maybe_migrate_legacy_memory(base: str = "data") -> None:
    """One-time migration: move data/memory.db + data/memory/ → data/agents/main/."""
    legacy_db = Path(base) / "memory.db"
    target_dir = resolve_agent_data_dir("main", base)

    if not legacy_db.exists():
        return
    if (target_dir / "memory.db").exists():
        return

    target_dir.mkdir(parents=True, exist_ok=True)

    for suffix in ("", "-wal", "-shm"):
        src = Path(f"{legacy_db}{suffix}")
        if src.exists():
            src.rename(target_dir / f"memory.db{suffix}")

    legacy_dir = Path(base) / "memory"
    if legacy_dir.is_dir():
        legacy_dir.rename(target_dir / "memory")

    log.info("legacy_memory_migrated", target=str(target_dir))
