from __future__ import annotations

import sys
from io import BytesIO, TextIOWrapper

import typer

from opensquilla.cli.agent_event_stream import StderrAgentEventSink
from opensquilla.cli.stdio import configure_stdio_for_unicode
from opensquilla.engine.types import ErrorEvent


def test_configure_stdio_for_unicode_allows_typer_echo_on_gbk_stream(
    monkeypatch,
) -> None:
    raw = BytesIO()
    stream = TextIOWrapper(raw, encoding="cp936", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)

    configure_stdio_for_unicode()
    typer.echo("hello 🦐")
    stream.flush()

    assert raw.getvalue().decode("utf-8").strip() == "hello 🦐"


def test_configure_stdio_for_unicode_keeps_stderr_event_jsonl_utf8(
    monkeypatch,
) -> None:
    raw = BytesIO()
    stream = TextIOWrapper(raw, encoding="cp936", errors="strict")
    monkeypatch.setattr(sys, "stderr", stream)

    configure_stdio_for_unicode()
    StderrAgentEventSink()(ErrorEvent(code="测试", message="你好🙂"))
    stream.flush()

    assert raw.getvalue().decode("utf-8").strip() == (
        '{"_event":true,"schema_version":1,"kind":"error",'
        '"code":"测试","message":"你好🙂"}'
    )
