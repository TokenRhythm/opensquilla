from __future__ import annotations

import re
from pathlib import Path

import pytest

from opensquilla.cli.tui.opentui.themes import (
    COLOR_ENV_VAR,
    DEFAULT_THEME,
    THEME_ENV_VAR,
    THEME_NAMES,
    handle_theme_command,
)
from opensquilla.cli.ui import console

_THEME_MJS = (
    Path(__file__).resolve().parents[4] / "src/opensquilla/cli/tui/opentui/package/src/theme.mjs"
)
_MAIN_MJS = (
    Path(__file__).resolve().parents[4] / "src/opensquilla/cli/tui/opentui/package/src/main.mjs"
)


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path_factory: pytest.TempPathFactory, monkeypatch) -> None:
    # handle_theme_command persists valid /theme choices; isolate the state
    # root so these tests never write into the shared pytest-session state.
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path_factory.mktemp("state")))


def _js_palette_names() -> list[str]:
    text = _THEME_MJS.read_text(encoding="utf-8")
    block = text.split("PALETTES = Object.freeze({", 1)[1].split("});", 1)[0]
    # Theme entries are at 2-space indent with an object value (`: {`); the inner
    # tokens are deeper-indented string values, so they do not match.
    return re.findall(r'^ {2}"?([a-z][a-z0-9-]*)"?:\s*\{', block, re.M)


def test_theme_names_match_js_registry() -> None:
    js_names = _js_palette_names()
    assert js_names, "could not parse PALETTES from theme.mjs"
    assert list(THEME_NAMES) == js_names
    assert DEFAULT_THEME in THEME_NAMES


def test_theme_env_var_matches_js_host() -> None:
    # The JS host reads the variable as a literal; the Python constant only
    # stays truthful if both sides name the same variable, so pin the literal
    # here the same way the palette names are pinned above.
    text = _MAIN_MJS.read_text(encoding="utf-8")
    assert f"process.env.{THEME_ENV_VAR}" in text


def test_color_env_var_matches_js_host() -> None:
    # Same pinning for the color-mode override: detectColorMode reads the
    # variable off the env object it is handed (process.env at import), so the
    # literal must appear in theme.mjs for the Python constant to stay truthful.
    text = _THEME_MJS.read_text(encoding="utf-8")
    assert f"env.{COLOR_ENV_VAR}" in text


@pytest.mark.asyncio
async def test_theme_command_opens_picker_with_no_argument() -> None:
    class _FakeOutput:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict[str, object]]] = []

        async def send_message(self, message_type: str, payload: dict[str, object]) -> None:
            self.sent.append((message_type, payload))

    output = _FakeOutput()
    await handle_theme_command("/theme", output)
    assert output.sent == [("theme.pick", {})]


@pytest.mark.asyncio
async def test_theme_command_switches_via_send_message() -> None:
    class _FakeOutput:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict[str, object]]] = []

        async def send_message(self, message_type: str, payload: dict[str, object]) -> None:
            self.sent.append((message_type, payload))

    output = _FakeOutput()
    with console.capture():
        await handle_theme_command("/theme midnight", output)
    assert output.sent == [("theme.set", {"name": "midnight"})]


@pytest.mark.asyncio
async def test_theme_command_unknown_name_opens_picker() -> None:
    class _FakeOutput:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict[str, object]]] = []

        async def send_message(self, message_type: str, payload: dict[str, object]) -> None:
            self.sent.append((message_type, payload))

    output = _FakeOutput()
    await handle_theme_command("/theme nope", output)
    assert output.sent == [("theme.pick", {})]


@pytest.mark.asyncio
async def test_theme_command_explains_opentui_only_on_native() -> None:
    # A native output handle has no send_message; the command must not crash and
    # should explain that themes are OpenTUI-only.
    class _NativeOutput:
        async def write_through(self, payload: str) -> None:
            return None

    with console.capture() as capture:
        await handle_theme_command("/theme ember", _NativeOutput())
    assert "OpenTUI backend" in capture.get()


@pytest.mark.asyncio
async def test_theme_command_explains_when_wrapped_native_handle_cannot_send() -> None:
    # The plugin wrapper ALWAYS exposes a callable send_message that silently
    # no-ops on the native backend. callable() alone would misclassify /theme as
    # sendable and do nothing; the command must consult supports_send_message and
    # fall back to the OpenTUI-only explanation instead.
    from opensquilla.cli.tui.adapters.runtime_helpers import TuiPluginOutputHandle

    class _NativeOutput:
        approval_surface = object()

        async def write_through(self, payload: str) -> None:
            return None

    wrapper = TuiPluginOutputHandle(_NativeOutput(), plugin_manager=object())
    assert wrapper.supports_send_message is False
    with console.capture() as capture:
        await handle_theme_command("/theme ember", wrapper)
    assert "OpenTUI backend" in capture.get()


@pytest.mark.asyncio
async def test_theme_command_sends_through_wrapped_ipc_capable_handle() -> None:
    from opensquilla.cli.tui.adapters.runtime_helpers import TuiPluginOutputHandle

    class _OpenTuiOutput:
        approval_surface = object()

        def __init__(self) -> None:
            self.sent: list[tuple[str, dict[str, object]]] = []

        async def write_through(self, payload: str) -> None:
            return None

        async def send_message(self, message_type: str, payload: dict[str, object]) -> None:
            self.sent.append((message_type, payload))

    inner = _OpenTuiOutput()
    wrapper = TuiPluginOutputHandle(inner, plugin_manager=object())
    assert wrapper.supports_send_message is True
    await handle_theme_command("/theme", wrapper)
    assert inner.sent == [("theme.pick", {})]
