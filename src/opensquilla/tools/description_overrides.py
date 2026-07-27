"""Env-gated overrides for model-facing tool and parameter descriptions.

``OPENSQUILLA_TOOL_DESCRIPTION_OVERRIDES`` selects the override source:
unset/"off" keeps the builtin wording byte-identical, "config"/"on" reads the
gateway ``[tools.description_overrides]`` table, and a ``.toml``/``.json``
path loads the table from that file (the file wins over the config table).
Keys name a tool ("exec_command") or a parameter ("exec_command.command");
values replace the matching description verbatim. The engine ships no
override wording — deployments supply it via config. Unrecognized env values
and unreadable or malformed override files raise instead of being silently
ignored so a run manifest cannot record an override the run did not actually
apply.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_TOOL_DESCRIPTION_OVERRIDES_ENV = "OPENSQUILLA_TOOL_DESCRIPTION_OVERRIDES"
_TOOL_DESCRIPTION_OVERRIDES_OFF = {"off", "0", "false", "no"}
_TOOL_DESCRIPTION_OVERRIDES_CONFIG = {"config", "on"}


def resolve_tool_description_overrides(
    config: object | None,
) -> tuple[dict[str, str], str] | None:
    """Resolve the tool-description override table for the current turn.

    Returns ``(overrides, source)`` with ``source`` in ``{"config",
    "env_file"}``, or ``None`` when the mechanism is off or the selected table
    is empty. The config table alone never activates the mechanism; the
    ``OPENSQUILLA_TOOL_DESCRIPTION_OVERRIDES`` env gate must be set.
    """
    env_value = os.environ.get(_TOOL_DESCRIPTION_OVERRIDES_ENV, "").strip()
    if not env_value or env_value.lower() in _TOOL_DESCRIPTION_OVERRIDES_OFF:
        return None
    if env_value.lower() in _TOOL_DESCRIPTION_OVERRIDES_CONFIG:
        tools_cfg = getattr(config, "tools", None)
        table = getattr(tools_cfg, "description_overrides", None)
        overrides = _normalize_overrides(table, source_label="config")
        if not overrides:
            return None
        return overrides, "config"
    if env_value.lower().endswith((".toml", ".json")):
        overrides = _normalize_overrides(
            _load_override_file(env_value),
            source_label=env_value,
        )
        if not overrides:
            return None
        return overrides, "env_file"
    raise ValueError(
        f"{_TOOL_DESCRIPTION_OVERRIDES_ENV} must be one of: "
        + ", ".join(
            sorted(_TOOL_DESCRIPTION_OVERRIDES_CONFIG | _TOOL_DESCRIPTION_OVERRIDES_OFF)
        )
        + ", or a .toml/.json override file path"
    )


def _load_override_file(path_value: str) -> Any:
    path = Path(path_value)
    try:
        if path_value.lower().endswith(".toml"):
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"{_TOOL_DESCRIPTION_OVERRIDES_ENV} file {path_value!r} "
            f"could not be loaded: {exc}"
        ) from exc
    # Accept both a top-level override table and the gateway config shape
    # ([tools.description_overrides]) so an arm can point the env at its
    # config.toml copy directly.
    if isinstance(data, Mapping):
        tools_table = data.get("tools")
        if isinstance(tools_table, Mapping):
            nested = tools_table.get("description_overrides")
            if isinstance(nested, Mapping):
                return nested
    return data


def _normalize_overrides(table: Any, *, source_label: str) -> dict[str, str]:
    if table is None:
        return {}
    if not isinstance(table, Mapping):
        raise ValueError(
            f"{_TOOL_DESCRIPTION_OVERRIDES_ENV} overrides from {source_label} "
            "must be a table of strings"
        )
    overrides: dict[str, str] = {}
    for raw_key, raw_value in table.items():
        key = str(raw_key)
        if isinstance(raw_value, Mapping):
            # TOML parses an unquoted dotted key ("exec_command.command") as a
            # nested table; flatten one level back into dotted parameter keys.
            for param_key, param_value in raw_value.items():
                _add_override(overrides, f"{key}.{param_key}", param_value, source_label)
            continue
        _add_override(overrides, key, raw_value, source_label)
    return overrides


def _add_override(
    overrides: dict[str, str],
    key: str,
    value: Any,
    source_label: str,
) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{_TOOL_DESCRIPTION_OVERRIDES_ENV} override {key!r} from "
            f"{source_label} must be a non-empty string"
        )
    overrides[key] = value
