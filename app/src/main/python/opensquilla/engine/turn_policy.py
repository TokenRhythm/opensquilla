"""Pure turn policy resolution helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from opensquilla.engine.types import AgentConfig


@dataclass(frozen=True)
class TurnPolicy:
    max_iterations: int
    max_iterations_source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_turn_policy(
    *,
    session_key: str,
    explicit_max_iterations: int | None = None,
    session_manager: Any = None,
    gateway_config: Any = None,
    env: Mapping[str, str] | None = None,
    agent_default: int | None = None,
) -> TurnPolicy:
    """Resolve loop limits with the existing precedence order."""

    if explicit_max_iterations is not None:
        if _non_bool_int(explicit_max_iterations) and explicit_max_iterations >= 0:
            return TurnPolicy(int(explicit_max_iterations), "explicit argument")
        raise ValueError("max_iterations must be an integer >= 0")

    session_value = _session_agent_max_iterations(session_manager, session_key)
    if _non_bool_int(session_value) and session_value >= 0:
        return TurnPolicy(int(session_value), "session config")

    raw_env = (env or os.environ).get("OPENSQUILLA_AGENT_MAX_ITERATIONS")
    if raw_env is not None and raw_env.strip():
        try:
            env_value = int(raw_env.strip())
        except ValueError:
            env_value = None
        if env_value is not None and env_value >= 0:
            return TurnPolicy(env_value, "env OPENSQUILLA_AGENT_MAX_ITERATIONS")

    config_value = getattr(gateway_config, "agent_max_iterations", None)
    if isinstance(config_value, int) and not isinstance(config_value, bool):
        config_iterations = int(config_value)
        if config_iterations >= 0:
            return TurnPolicy(config_iterations, "gateway config")

    return TurnPolicy(
        AgentConfig().max_iterations if agent_default is None else int(agent_default),
        "AgentConfig default",
    )


def _session_agent_max_iterations(session_manager: Any, session_key: str) -> Any:
    getter = getattr(session_manager, "get_session_config", None)
    if not callable(getter):
        return None
    try:
        session_cfg = getter(session_key)
    except Exception:  # noqa: BLE001 - resolver fallback mirrors runtime behavior
        return None
    return getattr(session_cfg, "agent_max_iterations", None) if session_cfg is not None else None


def _non_bool_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int)
