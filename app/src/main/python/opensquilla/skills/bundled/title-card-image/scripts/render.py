#!/usr/bin/env python3
"""Render a title/ending card as a static PNG using Pillow.

Designed as the "cover" and "ending" PNG generators for meta-short-drama:
deterministic, free, no API, CJK-friendly font fallback. The output PNG
can then be fed to video-still-animator to become a short MP4 clip.

Usage:
    python render.py --text "咖啡店偶遇" --output cover.png \\
        [--subtitle "短剧"] [--background "#101018"] \\
        [--text-color "#ffffff"] [--font-size 96] \\
        [--width 720] [--height 1280]
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterator
from pathlib import Path

_MEDIA_FONTS_DIR_ENV = "OPENSQUILLA_MEDIA_FONTS_DIR"

_PRIORITY_FONT_NAMES = (
    "NotoSansCJK-Regular.ttc",
    "NotoSansCJKsc-Regular.otf",
    "NotoSansSC-Regular.ttf",
    "SourceHanSansSC-Regular.otf",
    "SourceHanSansCN-Regular.otf",
    "PingFang.ttc",
    "Hiragino Sans GB.ttc",
    "STHeiti Medium.ttc",
    "msyh.ttc",
    "simhei.ttf",
    "simsun.ttc",
)

_SYSTEM_CJK_FONT_CANDIDATES = (
    # Windows
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\YuGothM.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Songti.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    # Linux common
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
)


def _font_directories() -> Iterator[Path]:
    """Yield explicit managed and user font directories before system paths."""
    managed = (os.environ.get(_MEDIA_FONTS_DIR_ENV) or "").strip()
    for raw in managed.split(os.pathsep) if managed else ():
        path = Path(raw).expanduser()
        if path.is_file():
            yield path
        elif path.is_dir():
            yield path

    home = Path.home()
    for path in (
        home / "Library/Fonts",
        home / ".local/share/fonts",
        home / ".fonts",
    ):
        if path.is_dir():
            yield path

    local_appdata = (os.environ.get("LOCALAPPDATA") or "").strip()
    if local_appdata:
        path = Path(local_appdata) / "Microsoft/Windows/Fonts"
        if path.is_dir():
            yield path


def _iter_font_candidates(explicit: str | None = None) -> Iterator[str]:
    """Yield deterministic font candidates, with the managed toolchain first."""
    seen: set[str] = set()

    def emit(candidate: str | Path) -> Iterator[str]:
        value = str(candidate)
        key = os.path.normcase(os.path.abspath(os.path.expanduser(value)))
        if key not in seen:
            seen.add(key)
            yield value

    if explicit:
        yield from emit(explicit)

    for location in _font_directories():
        if location.is_file():
            yield from emit(location)
            continue
        for name in _PRIORITY_FONT_NAMES:
            candidate = location / name
            if candidate.is_file():
                yield from emit(candidate)
        # User/operator directories may contain a renamed font. Keep the scan
        # deterministic and limited to font files in explicitly trusted roots.
        for pattern in ("*.ttc", "*.otc", "*.otf", "*.ttf"):
            for candidate in sorted(location.rglob(pattern)):
                if candidate.is_file():
                    yield from emit(candidate)

    windows_dir = Path(os.environ.get("WINDIR") or r"C:\Windows") / "Fonts"
    for name in _PRIORITY_FONT_NAMES:
        candidate = windows_dir / name
        if candidate.is_file():
            yield from emit(candidate)

    for candidate in _SYSTEM_CJK_FONT_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            yield from emit(path)


def _is_cjk_character(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x2E80 <= codepoint <= 0x303F
        or 0x3040 <= codepoint <= 0x30FF
        or 0x31F0 <= codepoint <= 0x31FF
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xAC00 <= codepoint <= 0xD7AF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x323AF
    )


def _glyph_signature(font, char: str) -> tuple[tuple[int, int], bytes]:
    mask = font.getmask(char, mode="L")
    return mask.size, bytes(mask)


def _font_supports_text(font, text: str) -> bool:
    """Reject fonts that render requested non-ASCII glyphs as .notdef boxes."""
    required = sorted({char for char in text if ord(char) >= 128})
    if not required:
        return True
    try:
        missing_signatures = {
            _glyph_signature(font, probe)
            for probe in ("\u0378", "\uffff", "\U0010ffff")
        }
        return all(
            _glyph_signature(font, char) not in missing_signatures
            for char in required
        )
    except (AttributeError, OSError, ValueError):
        return False


def _resolve_font_source(text: str, explicit: str | None = None) -> str | None:
    """Select a compatible scalable font, or the built-in font for ASCII."""
    from PIL import ImageFont  # type: ignore

    for candidate in _iter_font_candidates(explicit):
        try:
            font = ImageFont.truetype(candidate, 48)
        except OSError:
            continue
        if _font_supports_text(font, text):
            return candidate

    if all(ord(char) < 128 for char in text):
        return None

    requirement = "CJK-capable font" if any(map(_is_cjk_character, text)) else "compatible font"
    raise RuntimeError(
        f"No {requirement} could be loaded. Set {_MEDIA_FONTS_DIR_ENV} to a font "
        "directory or pass --font with a compatible .ttf/.otf/.ttc file."
    )


def _load_font(source: str | None, size: int):
    from PIL import ImageFont  # type: ignore

    if source is not None:
        return ImageFont.truetype(source, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        # Pillow before 10.1 has no scalable default-font size parameter.
        return ImageFont.load_default()


def _parse_color(spec: str) -> tuple[int, int, int]:
    s = spec.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(f"invalid color: {spec!r}")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _wrap_text(text: str, max_chars: int) -> list[str]:
    """Greedy line wrap that respects CJK (no spaces) and ASCII (whitespace)."""
    if not text:
        return [""]
    # If text already has explicit newlines, honour them.
    if "\n" in text:
        return text.split("\n")
    # For pure-ASCII strings, break on whitespace.
    if all(ord(c) < 0x4E00 for c in text):
        words = text.split()
        out: list[str] = []
        line = ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if len(candidate) <= max_chars:
                line = candidate
            else:
                if line:
                    out.append(line)
                line = word
        if line:
            out.append(line)
        return out or [text]
    # CJK / mixed: break at character count.
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", required=True, help="Main headline text.")
    parser.add_argument("--output", "-o", required=True)
    parser.add_argument("--subtitle", default="", help="Smaller line under the headline.")
    parser.add_argument("--background", default="#101018", help="Hex color #RRGGBB.")
    parser.add_argument("--text-color", default="#ffffff")
    parser.add_argument("--subtitle-color", default="#c8c8d0")
    parser.add_argument("--font-size", type=int, default=80)
    parser.add_argument("--subtitle-size", type=int, default=32)
    parser.add_argument("--width", type=int, default=720)
    parser.add_argument("--height", type=int, default=1280)
    parser.add_argument(
        "--max-chars-per-line", type=int, default=12,
        help="Soft wrap threshold for the headline (CJK char count). "
             "Subtitle uses 1.5× this value.",
    )
    parser.add_argument(
        "--auto-shrink", default="yes", choices=["yes", "no"],
        help="When rendered text exceeds 88%% of canvas width, shrink the font "
             "until it fits. Default yes.",
    )
    parser.add_argument("--font", default=None, help="Optional explicit font path.")
    args = parser.parse_args()

    try:
        from PIL import Image, ImageDraw  # type: ignore
    except ImportError as exc:
        print(f"Error: Pillow not installed ({exc}).", file=sys.stderr)
        return 1

    try:
        bg = _parse_color(args.background)
        fg = _parse_color(args.text_color)
        sfg = _parse_color(args.subtitle_color)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    img = Image.new("RGB", (args.width, args.height), color=bg)
    draw = ImageDraw.Draw(img)

    title_lines = _wrap_text(args.text, args.max_chars_per_line)
    sub_lines = (
        _wrap_text(args.subtitle, int(args.max_chars_per_line * 1.5))
        if args.subtitle else []
    )

    try:
        font_source = _resolve_font_source(
            "\n".join((args.text, args.subtitle)),
            args.font,
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    def _max_text_width(lines, font) -> int:
        widest = 0
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            widest = max(widest, bbox[2] - bbox[0])
        return widest

    def _fit_font(size: int, lines: list[str]) -> tuple[int, object]:
        max_safe = int(args.width * 0.88)
        font = _load_font(font_source, size)
        if args.auto_shrink == "no" or not lines:
            return size, font
        # Shrink until rendered max line fits.
        while size > 12 and _max_text_width(lines, font) > max_safe:
            size = int(size * 0.92)
            font = _load_font(font_source, size)
        return size, font

    title_size, font_title = _fit_font(args.font_size, title_lines)
    sub_size, font_sub = _fit_font(args.subtitle_size, sub_lines) if sub_lines else (args.subtitle_size, None)

    # Stack all lines, vertically centered, using the (potentially shrunken) sizes.
    line_gap_title = int(title_size * 0.25)
    line_gap_sub = int(sub_size * 0.25)
    pad_between_groups = int(title_size * 0.6)

    total_h = title_size * len(title_lines) + line_gap_title * max(0, len(title_lines) - 1)
    if sub_lines:
        total_h += pad_between_groups + sub_size * len(sub_lines) + line_gap_sub * max(0, len(sub_lines) - 1)
    y = (args.height - total_h) // 2

    def _draw_line(text: str, font, color, y_pos: int) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        x = (args.width - text_w) // 2
        draw.text((x, y_pos), text, fill=color, font=font)

    for line in title_lines:
        _draw_line(line, font_title, fg, y)
        y += title_size + line_gap_title

    if sub_lines:
        y += pad_between_groups - line_gap_title
        for line in sub_lines:
            _draw_line(line, font_sub, sfg, y)
            y += sub_size + line_gap_sub

    out_path = Path(args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
