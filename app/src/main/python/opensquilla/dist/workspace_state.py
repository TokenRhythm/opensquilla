"""workspace-state.json builder.

Emits a reproducible, versioned inventory of the current opensquilla install:

    {
      "schema_version": 1,
      "opensquilla_version": "<pyproject version>",
      "python_requires": "<pyproject requires-python>",
      "bundled_channels": [...],
      "bundled_tools": [...],
      "gateway_defaults": {"listen": "127.0.0.1", "port": 18791}
    }

Reproducibility contract
------------------------

This module takes NO live-environment input. It reads only:

1. Installed package metadata via ``importlib.metadata`` (static, frozen at
   install time).
2. Hard-coded constants for bundled-channels and bundled-tools (edited
   deliberately when new surface lands).
3. Hard-coded gateway safety defaults.

No timestamps. No UUIDs. No environment variables. No filesystem paths.
Two consecutive calls from the same installed distribution produce
byte-identical JSON.

Secret-hygiene contract
-----------------------

No value in the payload is derived from ``os.environ`` or any file outside
this module and the installed package's metadata. No key whose name
matches ``*_key`` / ``*_token`` / ``*_secret`` appears in the schema.
"""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError
from importlib.metadata import metadata as _pkg_metadata

SCHEMA_VERSION = 1

# Bundled channel adapters. Channels are listed by adapter module name
# under ``opensquilla.channels``. Helpers (``_util``, ``manager``, ``types``)
# are intentionally excluded — they are infra, not channels.
BUNDLED_CHANNELS: tuple[str, ...] = (
    "dingtalk",
    "discord",
    "feishu",
    "matrix",
    "qq",
    "slack",
    "telegram",
    "terminal",
    "wecom",
    "websocket",
)

# Bundled built-in tools. Tools are listed by module name under
# ``opensquilla.tools.builtin``. ``shell_policy`` is excluded as a helper
# consumed by ``shell``.
BUNDLED_TOOLS: tuple[str, ...] = (
    "admin",
    "agent",
    "agents",
    "code_exec",
    "filesystem",
    "git",
    "media",
    "memory_tools",
    "messaging",
    "nodes",
    "patch",
    "session_search",
    "sessions",
    "shell",
    "skill_tools",
    "web",
    "web_fetch",
)

# Gateway safety defaults that every install surface (launchd plist,
# systemd --user unit, Windows Task Scheduler XML, Docker image) echoes.
# Packaging-side enforcement keeps these values consistent across release surfaces.
GATEWAY_DEFAULTS: dict[str, object] = {
    "listen": "127.0.0.1",
    "port": 18791,
}

# Fallback version used only when the package is not installed (editable
# dev tree without metadata). Production installs always read from
# ``importlib.metadata``.
_FALLBACK_VERSION = "0.0.0+unknown"
_FALLBACK_PYTHON_REQUIRES = ">=3.12"


def _read_package_metadata() -> tuple[str, str]:
    """Return (version, python_requires) from installed opensquilla metadata."""

    try:
        meta = _pkg_metadata("opensquilla")
    except PackageNotFoundError:
        return _FALLBACK_VERSION, _FALLBACK_PYTHON_REQUIRES
    version = meta["Version"] or _FALLBACK_VERSION
    python_requires = meta.get("Requires-Python") or _FALLBACK_PYTHON_REQUIRES
    return version, python_requires


def build_workspace_state() -> dict[str, object]:
    """Return the workspace-state payload as a plain dict.

    The returned dict is deliberately simple (no pydantic model) so the
    JSON serialisation is trivially reproducible via
    ``json.dumps(..., sort_keys=True, indent=2)``.
    """

    opensquilla_version, python_requires = _read_package_metadata()
    return {
        "schema_version": SCHEMA_VERSION,
        "opensquilla_version": opensquilla_version,
        "python_requires": python_requires,
        "bundled_channels": list(BUNDLED_CHANNELS),
        "bundled_tools": list(BUNDLED_TOOLS),
        "gateway_defaults": dict(GATEWAY_DEFAULTS),
    }


def to_json(state: dict[str, object] | None = None) -> str:
    """Serialise the workspace state as reproducible JSON.

    Uses ``sort_keys=True`` + ``indent=2`` + a trailing newline so the
    output is stable across Python versions and easy to diff.
    """

    payload = state if state is not None else build_workspace_state()
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"
