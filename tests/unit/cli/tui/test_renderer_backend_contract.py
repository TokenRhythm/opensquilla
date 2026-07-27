from __future__ import annotations

import pytest

from opensquilla.cli.tui.renderers.selection import (
    DEFAULT_CHAT_UI_MODE,
    DEFAULT_TUI_BACKEND_ID,
    OPENSQUILLA_TUI_BACKEND_ENV,
    RendererBackendSelectionError,
    RendererBackendUnavailableError,
    RendererBackendUnavailableReason,
    get_renderer_backend,
    renderer_backends,
    select_chat_ui_backend,
    select_renderer_backend,
    select_renderer_backend_from_env,
)

REMOVED_TEXT_BACKEND = "text" + "ual"
REMOVED_BACKEND_IDS = ["terminal", REMOVED_TEXT_BACKEND, f"live-{REMOVED_TEXT_BACKEND}"]


def test_native_backend_is_internal_plain_renderer() -> None:
    backend = select_renderer_backend()

    assert backend.backend_id == DEFAULT_TUI_BACKEND_ID
    assert backend.supports_streaming_fast_path is True
    assert backend.supports_structured_ui is True
    assert set(renderer_backends()) == {"native", "opentui"}


def test_bare_chat_auto_selects_opentui_when_host_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui.opentui import bridge as opentui_bridge
    from opensquilla.cli.tui.renderers.selection import RendererBackendAvailability

    monkeypatch.setattr(
        opentui_bridge.OpenTuiRendererBackend,
        "is_available",
        lambda self: RendererBackendAvailability(available=True),
    )

    selection = select_chat_ui_backend(None)

    assert DEFAULT_CHAT_UI_MODE == "auto"
    assert selection.requested_mode == "auto"
    assert selection.backend.backend_id == "opentui"
    assert selection.fallback_reason is None


def test_bare_chat_auto_falls_back_to_plain_when_host_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui.opentui import bridge as opentui_bridge
    from opensquilla.cli.tui.renderers.selection import RendererBackendAvailability

    monkeypatch.setattr(
        opentui_bridge.OpenTuiRendererBackend,
        "is_available",
        lambda self: RendererBackendAvailability(
            available=False,
            reason="host missing",
            reason_code=RendererBackendUnavailableReason.MISSING,
        ),
    )

    selection = select_chat_ui_backend(None)

    assert selection.requested_mode == "auto"
    assert selection.backend.backend_id == "native"
    assert selection.fallback_reason == "host missing"
    assert selection.fallback is not None
    assert selection.fallback.code is RendererBackendUnavailableReason.MISSING


def test_renderer_backend_lookup_rejects_unknown_ids() -> None:
    with pytest.raises(RendererBackendSelectionError) as exc_info:
        get_renderer_backend("unknown")

    assert "Unsupported TUI backend" in str(exc_info.value)
    assert "opentui" in str(exc_info.value)
    assert "terminal" not in str(exc_info.value)
    assert REMOVED_TEXT_BACKEND not in str(exc_info.value)


def test_backend_selection_reads_env_and_preserves_native_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui.opentui import bridge as opentui_bridge
    from opensquilla.cli.tui.renderers.selection import RendererBackendAvailability

    monkeypatch.setattr(
        opentui_bridge.OpenTuiRendererBackend,
        "is_available",
        lambda self: RendererBackendAvailability(available=True),
    )

    assert select_renderer_backend_from_env({}).backend_id == "native"
    assert (
        select_renderer_backend_from_env({OPENSQUILLA_TUI_BACKEND_ENV: ""}).backend_id == "native"
    )
    assert (
        select_renderer_backend_from_env({OPENSQUILLA_TUI_BACKEND_ENV: " opentui "}).backend_id
        == "opentui"
    )


def test_backend_selection_rejects_unknown_env_values_clearly() -> None:
    with pytest.raises(RendererBackendSelectionError) as exc_info:
        select_renderer_backend_from_env({OPENSQUILLA_TUI_BACKEND_ENV: "bogus"})

    assert "Unsupported TUI backend" in str(exc_info.value)
    assert "opentui" in str(exc_info.value)
    assert "bogus" in str(exc_info.value)


@pytest.mark.parametrize("backend_id", REMOVED_BACKEND_IDS)
def test_backend_selection_rejects_removed_backend_ids(backend_id: str) -> None:
    with pytest.raises(RendererBackendSelectionError) as exc_info:
        select_renderer_backend(backend_id)

    message = str(exc_info.value)
    assert "Unsupported TUI backend" in message
    assert backend_id in message
    assert "opentui" in message


def test_opentui_backend_is_registered_without_importing_legacy_backends() -> None:
    backend = get_renderer_backend("opentui")

    assert backend.backend_id == "opentui"
    assert backend.supports_structured_ui is True
    assert backend.supports_streaming_fast_path is True


def test_public_chat_ui_plain_selects_native() -> None:
    selection = select_chat_ui_backend("plain")

    assert selection.requested_mode == "plain"
    assert selection.backend.backend_id == "native"
    assert selection.fallback_reason is None


def test_public_chat_ui_tui_selects_opentui_when_host_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui.opentui import bridge as opentui_bridge
    from opensquilla.cli.tui.renderers.selection import RendererBackendAvailability

    monkeypatch.setattr(
        opentui_bridge.OpenTuiRendererBackend,
        "is_available",
        lambda self: RendererBackendAvailability(available=True),
    )

    selection = select_chat_ui_backend("tui")

    assert selection.requested_mode == "tui"
    assert selection.backend.backend_id == "opentui"
    assert selection.fallback_reason is None


def test_public_chat_ui_auto_falls_back_before_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui.opentui import bridge as opentui_bridge
    from opensquilla.cli.tui.renderers.selection import RendererBackendAvailability

    monkeypatch.setattr(
        opentui_bridge.OpenTuiRendererBackend,
        "is_available",
        lambda self: RendererBackendAvailability(available=False, reason="host missing"),
    )

    selection = select_chat_ui_backend("auto")

    assert selection.backend.backend_id == "native"
    assert selection.fallback_reason == "host missing"


def test_public_chat_ui_tui_is_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    from opensquilla.cli.tui.opentui import bridge as opentui_bridge
    from opensquilla.cli.tui.renderers.selection import RendererBackendAvailability

    monkeypatch.setattr(
        opentui_bridge.OpenTuiRendererBackend,
        "is_available",
        lambda self: RendererBackendAvailability(available=False, reason="host missing"),
    )

    with pytest.raises(RendererBackendUnavailableError, match="host missing"):
        select_chat_ui_backend("tui")


def test_public_chat_ui_ignores_internal_backend_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OPENSQUILLA_TUI_BACKEND_ENV, "bogus")

    selection = select_chat_ui_backend("plain")

    assert selection.backend.backend_id == "native"


def test_public_chat_ui_rejects_unknown_mode() -> None:
    with pytest.raises(RendererBackendSelectionError, match="auto, plain, tui"):
        select_chat_ui_backend("visual")
