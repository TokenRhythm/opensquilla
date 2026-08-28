"""Extract text and tables from a PDF using pdfplumber + pypdf metadata."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pdfplumber
from pypdf import PdfReader


def extract(path: Path, tables_strategy: str | None) -> dict[str, Any]:
    reader = PdfReader(str(path))
    metadata: dict[str, Any] = {}
    if reader.metadata is not None:
        for key, value in reader.metadata.items():
            metadata[str(key).lstrip("/")] = str(value)

    pages_text: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    table_settings = {"vertical_strategy": tables_strategy or "lines"}
    with pdfplumber.open(str(path)) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            content = page.extract_text() or ""
            pages_text.append({"page": idx, "content": content})
            for tbl in page.extract_tables(table_settings) or []:
                tables.append({"page": idx, "rows": tbl})

    return {
        "pages": len(pages_text),
        "metadata": metadata,
        "text": pages_text,
        "tables": tables,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract text and tables from a PDF.")
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--tables-strategy",
        choices=("lines", "text", "explicit"),
        default=None,
        help="pdfplumber table-detection strategy",
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="Force JSON output (default)")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.path.is_file():
        print(f"error: {args.path} not found", file=sys.stderr)
        return 2
    payload = extract(args.path, args.tables_strategy)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
