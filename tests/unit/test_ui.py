from __future__ import annotations

import sys
from io import BytesIO, TextIOWrapper

from opensquilla.ui import _DynamicStream


def test_dynamic_stream_write_returns_length_when_wrapped_stream_returns_none(
    monkeypatch,
) -> None:
    class NullStyleStream:
        def __init__(self) -> None:
            self.writes: list[str] = []

        def write(self, data: str) -> None:
            self.writes.append(data)
            return None

    stream = NullStyleStream()
    monkeypatch.setattr(sys, "stdout", stream)

    assert _DynamicStream("stdout").write("sample payload") == len("sample payload")
    assert stream.writes == ["sample payload"]


def test_dynamic_stream_replaces_unencodable_text_for_legacy_gbk_stream(
    monkeypatch,
) -> None:
    raw = BytesIO()
    stream = TextIOWrapper(raw, encoding="cp936", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)

    assert _DynamicStream("stdout").write("中文 🦐") == len("中文 🦐")
    stream.flush()

    assert raw.getvalue().decode("cp936") == "中文 ?"


class _ClosedPipeStream:
    """A stdout whose reader already went away, as with `| head`."""

    def write(self, data: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


def test_dynamic_stream_treats_a_closed_pipe_as_end_of_output(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdout", _ClosedPipeStream())

    stream = _DynamicStream("stdout")

    assert stream.write("sample payload") == len("sample payload")
    stream.flush()


class _InvalidArgumentStream:
    """Windows colorama raises OSError(EINVAL) instead of BrokenPipeError."""

    def write(self, data: str) -> int:
        raise OSError(22, "Invalid argument")

    def flush(self) -> None:
        raise OSError(22, "Invalid argument")


def test_dynamic_stream_treats_an_invalid_argument_write_as_end_of_output(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sys, "stdout", _InvalidArgumentStream())

    stream = _DynamicStream("stdout")

    assert stream.write("sample payload") == len("sample payload")
    stream.flush()
