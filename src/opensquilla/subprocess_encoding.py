"""Encoding helpers for subprocess stdout/stderr on non-UTF-8 consoles.

On Windows the console / ANSI code page is frequently a legacy multibyte code
page (e.g. CP936/GBK on Chinese systems, CP932 on Japanese systems).  Commands
launched by ``exec_command``, ``background_process`` and ``execute_code`` emit
their output in that code page, but the gateway historically decoded every
subprocess byte stream as UTF-8.  Non-ASCII text (Chinese, Japanese, ...) then
collapsed into U+FFFD replacement characters -- the "乱码" reported in issue #336.

This module centralises two fixes:

* :func:`decode_subprocess_output` -- decode captured bytes, keeping clean UTF-8
  as-is but otherwise scoring a UTF-8 reading against the Windows system code
  page and choosing whichever looks less like a misread.
* :func:`apply_utf8_child_env` -- force Python child processes to emit UTF-8 on
  Windows so the strict UTF-8 path is taken in the first place.

On POSIX both helpers preserve the previous behaviour byte-for-byte: decoding is
UTF-8 with replacement and no environment variables are injected.
"""

from __future__ import annotations

import codecs
import locale
import os


def _windows_system_encoding() -> str | None:
    """Return the Windows console/ANSI code page name, or ``None`` off Windows.

    Prefers the console output code page (what redirected console apps and the
    ``cmd.exe`` builtins write with), then the ANSI code page, then the locale's
    preferred encoding.  Best-effort: any failure yields ``None`` so callers fall
    back to plain UTF-8 handling.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        code_page = int(kernel32.GetConsoleOutputCP()) or int(kernel32.GetACP())
        if code_page:
            return f"cp{code_page}"
    except Exception:
        pass
    preferred = locale.getpreferredencoding(False)
    return preferred or None


# Resolved once at import.  ``None`` on POSIX so decoding stays plain UTF-8 there.
_DEFAULT_FALLBACK_ENCODING = _windows_system_encoding()


# The C1 control block (U+0080..U+009F) is essentially never present in real
# program output; it only appears when legacy code-page bytes are misread as
# UTF-8, so its presence is a strong "this is not really UTF-8" signal.
_C1_CONTROL_STRIP = {codepoint: None for codepoint in range(0x80, 0xA0)}


def _decode_dropping_incomplete_tail(raw: bytes, encoding: str) -> str:
    """Decode *raw* in *encoding*, replacing bad bytes but dropping a partial tail.

    An ``errors="replace"`` incremental decoder fed with ``final=False`` emits a
    replacement character for genuinely invalid bytes, but merely *buffers* an
    incomplete multibyte sequence at the very end -- which is then discarded.  So
    a byte-cap or mid-stream truncation loses only the fraction of one cut
    character instead of producing a stray replacement char.
    """
    return codecs.getincrementaldecoder(encoding)("replace").decode(raw, final=False)


def _misread_score(text: str) -> int:
    """How much a decoded string looks like a *misread*; lower is better.

    Counts replacement characters (invalid bytes for the assumed encoding) plus
    C1 control codes (a fingerprint of code-page bytes forced through UTF-8).
    """
    replacements = text.count("�")
    c1_controls = len(text) - len(text.translate(_C1_CONTROL_STRIP))
    return replacements + c1_controls


def decode_subprocess_output(
    raw: bytes | None,
    *,
    fallback_encoding: str | None = _DEFAULT_FALLBACK_ENCODING,
) -> str:
    """Decode subprocess output bytes to text.

    Without a fallback encoding (POSIX, or the fallback explicitly disabled) this
    is exactly the historical behaviour: UTF-8 with replacement.

    With a fallback encoding (Windows) UTF-8 that decodes cleanly *and*
    plausibly (no replacement or C1-control characters) is returned as-is -- so
    anything the child emitted cleanly as UTF-8 is preserved (the common case
    once ``PYTHONUTF8`` forces child stdio to UTF-8).  Otherwise the bytes are
    truncated, legacy code page, or valid-but-implausible UTF-8: decode with
    *both* UTF-8 and the system code page -- each dropping an incomplete trailing
    sequence -- and keep the reading that looks less like a misread (fewest
    replacement + C1-control characters).

    This scoring handles the failure modes around the truncation boundary
    (issue #336): real code-page output whose bytes parse as UTF-8 is kept as
    code page (its low trail bytes make the UTF-8 reading accrue replacement or
    C1 characters -- even when the buffer is *fully* valid UTF-8, e.g. GBK
    ``0xC2 0x80`` = 聙 vs UTF-8 U+0080), while genuinely-UTF-8 output merely cut
    mid-character stays UTF-8 (a tie resolves to UTF-8).  It cannot disambiguate
    byte sequences that are individually valid *and printable* in both encodings
    -- rare in real multi-character output -- which resolve to UTF-8.
    """
    if not raw:
        return ""
    if not fallback_encoding:
        return raw.decode("utf-8", errors="replace")
    try:
        utf8_strict = raw.decode("utf-8")
    except UnicodeDecodeError:
        utf8_strict = None
    # Cleanly *and* plausibly valid UTF-8 is unambiguous; a valid-but-implausible
    # decode (C1 controls) may be misread code-page bytes and must still be scored.
    if utf8_strict is not None and _misread_score(utf8_strict) == 0:
        return utf8_strict
    utf8_text = (
        utf8_strict if utf8_strict is not None else _decode_dropping_incomplete_tail(raw, "utf-8")
    )
    try:
        codepage_text = _decode_dropping_incomplete_tail(raw, fallback_encoding)
    except LookupError:
        return raw.decode("utf-8", errors="replace")
    if _misread_score(codepage_text) < _misread_score(utf8_text):
        return codepage_text
    return utf8_text


def apply_utf8_child_env(env: dict[str, str]) -> dict[str, str]:
    """Force UTF-8 stdio for Python child processes on Windows.

    Mutates and returns *env*.  Uses ``setdefault`` so an explicit value supplied
    by the caller or the user is never overridden.  No-op on POSIX, where the
    default locale is already UTF-8.
    """
    if os.name == "nt":
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
    return env
