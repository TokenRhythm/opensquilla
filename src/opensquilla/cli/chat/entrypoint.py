"""Typed default launcher resolver for ``opensquilla chat``."""

from __future__ import annotations

import importlib
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, cast, overload

from opensquilla.cli.chat.frontend import ChatCommandLauncher
from opensquilla.cli.chat.launch import ChatCommandLaunchOverrides, ChatCommandRequest


class LazyChatCommandAll(Sequence[str]):
    """Lazy ``chat_cmd.__all__`` that preserves legacy star-import names."""

    def _names(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    "run_chat",
                    *(name for name in legacy_chat_export_names() if not name.startswith("_")),
                }
            )
        )

    def __iter__(self) -> Iterator[str]:
        return iter(self._names())

    def __contains__(self, name: object) -> bool:
        return name in self._names()

    def __len__(self) -> int:
        return len(self._names())

    @overload
    def __getitem__(self, index: int) -> str: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[str, ...]: ...

    def __getitem__(self, index: int | slice) -> str | tuple[str, ...]:
        return self._names()[index]


def _chat_cmd_exports() -> Any:
    return importlib.import_module("opensquilla.cli.tui.adapters.chat_cmd_exports")


def default_chat_launcher() -> ChatCommandLauncher:
    module = importlib.import_module("opensquilla.cli.tui.adapters.launch_bridge")
    return cast(ChatCommandLauncher, module.launch_chat_command)


def legacy_chat_export_names() -> frozenset[str]:
    return cast(frozenset[str], _chat_cmd_exports().LEGACY_CHAT_CMD_EXPORT_NAMES)


def legacy_chat_export(name: str) -> Any:
    return _chat_cmd_exports().resolve_legacy_chat_cmd_export(name)


def legacy_launch_overrides(
    values: Mapping[str, Any] | None,
) -> ChatCommandLaunchOverrides:
    return cast(
        ChatCommandLaunchOverrides,
        _chat_cmd_exports().resolve_legacy_chat_cmd_launch_overrides(values),
    )


def run_chat_request(
    request: ChatCommandRequest,
    *,
    module_globals: Mapping[str, Any] | None = None,
) -> None:
    values = {} if module_globals is None else module_globals
    launcher = values.get("_launch_chat_command") or default_chat_launcher()
    launcher(request, overrides=legacy_launch_overrides(values))
