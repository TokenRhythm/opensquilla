"""HTML recognition and bounded text validation for previews and imports."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path


def is_html(name: str, mime: str, payload: bytes | None = None) -> bool:
    if mime.split(";", 1)[0].strip().lower() in {"text/html", "application/xhtml+xml"}:
        return True
    if Path(name).suffix.lower() in {".html", ".htm", ".xhtml"}:
        return True
    if payload is None:
        return False
    try:
        prefix = payload[:4096].decode("utf-8-sig").lstrip().lower()
    except UnicodeDecodeError:
        return False
    return bool(re.match(r"<(?:!doctype\s+html\b|html\b|head\b|body\b)", prefix))


def validate_html(source: str, *, max_bytes: int = 5 * 1024 * 1024) -> None:
    if not source or "\x00" in source or len(source.encode("utf-8")) > max_bytes:
        raise ValueError("HTML must be nonempty, bounded UTF-8 text without NUL bytes")
    parser = HTMLParser()
    parser.feed(source)
    parser.close()
