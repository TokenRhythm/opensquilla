#!/usr/bin/env python3
"""Extract text from a .pptx file using the python-pptx public API.

Usage:
    python extract_text.py input.pptx
    python extract_text.py input.pptx --json
    python extract_text.py input.pptx --include-notes

Output (default):
    --- slide 1 ---
    Title text
    Bullet a
    Bullet b
    --- slide 2 ---
    ...

Output (--json):
    [
      {"slide": 1, "text": ["Title text", "Bullet a", "Bullet b"], "notes": "..."},
      ...
    ]

Exit codes:
    0  success
    1  unexpected error (file unreadable, parse failure)
    2  argument error or python-pptx not installed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_python_pptx():
    try:
        from pptx import Presentation
    except ImportError:
        sys.stderr.write(
            "python-pptx is not installed. Install it with one of:\n"
            "    uv pip install python-pptx\n"
            "    pip install python-pptx\n"
        )
        raise SystemExit(2)
    return Presentation


def _shape_text(shape) -> list[str]:
    """Return non-empty paragraph strings from a shape's text frame."""
    if not getattr(shape, "has_text_frame", False):
        return []
    out: list[str] = []
    for para in shape.text_frame.paragraphs:
        line = "".join(run.text for run in para.runs).strip()
        if line:
            out.append(line)
    return out


def _table_text(shape) -> list[str]:
    """Return cell strings from a table shape, row by row."""
    if not getattr(shape, "has_table", False):
        return []
    out: list[str] = []
    for row in shape.table.rows:
        cells = [
            "".join(
                run.text
                for para in cell.text_frame.paragraphs
                for run in para.runs
            ).strip()
            for cell in row.cells
        ]
        cells = [c for c in cells if c]
        if cells:
            out.append(" | ".join(cells))
    return out


def _slide_text(slide) -> list[str]:
    """Walk shapes (and one level of grouped shapes) collecting text."""
    out: list[str] = []
    for shape in slide.shapes:
        out.extend(_shape_text(shape))
        out.extend(_table_text(shape))
        # one level of group expansion (sufficient for most decks)
        if getattr(shape, "shape_type", None) and getattr(shape, "shapes", None):
            try:
                for inner in shape.shapes:
                    out.extend(_shape_text(inner))
                    out.extend(_table_text(inner))
            except (AttributeError, TypeError):
                pass
    return out


def _notes_text(slide) -> str:
    notes = getattr(slide, "notes_slide", None)
    if not notes:
        return ""
    tf = getattr(notes, "notes_text_frame", None)
    if not tf:
        return ""
    return "\n".join(p.text for p in tf.paragraphs if p.text.strip())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Extract slide text from a .pptx file."
    )
    ap.add_argument("path", type=Path, help="Path to .pptx file")
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    ap.add_argument(
        "--include-notes",
        action="store_true",
        help="Include speaker notes in output",
    )
    args = ap.parse_args(argv)

    if not args.path.is_file():
        sys.stderr.write(f"File not found: {args.path}\n")
        return 1
    if args.path.suffix.lower() != ".pptx":
        sys.stderr.write(
            f"Expected .pptx, got {args.path.suffix}. "
            "Convert .ppt/.key/Google-Slides exports to .pptx first.\n"
        )
        return 1

    Presentation = _load_python_pptx()  # noqa: N806  late-imported class
    try:
        prs = Presentation(str(args.path))
    except Exception as exc:  # noqa: BLE001  surface the upstream error verbatim
        sys.stderr.write(f"Failed to parse {args.path}: {exc}\n")
        return 1

    slides_data: list[dict] = []
    for i, slide in enumerate(prs.slides, 1):
        slides_data.append(
            {
                "slide": i,
                "text": _slide_text(slide),
                **(
                    {"notes": _notes_text(slide)}
                    if args.include_notes
                    else {}
                ),
            }
        )

    if args.json:
        json.dump(slides_data, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    for entry in slides_data:
        sys.stdout.write(f"--- slide {entry['slide']} ---\n")
        for line in entry["text"]:
            sys.stdout.write(line + "\n")
        if args.include_notes and entry.get("notes"):
            sys.stdout.write("[notes]\n")
            sys.stdout.write(entry["notes"] + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
