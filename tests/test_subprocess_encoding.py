"""Tests for subprocess output decoding and UTF-8 child-env forcing (issue #336).

The Windows fallback path is exercised deterministically on any OS by passing an
explicit ``fallback_encoding`` and by monkeypatching ``os.name`` -- the CI hosts
are POSIX, so we cannot rely on the ambient code page.
"""

from __future__ import annotations

import pytest

from opensquilla import subprocess_encoding
from opensquilla.subprocess_encoding import apply_utf8_child_env, decode_subprocess_output

_HELLO = "你好世界"


class TestDecodeSubprocessOutput:
    def test_gbk_bytes_with_fallback_decode_correctly(self) -> None:
        raw = _HELLO.encode("gbk")
        assert decode_subprocess_output(raw, fallback_encoding="gbk") == _HELLO

    def test_utf8_bytes_win_over_fallback(self) -> None:
        # Strict UTF-8 is tried first, so genuine UTF-8 output is preserved even
        # when a legacy fallback encoding is configured.
        raw = _HELLO.encode("utf-8")
        assert decode_subprocess_output(raw, fallback_encoding="gbk") == _HELLO

    def test_no_fallback_matches_legacy_behavior(self) -> None:
        # POSIX / fallback disabled must behave exactly like the previous code:
        # UTF-8 with replacement.
        raw = _HELLO.encode("gbk")
        assert decode_subprocess_output(raw, fallback_encoding=None) == raw.decode(
            "utf-8", errors="replace"
        )

    def test_valid_utf8_with_no_fallback_is_exact(self) -> None:
        raw = _HELLO.encode("utf-8")
        assert decode_subprocess_output(raw, fallback_encoding=None) == _HELLO

    def test_empty_and_none(self) -> None:
        assert decode_subprocess_output(b"", fallback_encoding="gbk") == ""
        assert decode_subprocess_output(None, fallback_encoding="gbk") == ""

    def test_undecodable_in_both_never_raises(self) -> None:
        # 0xFF is invalid UTF-8 and (as a lone byte) invalid in utf-16 too; the
        # final replacement safety net must keep us from raising.
        result = decode_subprocess_output(b"\xff\xfe\x00bad", fallback_encoding="utf-16")
        assert isinstance(result, str)

    def test_bad_fallback_encoding_name_falls_back_to_replace(self) -> None:
        raw = _HELLO.encode("gbk")
        result = decode_subprocess_output(raw, fallback_encoding="not-a-real-codec")
        assert result == raw.decode("utf-8", errors="replace")

    def test_ascii_is_stable_across_paths(self) -> None:
        raw = b"exit_code=0\nhello\n"
        assert decode_subprocess_output(raw, fallback_encoding="gbk") == raw.decode()
        assert decode_subprocess_output(raw, fallback_encoding=None) == raw.decode()

    def test_chunk_boundary_split_reassembles(self) -> None:
        # Emulate the background_process fix: raw bytes are accumulated and the
        # whole buffer is decoded once, so a multibyte char split across a read
        # boundary is not garbled the way per-chunk decoding was.
        full = _HELLO.encode("gbk")
        first, second = full[:3], full[3:]  # splits a 2-byte GBK char
        per_chunk = first.decode("gbk", errors="replace") + second.decode("gbk", errors="replace")
        buffer = bytearray(first)
        buffer.extend(second)
        whole = decode_subprocess_output(bytes(buffer), fallback_encoding="gbk")
        assert whole == _HELLO
        assert per_chunk != _HELLO  # the old per-chunk approach was lossy

    def test_truncated_gbk_tail_does_not_garble_whole_buffer(self) -> None:
        # Byte-cap truncation cuts the last GBK character in half.  A single cut
        # trailing byte must not collapse the whole buffer to replacement chars.
        body = (_HELLO * 500).encode("gbk")
        truncated = body[:-1]
        result = decode_subprocess_output(truncated, fallback_encoding="gbk")
        assert result.startswith(_HELLO * 10)
        assert "�" not in result  # only the dropped tail is lost, nothing garbled
        assert result == (_HELLO * 500)[:-1]

    def test_truncated_utf8_tail_drops_cleanly(self) -> None:
        # UTF-8 output truncated mid-character (e.g. PYTHONUTF8 child hitting the
        # byte cap) keeps the valid prefix and drops the incomplete tail.
        body = (_HELLO * 500).encode("utf-8")
        truncated = body[:-1]
        result = decode_subprocess_output(truncated, fallback_encoding="gbk")
        assert result == (_HELLO * 500)[:-1]
        assert "�" not in result

    def test_truncated_gbk_with_utf8_lookalike_body_prefers_codepage(self) -> None:
        # 聙 is 0xC2 0x80 in GBK, which is *also* a valid UTF-8 sequence (U+0080).
        # With a truncated trailing lead byte a lenient UTF-8 decode would emit the
        # U+0080 control char and mojibake the real GBK text; scoring by
        # replacement count must keep the code-page reading instead.
        raw = b"\xc2\x80\xe4"  # 聙 + an incomplete trailing multibyte lead
        assert decode_subprocess_output(raw, fallback_encoding="gbk") == "聙"

    def test_truncated_utf8_beats_codepage_on_score(self) -> None:
        # The mirror case: genuinely UTF-8 output cut mid-character must not be
        # dragged into the code page even though the fallback is configured.
        raw = (_HELLO * 50).encode("utf-8")[:-1]
        assert decode_subprocess_output(raw, fallback_encoding="gbk") == (_HELLO * 50)[:-1]

    def test_complete_gbk_valid_as_utf8_still_prefers_codepage(self) -> None:
        # 聙聛 in GBK is 0xC2 0x80 0xC2 0x81 -- a *complete*, fully valid UTF-8
        # string (U+0080 U+0081).  The clean-UTF-8 fast path must not short-circuit
        # past scoring and hand back the C1-control mojibake.
        raw = "聙聛".encode("gbk")
        assert raw == b"\xc2\x80\xc2\x81"
        assert decode_subprocess_output(raw, fallback_encoding="gbk") == "聙聛"

    def test_realistic_output_survives_truncation_both_encodings(self) -> None:
        # Real mixed CJK+ASCII output, in either encoding, truncated by up to a
        # full multibyte char, must decode cleanly and keep the right prefix.
        text = "你好，OpenSquilla 支持中文输出！日志：正常运行 abc123。"
        for encoding in ("utf-8", "gbk"):
            body = (text * 5).encode(encoding)
            for cut in range(5):
                raw = body[: len(body) - cut]
                got = decode_subprocess_output(raw, fallback_encoding="gbk")
                assert "�" not in got, (encoding, cut)
                assert got.startswith("你好，OpenSquilla"), (encoding, cut)


class TestApplyUtf8ChildEnv:
    def test_sets_vars_on_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subprocess_encoding.os, "name", "nt")
        env: dict[str, str] = {}
        result = apply_utf8_child_env(env)
        assert result is env  # mutates and returns the same dict
        assert env["PYTHONUTF8"] == "1"
        assert env["PYTHONIOENCODING"] == "utf-8"

    def test_does_not_override_existing_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subprocess_encoding.os, "name", "nt")
        env = {"PYTHONIOENCODING": "latin-1", "PYTHONUTF8": "0"}
        apply_utf8_child_env(env)
        assert env["PYTHONIOENCODING"] == "latin-1"
        assert env["PYTHONUTF8"] == "0"

    def test_noop_on_posix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subprocess_encoding.os, "name", "posix")
        env: dict[str, str] = {}
        apply_utf8_child_env(env)
        assert env == {}
