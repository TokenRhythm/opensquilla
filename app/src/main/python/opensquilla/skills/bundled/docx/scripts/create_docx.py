"""Create a `.docx` from a declarative JSON spec.

Spec schema:
    {
      "metadata": {"title": "...", "author": "..."},
      "body": [
        {"kind": "heading", "level": 1, "text": "..."},
        {"kind": "paragraph", "text": "...", "style": "Normal"},
        {"kind": "table", "rows": [["..."]]}
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from docx import Document


def build(spec: dict[str, Any]) -> Document:
    doc = Document()

    meta = spec.get("metadata", {})
    if isinstance(meta, dict):
        core = doc.core_properties
        if "title" in meta:
            core.title = str(meta["title"])
        if "author" in meta:
            core.author = str(meta["author"])

    for item in spec.get("body", []):
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind == "heading":
            doc.add_heading(str(item.get("text", "")), level=int(item.get("level", 1)))
        elif kind == "paragraph":
            style = item.get("style") or "Normal"
            doc.add_paragraph(str(item.get("text", "")), style=style)
        elif kind == "table":
            rows = item.get("rows") or []
            if not rows:
                continue
            ncols = max(len(r) for r in rows)
            table = doc.add_table(rows=len(rows), cols=ncols)
            for r_idx, row in enumerate(rows):
                for c_idx, value in enumerate(row):
                    table.rows[r_idx].cells[c_idx].text = str(value)
        elif kind == "page_break":
            doc.add_page_break()
    return doc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a .docx from a JSON spec.")
    parser.add_argument("spec", type=Path, help="Path to a JSON spec file")
    parser.add_argument("--out", type=Path, required=True, help="Output .docx path")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.spec.is_file():
        print(f"error: spec {args.spec} not found", file=sys.stderr)
        return 2
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    doc = build(spec)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
