"""Active TUI output-handle binding for chat runtime scopes."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from opensquilla.cli.tui.backend.contracts import TuiOutputHandle

TuiOutputScope = MutableMapping[str, Any]

_TUI_OUTPUT_KEY = "tui_output"


class TuiOutputBinding:
    """Owns active output-handle storage for dict-backed chat runtimes."""

    def __init__(self, scope: TuiOutputScope) -> None:
        self._scope = scope

    def get(self) -> TuiOutputHandle | None:
        output_handle = self._scope.get(_TUI_OUTPUT_KEY)
        if isinstance(output_handle, TuiOutputHandle):
            return output_handle
        return None

    def expose(self, output_handle: TuiOutputHandle) -> None:
        self._scope[_TUI_OUTPUT_KEY] = output_handle

    def expose_from_surface(self, tui_surface: object) -> None:
        output_handle = getattr(tui_surface, "output_handle", None)
        if isinstance(output_handle, TuiOutputHandle):
            self.expose(output_handle)

    def clear(self) -> None:
        self._scope.pop(_TUI_OUTPUT_KEY, None)
