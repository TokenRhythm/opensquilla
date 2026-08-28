"""Apply run-level edits to an existing `.docx`.

Operations:
    {"op": "replace_run", "para": <int>, "run": <int>, "text": "..."}
    {"op": "replace_text", "find": "...", "with": "..."}

`replace_text` walks every paragraph and concatenates run texts when the
target string spans multiple runs, then writes the replacement back into the
first run and clears the others — preserving the first run's style.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from docx import Document
from docx.text.paragraph import Paragraph


def _replace_run(para: Paragraph, run_idx: int, text: str) -> None:
    if 0 <= run_idx < len(para.runs):
        para.runs[run_idx].text = text


def _replace_text_in_paragraph(para: Paragraph, find: str, replacement: str) -> bool:
    if not para.runs:
        return False
    full = "".join(run.text for run in para.runs)
    if find not in full:
        return False
    new_full = full.replace(find, replacement)
    para.runs[0].text = new_full
    for run in para.runs[1:]:
        run.text = ""
    return True


def apply_ops(doc: Document, ops: list[dict[str, Any]]) -> int:
    applied = 0
    for op in ops:
        kind = op.get("op")
        if kind == "replace_run":
            try:
                para = doc.paragraphs[int(op["para"])]
            except (KeyError, IndexError, ValueError):
                continue
            _replace_run(para, int(op.get("run", 0)), str(op.get("text", "")))
            applied += 1
        elif kind == "replace_text":
            find = str(op.get("find", ""))
            replacement = str(op.get("with", ""))
            if not find:
                continue
            for para in doc.paragraphs:
                if _replace_text_in_paragraph(para, find, replacement):
                    applied += 1
    return applied


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Edit a .docx in place via run-level ops.")
    parser.add_argument("input", type=Path, help="Path to the source .docx")
    parser.add_argument("ops", type=Path, help="JSON file containing a list of ops")
    parser.add_argument("--out", type=Path, required=True, help="Output .docx path")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.input.is_file():
        print(f"error: input {args.input} not found", file=sys.stderr)
        return 2
    if not args.ops.is_file():
        print(f"error: ops {args.ops} not found", file=sys.stderr)
        return 2
    raw = json.loads(args.ops.read_text(encoding="utf-8"))
    ops = raw if isinstance(raw, list) else []
    doc = Document(str(args.input))
    applied = apply_ops(doc, ops)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(args.out))
    print(json.dumps({"applied": applied}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
