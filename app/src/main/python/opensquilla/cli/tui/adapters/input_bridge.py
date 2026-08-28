"""Terminal input-asset bridge for chat command wiring."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opensquilla.cli.chat import input_assets as _input_assets
from opensquilla.cli.ui import console

CLI_ALLOWED_FILE_MIMES = _input_assets.CLI_ALLOWED_FILE_MIMES
CLI_INLINE_THRESHOLD_BYTES = _input_assets.CLI_INLINE_THRESHOLD_BYTES
PATH_REMOTE_GATEWAY_MESSAGE = _input_assets.PATH_REMOTE_GATEWAY_MESSAGE


def image_prompt_from_command(command: str) -> str:
    return _input_assets.image_prompt_from_command(command)


def image_prompt_and_attachments(
    command: str,
    *,
    output_console: Any | None = None,
) -> tuple[str, list[dict[str, str]]]:
    prompt, attachments = _input_assets.image_prompt_and_attachments(command)
    if attachments:
        active_console = console if output_console is None else output_console
        name = str(attachments[0].get("name") or "image")
        label = name.replace("\\", "/").rsplit("/", 1)[-1]
        active_console.print(f"[dim]Preparing image: {label}[/dim]")
    return prompt, attachments


def gateway_client_is_local(client: object) -> bool:
    return _input_assets.gateway_client_is_local(client)


def parse_path_command(command: str) -> tuple[Path, str]:
    return _input_assets.parse_path_command(command)


def path_strategy_hint(path: Path) -> str:
    return _input_assets.path_strategy_hint(path)


def path_prompt_and_attachments(command: str) -> tuple[str, list[dict[str, Any]]]:
    return _input_assets.path_prompt_and_attachments(command)


def file_prompt_and_attachments(
    command: str,
    *,
    upload_callable: Any | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    return _input_assets.file_prompt_and_attachments(
        command,
        upload_callable=upload_callable,
    )


async def async_file_prompt_and_attachments(
    command: str,
    *,
    upload_callable: Any | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    return await _input_assets.async_file_prompt_and_attachments(
        command,
        upload_callable=upload_callable,
    )
