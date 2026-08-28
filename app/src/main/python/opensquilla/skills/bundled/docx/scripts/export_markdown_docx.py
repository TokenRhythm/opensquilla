"""Export simple markdown text to a `.docx` file."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import create_docx


def _is_table_separator(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(cell and set(cell) <= {"-", ":"} for cell in cells)


def _table_rows(lines: list[str], start: int) -> tuple[list[list[str]], int] | None:
    if start + 1 >= len(lines) or "|" not in lines[start]:
        return None
    if not _is_table_separator(lines[start + 1]):
        return None

    rows: list[list[str]] = []
    index = start
    while index < len(lines) and "|" in lines[index]:
        if not _is_table_separator(lines[index]):
            rows.append([cell.strip() for cell in lines[index].strip().strip("|").split("|")])
        index += 1
    return rows, index


def markdown_to_spec(markdown: str) -> dict[str, Any]:
    body: list[dict[str, Any]] = []
    pending: list[str] = []
    lines = markdown.splitlines()
    index = 0

    def flush_pending() -> None:
        if pending:
            body.append({"kind": "paragraph", "text": " ".join(pending)})
            pending.clear()

    while index < len(lines):
        raw = lines[index].rstrip()
        line = raw.strip()
        if not line:
            flush_pending()
            index += 1
            continue

        table = _table_rows(lines, index)
        if table is not None:
            flush_pending()
            rows, index = table
            if rows:
                body.append({"kind": "table", "rows": rows})
            continue

        if line.startswith("#"):
            marker, _, text = line.partition(" ")
            if marker and set(marker) == {"#"} and len(marker) <= 6:
                flush_pending()
                body.append(
                    {
                        "kind": "heading",
                        "level": min(len(marker), 4),
                        "text": text.strip() or line.lstrip("#").strip(),
                    }
                )
                index += 1
                continue

        pending.append(line)
        index += 1

    flush_pending()
    if not body:
        body.append({"kind": "paragraph", "text": markdown.strip()})
    return {"body": body}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export markdown text to a .docx.")
    parser.add_argument("--out", type=Path, required=True, help="Output .docx path")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    markdown = sys.stdin.read()
    doc = create_docx.build(markdown_to_spec(markdown))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(args.out))
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
