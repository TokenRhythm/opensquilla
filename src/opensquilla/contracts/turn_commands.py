"""Explicit ordinary-turn command intent shared by transport adapters."""

from __future__ import annotations

from typing import Literal, TypedDict


class TurnCommand(TypedDict):
    name: Literal["project.init"]
    args: str


def normalize_turn_command(value: object) -> TurnCommand | None:
    """Validate intent without inferring a command from user text or history."""
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"name", "args"}
        or value.get("name") != "project.init"
        or not isinstance(value.get("args"), str)
    ):
        raise ValueError("turnCommand must contain name='project.init' and string args")
    return {"name": "project.init", "args": value["args"]}
