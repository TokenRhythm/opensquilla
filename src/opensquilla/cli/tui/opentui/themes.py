"""Theme catalog and the ``/theme`` command for the OpenTUI footer host.

The themes themselves live in the JS host (``package/src/theme.mjs``). This module
only mirrors the theme NAMES so the CLI can list/validate them and drive live
switching by sending a ``theme.set`` IPC message through the OpenTUI output handle.
The name list is kept in sync with ``theme.mjs`` by a conformance test.
"""

from __future__ import annotations

# Must equal the keys of PALETTES in
# src/opensquilla/cli/tui/opentui/package/src/theme.mjs (enforced by
# tests/unit/cli/tui/test_opentui_themes.py::test_theme_names_match_js_registry).
THEME_NAMES: tuple[str, ...] = (
    "opensquilla-dark",
    "opensquilla-light",
    "midnight",
    "ember",
    "slate",
    "high-contrast",
    "nord",
    "mono",
    "monochrome",
)
DEFAULT_THEME = "opensquilla-dark"
# Read by the JS host as ``process.env.OPENSQUILLA_TUI_THEME`` (main.mjs); the
# literal on both sides is pinned by
# tests/unit/cli/tui/test_opentui_themes.py::test_theme_env_var_matches_js_host.
THEME_ENV_VAR = "OPENSQUILLA_TUI_THEME"
# Read by the JS host in ``detectColorMode`` (theme.mjs) to force a color mode
# ("truecolor" | "16" | "mono"); overrides NO_COLOR per the NO_COLOR spec. The
# literal on both sides is pinned by
# tests/unit/cli/tui/test_opentui_themes.py::test_color_env_var_matches_js_host.
COLOR_ENV_VAR = "OPENSQUILLA_TUI_COLOR"


async def handle_theme_command(cmd: str, tui_output: object | None) -> None:
    """Handle ``/theme`` and ``/theme <name>`` (OpenTUI only).

    ``/theme <name>`` switches directly; bare ``/theme`` (or an unknown name)
    opens the interactive picker in the host (arrow-key live preview). Both are
    driven over the host output handle, so the host renders a panel rather than
    dumping a list into the scrollback. On the native backend (no
    ``send_message``) it explains that themes apply to the OpenTUI backend.
    """
    send_message = getattr(tui_output, "send_message", None)
    # The plugin wrapper always exposes a callable send_message that silently
    # no-ops on the native backend, so callable() alone can't distinguish an
    # IPC-capable OpenTUI surface from a native terminal. Prefer the wrapper's
    # explicit capability flag; fall back to callable() for an unwrapped handle.
    supports = getattr(tui_output, "supports_send_message", None)
    if supports is None:
        supports = callable(send_message)
    if not supports or not callable(send_message):
        from opensquilla.cli.ui import console  # noqa: PLC0415 - keep module import-light

        console.print(
            "[yellow]Themes apply to the OpenTUI backend (start chat with --ui tui).[/yellow]"
        )
        return

    parts = cmd.split()
    if len(parts) >= 2:
        name = parts[1].strip().lower()
        if name in THEME_NAMES:
            import asyncio  # noqa: PLC0415

            from opensquilla.cli.tui.opentui.prefs import (  # noqa: PLC0415
                save_theme_preference,
            )

            await send_message("theme.set", {"name": name})
            # Off the loop: the save takes a file lock and does disk IO, and
            # this handler runs on the chat event loop.
            await asyncio.to_thread(save_theme_preference, name)
            return

    # No name, or an unknown one: open the interactive picker in the host.
    # The picker's confirmed choice comes back as a ``theme.selected`` frame
    # and is persisted by the surface loop.
    await send_message("theme.pick", {})
