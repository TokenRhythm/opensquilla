"""Config command — get/set configuration values."""

from __future__ import annotations

import collections.abc
import os
import tomllib
import types
import typing
from pathlib import Path
from typing import Any, get_args, get_origin

import typer
from pydantic import BaseModel
from rich.markup import escape
from rich.table import Table

from opensquilla.cli.ui import ACCENT_HEADER, ACCENT_MARKUP, console

app = typer.Typer(help="Manage OpenSquilla configuration.")


@app.command("get")
def config_get(
    key: str = typer.Argument("", help="Config key to get (empty = show all)"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Get a configuration value."""
    from opensquilla.gateway.config import GatewayConfig

    cfg = GatewayConfig.load(config_path or os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"))
    data = cfg.to_public_dict()

    if key:
        # Support dot-notation: auth.mode
        val = _get_key(data, key)
        if val is _MISSING:
            console.print(f"[red]Key not found: {key}[/red]")
            raise typer.Exit(1)
        console.print(f"[{ACCENT_MARKUP}]{escape(key)}[/] = [green]{escape(repr(val))}[/green]")
    else:
        table = Table(title="Gateway Config", show_header=True, header_style=ACCENT_HEADER)
        table.add_column("Key")
        table.add_column("Value")
        _add_flat(table, data)
        console.print(table)


_MISSING = object()


def _get_key(data: dict[str, Any], key: str) -> Any:
    val: Any = data
    for part in key.split("."):
        if isinstance(val, dict) and part in val:
            val = val[part]
        else:
            return _MISSING
    return val


@app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key (dot-notation)"),
    value: str = typer.Argument(..., help="Value to set"),
    config_path: Path | None = typer.Option(None, "--config", help="Persist to config path."),
) -> None:
    """Set a configuration value (env-var backed, prints export command)."""
    from opensquilla.gateway.config import GatewayConfig

    if config_path is not None:
        from opensquilla.onboarding.config_store import load_config, persist_config

        cfg = load_config(config_path)
        data = cfg.to_toml_dict()
        if not _set_key(data, key, _parse_config_value(value)):
            console.print(f"[red]Key not found: {escape(key)}[/red]")
            raise typer.Exit(1)
        try:
            updated = GatewayConfig.model_validate(data)
            updated._mark_env_absorbed_secrets(data)
        except Exception as exc:  # noqa: BLE001 - show config validation errors as CLI input errors.
            console.print(f"[red]Invalid value for {escape(key)}:[/red] {escape(str(exc))}")
            raise typer.Exit(2) from exc
        from opensquilla.gateway.model_routing import (
            reconcile_model_routing_write,
        )

        reconcile_model_routing_write(updated, {key}, previous=cfg)
        persist = persist_config(updated, path=config_path, restart_required=True)
        console.print(f"[{ACCENT_MARKUP}]Config:[/] {persist.path}")
        if persist.backup_path:
            console.print(f"[dim]Backup:[/dim] {persist.backup_path}")
        console.print("[yellow]Restart the gateway to apply this setting.[/yellow]")
        return

    # Same key check as the persisting form. Without it the command answered
    # "export OPENSQUILLA_GATEWAY_DEFINITELY__INVALID=123" to a key that does
    # not exist, and the export is not merely useless: it reads as confirmation
    # that the setting was understood, so the operator sets it and then hunts
    # for why nothing changed. `gateway.port` is the case that shows up in
    # practice — the field is `port`, so the accepted spelling produced
    # OPENSQUILLA_GATEWAY_GATEWAY__PORT while the correct variable stayed unset.
    if not _set_key(_key_surface(), key, None):
        console.print(f"[red]Key not found: {escape(key)}[/red]")
        raise typer.Exit(1)

    env_key = "OPENSQUILLA_GATEWAY_" + key.upper().replace(".", "__")
    console.print("[dim]To persist this setting, export:[/dim]")
    console.print(f"  [bold]export {env_key}={value}[/bold]")


def _key_surface() -> dict[str, Any]:
    """The document keys are checked against when nothing is being written.

    The operator's own config is the accurate surface: it is what carries the
    mapping entries the schema cannot enumerate (their agent ids, their router
    tier names). A config too broken to load must not take this command down
    with it, though — reaching for `config set` is a normal way out of that
    state — so the schema defaults stand in, and every declared field is still
    checked.
    """

    from opensquilla.gateway.config import GatewayConfig

    try:
        cfg = GatewayConfig.load(os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"))
    except Exception:  # noqa: BLE001 - an unreadable config still gets key checking.
        return GatewayConfig.model_construct().to_toml_dict()
    return cfg.to_toml_dict()


def _parse_config_value(value: str) -> Any:
    try:
        return tomllib.loads(f"value = {value}\n")["value"]
    except tomllib.TOMLDecodeError:
        return value


def _unwrap_optional(annotation: Any) -> Any:
    """Strip a single ``None`` arm so ``X | None`` resolves like ``X``."""

    if get_origin(annotation) in (typing.Union, types.UnionType):
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _as_model(annotation: Any) -> type[BaseModel] | None:
    annotation = _unwrap_optional(annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _mapping_value_annotation(annotation: Any) -> Any | None:
    """The value type of a mapping field, or ``None`` if this is not a mapping.

    Both spellings that appear in the config model have to resolve: the
    parameterised ``dict[str, X]`` and the bare ``dict`` that
    ``squilla_router.tiers`` uses, whose ``get_origin`` is ``None``. An
    unparameterised or ``Any`` value type keeps the walk going with no schema
    left to check, which is exactly the case where presence in the document is
    the only evidence available.
    """

    annotation = _unwrap_optional(annotation)
    if annotation is Any:
        return Any
    origin = get_origin(annotation) or annotation
    try:
        is_mapping = isinstance(origin, type) and issubclass(origin, collections.abc.Mapping)
    except TypeError:  # a typing special form that is not a runtime class
        is_mapping = False
    if not is_mapping:
        return None
    args = get_args(annotation)
    return args[1] if len(args) == 2 else Any


def _set_key(data: dict[str, Any], key: str, value: Any) -> bool:
    """Write ``value`` at ``key``, reporting whether ``key`` is a real setting.

    Validity comes from the ``GatewayConfig`` schema rather than from the values
    that happen to be in the document. The two differ: TOML has no null, so
    every field sitting at its ``None`` default — `auth.token`, `compaction.model`,
    the agent timeout overrides, sixty-odd others — is absent from
    ``to_toml_dict()`` and used to be refused as "Key not found", which left no
    way to set them through this command at all.

    Segments that index a mapping field are the exception, because the schema
    cannot enumerate an operator's agent ids or router tier names. Those must
    already be present, so a typo in one is still caught and this never invents
    a half-formed entry.

    Intermediate tables are created only along a schema-valid path, and only as
    far as the walk gets: a rejected key can leave empty tables behind, so the
    caller must not persist a document this returned ``False`` for.
    """

    from opensquilla.gateway.config import GatewayConfig

    parts = key.split(".")
    if not all(parts):
        return False

    node: Any = GatewayConfig
    cursor: Any = data
    for part in parts[:-1]:
        model = _as_model(node)
        if model is not None:
            field = model.model_fields.get(part)
            if field is None:
                return False
            node = field.annotation
        else:
            value_annotation = _mapping_value_annotation(node)
            if value_annotation is None or part not in cursor:
                return False
            node = value_annotation

        child = cursor.get(part)
        if child is None:
            child = {}
            cursor[part] = child
        elif not isinstance(child, dict):
            return False
        cursor = child

    leaf = parts[-1]
    model = _as_model(node)
    if model is not None:
        if leaf not in model.model_fields:
            return False
    elif _mapping_value_annotation(node) is None or leaf not in cursor:
        return False

    cursor[leaf] = value
    return True


def _add_flat(table: Table, data: dict, prefix: str = "") -> None:
    for k, v in data.items():
        full_key = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
        if isinstance(v, dict):
            _add_flat(table, v, full_key)
        else:
            table.add_row(escape(full_key), escape(str(v)))
