"""Input asset helpers for chat frontends."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opensquilla.cli import attachments as _cli_attachments

CLI_ALLOWED_FILE_MIMES = _cli_attachments.CLI_ALLOWED_FILE_MIMES
CLI_INLINE_THRESHOLD_BYTES = _cli_attachments.CLI_INLINE_THRESHOLD_BYTES
PATH_REMOTE_GATEWAY_MESSAGE = _cli_attachments.PATH_REMOTE_GATEWAY_MESSAGE


def image_prompt_from_command(command: str) -> str:
    return _cli_attachments.image_prompt_from_command(command)


def image_prompt_and_attachments(command: str) -> tuple[str, list[dict[str, str]]]:
    return _cli_attachments.image_prompt_and_attachments(command)


def gateway_client_is_local(client: object) -> bool:
    local_attr = getattr(client, "is_local_gateway", None)
    if callable(local_attr):
        try:
            return bool(local_attr())
        except TypeError:
            return False
    if local_attr is not None:
        return bool(local_attr)

    try:
        from opensquilla.cli.gateway_client import gateway_base_is_local
    except Exception:  # pragma: no cover - defensive import fallback
        return False
    return gateway_base_is_local(getattr(client, "_http_base", None))


def parse_path_command(command: str) -> tuple[Path, str]:
    return _cli_attachments.parse_path_command(command)


def path_strategy_hint(path: Path) -> str:
    return _cli_attachments.path_strategy_hint(path)


def path_prompt_and_attachments(command: str) -> tuple[str, list[dict[str, Any]]]:
    return _cli_attachments.path_prompt_and_attachments(command)


def file_prompt_and_attachments(
    command: str,
    *,
    upload_callable: Any | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    return _cli_attachments.file_prompt_and_attachments(
        command,
        upload_callable=upload_callable,
    )


async def async_file_prompt_and_attachments(
    command: str,
    *,
    upload_callable: Any | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    return await _cli_attachments.async_file_prompt_and_attachments(
        command,
        upload_callable=upload_callable,
    )
