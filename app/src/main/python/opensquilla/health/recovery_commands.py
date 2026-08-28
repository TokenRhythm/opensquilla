from __future__ import annotations

import shlex
from pathlib import Path

CONFIG_AWARE_COMMAND_PREFIXES = (
    "opensquilla gateway restart",
    "opensquilla gateway start",
    "opensquilla gateway status",
    "opensquilla providers configure",
    "opensquilla providers status",
    "opensquilla config ",
    "opensquilla search status",
    "opensquilla search configure",
    "opensquilla diagnostics status",
    "opensquilla memory status",
    "opensquilla memory repair list",
    "opensquilla memory repair run",
    "opensquilla configure ",
    "opensquilla onboard",
    "opensquilla sandbox ",
    "opensquilla channels add",
    "opensquilla channels edit",
    "opensquilla channels enable",
    "opensquilla channels disable",
    "opensquilla channels remove",
    "opensquilla channels list",
    "opensquilla channels restart",
    "opensquilla channels status",
)


def supports_config_option(command: str) -> bool:
    return any(command.startswith(prefix) for prefix in CONFIG_AWARE_COMMAND_PREFIXES)


def command_with_config(command: str, config_path: str | Path | None) -> str:
    if not config_path or " --config " in command or not supports_config_option(command):
        return command
    return f"{command} --config {shlex.quote(str(config_path))}"
