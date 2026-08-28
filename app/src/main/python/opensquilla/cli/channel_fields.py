"""Shared CLI helpers for channel field payloads."""

from __future__ import annotations

from typing import Any

import typer

from opensquilla.cli.ui import ACCENT_MARKUP, console
from opensquilla.onboarding.channel_specs import get_channel_setup_spec

TOKEN_ALIASES = (
    "token",
    "access_token",
    "client_secret",
    "app_secret",
    "app_password",
    "corp_secret",
)

_BOOL_TRUE_SPELLINGS = frozenset({"1", "true", "yes", "on"})
_BOOL_FALSE_SPELLINGS = frozenset({"0", "false", "no", "off"})
_BOOL_ACCEPTED_HINT = "1/0, true/false, yes/no, on/off"


def _field_label(field_name: str) -> str:
    return f"--field {field_name}" if field_name else "--field value"


def coerce_channel_field_value(
    field_type: str,
    raw: str,
    *,
    field_name: str = "",
) -> Any:
    """Coerce one ``--field key=value`` string to the spec's field type.

    Coercion failures raise ``typer.BadParameter`` naming the offending
    ``--field`` so the operator sees which key was rejected. Bool parsing is
    strict: a typo such as ``enabled=ture`` must not silently become False.
    """
    if field_type == "int":
        try:
            return int(raw)
        except ValueError:
            raise typer.BadParameter(
                f"{_field_label(field_name)} expects an integer, got {raw!r}"
            ) from None
    if field_type == "float":
        try:
            return float(raw)
        except ValueError:
            raise typer.BadParameter(
                f"{_field_label(field_name)} expects a number, got {raw!r}"
            ) from None
    if field_type == "bool":
        normalized = raw.strip().lower()
        if normalized in _BOOL_TRUE_SPELLINGS:
            return True
        if normalized in _BOOL_FALSE_SPELLINGS:
            return False
        raise typer.BadParameter(
            f"{_field_label(field_name)} expects a boolean "
            f"({_BOOL_ACCEPTED_HINT}), got {raw!r}"
        )
    return raw


def parse_channel_field_pairs(pairs: list[str], type_name: str) -> dict[str, Any]:
    spec = get_channel_setup_spec(type_name)
    by_name = {f.name: f for f in spec.fields}
    out: dict[str, Any] = {}
    for raw_pair in pairs:
        if "=" not in raw_pair:
            raise typer.BadParameter(f"--field expects key=value, got {raw_pair!r}")
        key, value = raw_pair.split("=", 1)
        if key not in by_name:
            raise typer.BadParameter(
                f"unknown field {key!r} for channel type {type_name!r}"
            )
        out[key] = coerce_channel_field_value(
            by_name[key].field_type, value, field_name=key
        )
    return out


def resolve_channel_token_field(type_name: str) -> str:
    """Pick the secret field that --token maps to, in alias-tuple order."""
    spec = get_channel_setup_spec(type_name)
    secret_names = {f.name for f in spec.fields if f.secret}
    for alias in TOKEN_ALIASES:
        if alias in secret_names:
            return alias
    typer.secho(
        f"--token is not supported for channel type {type_name!r}; "
        f"use --field <name>=... instead.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(code=2)


def apply_channel_token(payload: dict[str, Any], type_name: str, token: str) -> None:
    if not token:
        return
    field_name = resolve_channel_token_field(type_name)
    console.print(f"[{ACCENT_MARKUP}]--token resolved to[/] {type_name}.{field_name}")
    payload[field_name] = token
