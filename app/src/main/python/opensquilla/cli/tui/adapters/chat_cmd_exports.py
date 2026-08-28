"""Lazy private export resolver for ``opensquilla.cli.chat_cmd``.

The chat command module is intentionally only the Typer entrypoint. This
resolver keeps old private imports available without loading terminal runtime
modules when the Typer command module is imported.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any

from opensquilla.cli.chat.launch import (
    ChatCommandLaunchOverrides as _ChatCommandLaunchOverrides,
)
from opensquilla.cli.chat.launch import ChatCommandRequest as _ChatCommandRequest

_CHAT_COMPAT_MODULE = "opensquilla.cli.tui.adapters.chat_compat"
_RUNTIME_BRIDGE_MODULE = "opensquilla.cli.tui.adapters.runtime_bridge"
_LAUNCH_BRIDGE_MODULE = "opensquilla.cli.tui.adapters.launch_bridge"

CHAT_COMPAT_EXPORTS = {
    "_CLI_ALLOWED_FILE_MIMES": "CLI_ALLOWED_FILE_MIMES",
    "_CLI_INLINE_THRESHOLD_BYTES": "CLI_INLINE_THRESHOLD_BYTES",
    "_PATH_REMOTE_GATEWAY_MESSAGE": "PATH_REMOTE_GATEWAY_MESSAGE",
    "_CLI_ATTACHMENT_COMPAT_EXPORTS": "CLI_ATTACHMENT_COMPAT_EXPORTS",
    "GATEWAY_SLASH_HANDLER_WORDS": "GATEWAY_SLASH_HANDLER_WORDS",
    "STANDALONE_SLASH_HANDLER_WORDS": "STANDALONE_SLASH_HANDLER_WORDS",
    "TurnResult": "TurnResult",
    "UsageSummary": "UsageSummary",
    "_ORIGINAL_TURN_STREAM_WRAP": "ORIGINAL_TURN_STREAM_WRAP",
    "_DEFAULT_STREAM_HEARTBEAT_INTERVAL_SECONDS": (
        "DEFAULT_STREAM_HEARTBEAT_INTERVAL_SECONDS"
    ),
    "_DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS": "DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS",
    "_tool_result_success_from_status": "tool_result_success_from_status",
    "_turn_stream_error_message": "turn_stream_error_message",
    "_timeout_exception_message": "timeout_exception_message",
    "_optional_positive_config_float": "optional_positive_config_float",
    "_wrap_cli_turn_stream": "wrap_cli_turn_stream",
    "_resolve_compaction_provider": "resolve_compaction_provider",
    "_is_approval_or_blocked_result": "is_approval_or_blocked_result",
    "_approval_surface_for_tui_output": "approval_surface_for_tui_output",
    "_flush_before_standalone_rewrite": "flush_before_standalone_rewrite",
    "_handle_gateway_slash_command": "handle_gateway_slash_command",
    "_sync_gateway_slash_adapter_io": "sync_gateway_slash_adapter_io",
    "_sync_standalone_slash_adapter_io": "sync_standalone_slash_adapter_io",
    "_turn_stream_dependencies": "default_turn_stream_dependencies",
    "_print_sessions_table": "print_sessions_table",
    "_print_models_table": "print_models_table",
    "_save_transcript_command": "save_transcript_command",
    "_save_gateway_transcript_command": "save_gateway_transcript_command",
    "_image_prompt_from_command": "image_prompt_from_command",
    "_image_prompt_and_attachments": "image_prompt_and_attachments",
    "_gateway_client_is_local": "gateway_client_is_local",
    "_parse_path_command": "parse_path_command",
    "_path_strategy_hint": "path_strategy_hint",
    "_path_prompt_and_attachments": "path_prompt_and_attachments",
    "_file_prompt_and_attachments": "file_prompt_and_attachments",
    "_async_file_prompt_and_attachments": "async_file_prompt_and_attachments",
    "_forget_server_approvals": "forget_server_approvals",
    "_handle_approvals_command": "handle_approvals_command",
    "_handle_forget_command": "handle_forget_command",
    "_handle_elevated_command": "handle_elevated_command",
    "_render_gateway_task_group_status": "render_gateway_task_group_status",
    "_gateway_task_group_status": "gateway_task_group_status",
    "_arender_gateway_task_group_status": "arender_gateway_task_group_status",
    "_renderer_status": "renderer_status",
    "_renderer_tool_start": "renderer_tool_start",
    "_renderer_tool_finished": "renderer_tool_finished",
    "_renderer_error": "renderer_error",
    "_renderer_finalize": "renderer_finalize",
    "_renderer_close": "renderer_close",
    "_artifact_event_payload": "artifact_event_payload",
    "_artifact_status_line": "artifact_status_line",
    "_stream_response_gateway": "stream_response_gateway",
    "_local_approval_resolver": "local_approval_resolver",
    "_stream_response_turnrunner": "stream_response_turnrunner",
    "_handle_image_command_turnrunner": "handle_image_command_turnrunner",
}

RUNTIME_EXPORTS = {
    "_cli_sender_id": "cli_sender_id",
    "_run_concurrent_repl": "run_concurrent_repl",
    "_read_standalone_transcript": "read_standalone_transcript",
    "_standalone_slash_services_from_runtime": "standalone_slash_services_from_runtime",
    "_standalone_repl": "standalone_chat_runner",
    "_gateway_chat": "gateway_chat_runner",
}

LAUNCH_EXPORTS = {
    "_launch_chat_command": "launch_chat_command",
    "_quiet_logs_for_interactive_chat": "quiet_logs_for_interactive_chat",
    "_clear_screen_for_interactive_chat": "clear_screen_for_interactive_chat",
}

MODULE_EXPORTS = {
    "_chat_compat": _CHAT_COMPAT_MODULE,
    "_runtime_bridge": _RUNTIME_BRIDGE_MODULE,
    "_launch_bridge": _LAUNCH_BRIDGE_MODULE,
}

MODULE_COMPAT_EXPORTS = {
    "chat_compat": _CHAT_COMPAT_MODULE,
    "runtime_bridge": _RUNTIME_BRIDGE_MODULE,
    "launch_bridge": _LAUNCH_BRIDGE_MODULE,
}

LEGACY_CHAT_CMD_EXPORT_NAMES = frozenset(
    CHAT_COMPAT_EXPORTS | RUNTIME_EXPORTS | LAUNCH_EXPORTS | MODULE_EXPORTS
)

ChatCommandRequest = _ChatCommandRequest
ChatCommandLaunchOverrides = _ChatCommandLaunchOverrides


def _load_module(module_name: str) -> Any:
    return importlib.import_module(module_name)


def resolve_legacy_chat_cmd_launch_overrides(
    values: Mapping[str, Any] | None,
) -> ChatCommandLaunchOverrides:
    if values is None:
        return ChatCommandLaunchOverrides()
    launch_bridge = values.get("_launch_bridge")
    return ChatCommandLaunchOverrides(
        launch_chat=None
        if launch_bridge is None
        else getattr(launch_bridge, "launch_chat"),
        standalone_runner=values.get("_standalone_repl"),
        gateway_runner=values.get("_gateway_chat"),
    )


def resolve_legacy_chat_cmd_export(name: str) -> Any:
    if name in MODULE_EXPORTS:
        return _load_module(MODULE_EXPORTS[name])
    if target := CHAT_COMPAT_EXPORTS.get(name):
        return getattr(_load_module(_CHAT_COMPAT_MODULE), target)
    if target := RUNTIME_EXPORTS.get(name):
        return getattr(_load_module(_RUNTIME_BRIDGE_MODULE), target)
    if target := LAUNCH_EXPORTS.get(name):
        return getattr(_load_module(_LAUNCH_BRIDGE_MODULE), target)
    raise AttributeError(f"module 'opensquilla.cli.chat_cmd' has no attribute {name!r}")


def __getattr__(name: str) -> Any:
    if name in MODULE_COMPAT_EXPORTS:
        return _load_module(MODULE_COMPAT_EXPORTS[name])
    if name in LEGACY_CHAT_CMD_EXPORT_NAMES:
        return resolve_legacy_chat_cmd_export(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *MODULE_COMPAT_EXPORTS, *LEGACY_CHAT_CMD_EXPORT_NAMES})
