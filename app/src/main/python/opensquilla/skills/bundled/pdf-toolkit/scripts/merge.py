"""Merge whole PDFs or page ranges from multiple PDFs.

Usage:
    merge.py a.pdf b.pdf --out combined.pdf
    merge.py manifest.json --out combined.pdf

Manifest schema:
    [{"file": "a.pdf", "pages": "1-3"},
     {"file": "b.pdf"}]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path

from pypdf import PdfReader, PdfWriter


def parse_ranges(spec: str | None, total: int) -> list[int]:
    if not spec:
        return list(range(1, total + 1))
    pages: list[int] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            lo_s, hi_s = token.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
            if lo > hi:
                lo, hi = hi, lo
            pages.extend(range(lo, hi + 1))
        else:
            pages.append(int(token))
    return [p for p in pages if 1 <= p <= total]


def merge(items: Iterable[dict[str, str]], out: Path) -> int:
    writer = PdfWriter()
    count = 0
    for item in items:
        path = Path(item["file"])
        if not path.is_file():
            print(f"warn: missing {path}", file=sys.stderr)
            continue
        reader = PdfReader(str(path))
        total = len(reader.pages)
        for page_num in parse_ranges(item.get("pages"), total):
            writer.add_page(reader.pages[page_num - 1])
            count += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as fh:
        writer.write(fh)
    return count


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge PDFs or page ranges.")
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Either N PDF paths, or one .json manifest path",
    )
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    items: list[dict[str, str]]
    if len(args.inputs) == 1 and args.inputs[0].endswith(".json"):
        manifest_path = Path(args.inputs[0])
        if not manifest_path.is_file():
            print(f"error: manifest {manifest_path} not found", file=sys.stderr)
            return 2
        items = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            print("error: manifest must be a JSON array", file=sys.stderr)
            return 2
    else:
        items = [{"file": p} for p in args.inputs]
    written = merge(items, args.out)
    print(json.dumps({"pages_written": written, "out": str(args.out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
