from __future__ import annotations

from typing import Any

import pytest


def test_input_bridge_announces_image_attachment_with_supplied_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import input_bridge

    captured: dict[str, str] = {}
    prints: list[str] = []

    class FakeConsole:
        def print(self, message: str) -> None:
            prints.append(message)

    def fake_image_prompt_and_attachments(
        command: str,
    ) -> tuple[str, list[dict[str, str]]]:
        captured["command"] = command
        return (
            "describe it",
            [{"type": "image/png", "data": "x" * 3072, "name": "screen.png"}],
        )

    monkeypatch.setattr(
        input_bridge._input_assets,
        "image_prompt_and_attachments",
        fake_image_prompt_and_attachments,
    )

    prompt, attachments = input_bridge.image_prompt_and_attachments(
        "/image screen.png describe it",
        output_console=FakeConsole(),
    )

    assert captured["command"] == "/image screen.png describe it"
    assert prompt == "describe it"
    assert attachments == [{"type": "image/png", "data": "x" * 3072, "name": "screen.png"}]
    assert prints == ["[dim]Preparing image: screen.png[/dim]"]


def test_input_bridge_announces_image_attachment_with_default_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import input_bridge

    prints: list[str] = []

    class FakeConsole:
        def print(self, message: str) -> None:
            prints.append(message)

    def fake_image_prompt_and_attachments(
        command: str,
    ) -> tuple[str, list[dict[str, str]]]:
        return (
            "describe it",
            [{"type": "image/png", "data": "x" * 2048, "name": "screen.png"}],
        )

    monkeypatch.setattr(
        input_bridge._input_assets,
        "image_prompt_and_attachments",
        fake_image_prompt_and_attachments,
    )
    monkeypatch.setattr(input_bridge, "console", FakeConsole())

    prompt, attachments = input_bridge.image_prompt_and_attachments("/image screen.png describe it")

    assert prompt == "describe it"
    assert attachments == [{"type": "image/png", "data": "x" * 2048, "name": "screen.png"}]
    assert prints == ["[dim]Preparing image: screen.png[/dim]"]


def test_input_bridge_does_not_announce_when_image_has_no_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import input_bridge

    prints: list[str] = []

    class FakeConsole:
        def print(self, message: str) -> None:
            prints.append(message)

    def fake_image_prompt_and_attachments(
        command: str,
    ) -> tuple[str, list[dict[str, str]]]:
        return "describe it", []

    monkeypatch.setattr(
        input_bridge._input_assets,
        "image_prompt_and_attachments",
        fake_image_prompt_and_attachments,
    )

    prompt, attachments = input_bridge.image_prompt_and_attachments(
        "/image missing.png describe it",
        output_console=FakeConsole(),
    )

    assert prompt == "describe it"
    assert attachments == []
    assert prints == []


@pytest.mark.asyncio
async def test_input_bridge_wraps_async_file_prompt_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import input_bridge

    captured: dict[str, Any] = {}

    async def fake_async_file_prompt_and_attachments(
        command: str,
        *,
        upload_callable: Any | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        captured["command"] = command
        captured["upload_callable"] = upload_callable
        return "summarize", [{"type": "text/plain", "name": "note.txt"}]

    monkeypatch.setattr(
        input_bridge._input_assets,
        "async_file_prompt_and_attachments",
        fake_async_file_prompt_and_attachments,
    )

    def fake_upload(*args: Any) -> str:
        return "u-file"

    prompt, attachments = await input_bridge.async_file_prompt_and_attachments(
        "/file note.txt summarize",
        upload_callable=fake_upload,
    )

    assert captured["command"] == "/file note.txt summarize"
    assert captured["upload_callable"] is fake_upload
    assert prompt == "summarize"
    assert attachments == [{"type": "text/plain", "name": "note.txt"}]
