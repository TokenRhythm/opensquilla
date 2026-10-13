"""Portable environment-variable lookup shared by configuration and providers."""

from __future__ import annotations

import os
from collections.abc import Mapping


def environment_value(
    name: str,
    *,
    environment: Mapping[str, str] | None = None,
    case_insensitive: bool | None = None,
) -> str:
    """Return a value with the platform's environment name semantics."""

    env_name = str(name or "")
    if not env_name:
        return ""
    source = os.environ if environment is None else environment
    direct = source.get(env_name)
    if direct is not None:
        return str(direct)
    insensitive = os.name == "nt" if case_insensitive is None else case_insensitive
    if not insensitive:
        return ""
    folded_name = env_name.casefold()
    for key, value in source.items():
        if str(key).casefold() == folded_name:
            return str(value)
    return ""


__all__ = ["environment_value"]
