"""Per-agent run bookkeeping for the offline user-profile producer.

Mirrors ``self_learning.state.TrainState``: a tiny JSON sidecar the gates read
to enforce the cooldown, plus a consecutive-failure counter so a provider that
keeps erroring backs off instead of retrying every dream. Timestamps use the
same ``%Y-%m-%dT%H:%M:%SZ`` format ``self_learning.gates`` parses.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from opensquilla.squilla_router.user_profile.store import profiles_dir

_STATE_FILENAME = ".profile_state.json"


def utc_now_ts() -> str:
    """Current UTC time in the gate-parseable format."""

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ProfileRunState:
    """Persisted producer bookkeeping for one agent."""

    last_attempt_ts: str | None = None  # last gated production attempt
    last_run_ts: str | None = None  # last successful production
    last_version: str | None = None  # last version written
    consecutive_failures: int = 0

    def to_json(self) -> dict:
        return asdict(self)


def _state_path(agent_id: str, home: Path | None = None) -> Path:
    return profiles_dir(agent_id, home) / _STATE_FILENAME


def load_run_state(agent_id: str, home: Path | None = None) -> ProfileRunState:
    path = _state_path(agent_id, home)
    if not path.is_file():
        return ProfileRunState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ProfileRunState()
    if not isinstance(payload, dict):
        return ProfileRunState()
    allowed = ProfileRunState().to_json().keys()
    return ProfileRunState(**{k: v for k, v in payload.items() if k in allowed})


def save_run_state(state: ProfileRunState, agent_id: str, home: Path | None = None) -> Path:
    path = _state_path(agent_id, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


__all__ = [
    "ProfileRunState",
    "load_run_state",
    "save_run_state",
    "utc_now_ts",
]
