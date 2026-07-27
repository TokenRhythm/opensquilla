"""Pin the ``/keys`` cheatsheet to the OpenTUI host's keyboard-handler source.

The cheatsheet in ``opensquilla.cli.tui.adapters.commands`` is a hand-written
mirror of the imperative key handlers in ``package/src/composer.mjs`` and
``package/src/main.mjs``. Each documented row carries the exact source
literal it describes; these tests fail when a binding is removed or renamed
without updating the table (the same pattern that pins ``THEME_NAMES`` to
``theme.mjs``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.cli.tui.adapters.commands import (
    NATIVE_KEY_BINDINGS,
    OPENTUI_KEY_BINDINGS,
    render_keys_table,
)

_PACKAGE_SRC = (
    Path(__file__).resolve().parents[4] / "src/opensquilla/cli/tui/opentui/package/src"
)


@pytest.mark.parametrize(
    "binding",
    [binding for binding in OPENTUI_KEY_BINDINGS if binding.js_literals],
    ids=lambda binding: binding.keys,
)
def test_documented_chords_exist_in_host_source(binding) -> None:
    source = (_PACKAGE_SRC / binding.js_file).read_text(encoding="utf-8")
    for literal in binding.js_literals:
        assert literal in source, (
            f"/keys documents {binding.keys!r} via {literal!r}, which no longer "
            f"appears in {binding.js_file}; update OPENTUI_KEY_BINDINGS to match "
            "the host's handlers"
        )


def test_keys_table_lists_every_binding_for_each_backend() -> None:
    opentui_table = render_keys_table(opentui=True)
    assert opentui_table.row_count == len(OPENTUI_KEY_BINDINGS)
    native_table = render_keys_table(opentui=False)
    assert native_table.row_count == len(NATIVE_KEY_BINDINGS)
    # The plain backend must never advertise host-only chords.
    assert native_table.caption is not None


def test_native_rows_match_the_launch_banner_vocabulary() -> None:
    documented = " ".join(f"{binding.keys} {binding.action}" for binding in NATIVE_KEY_BINDINGS)
    # The three affordances the native banner promises, and nothing OpenTUI-only.
    assert "Enter" in documented
    assert "Ctrl+C" in documented
    assert "Ctrl+D" in documented
    assert "Ctrl+O" not in documented
    assert "PageUp" not in documented


def test_output_supports_host_ui_prefers_the_wrapper_capability_flag() -> None:
    from opensquilla.cli.tui.adapters.slash_common import output_supports_host_ui

    class _Wrapped:
        # The plugin wrapper always exposes a callable send_message that
        # no-ops on the native backend; only the flag tells the truth.
        supports_send_message = False

        async def send_message(self, *_args: object) -> None: ...

    class _Host:
        supports_send_message = True

        async def send_message(self, *_args: object) -> None: ...

    class _Bare:
        async def send_message(self, *_args: object) -> None: ...

    assert output_supports_host_ui(None) is False
    assert output_supports_host_ui(_Wrapped()) is False
    assert output_supports_host_ui(_Host()) is True
    assert output_supports_host_ui(_Bare()) is True  # unwrapped handle fallback
