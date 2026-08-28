"""Windows directory-picker child process for the gateway."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence


def _pick_directory(initial_dir: str | None = None) -> str | None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        selected = filedialog.askdirectory(
            parent=root,
            initialdir=initial_dir or "",
            mustexist=True,
        )
    finally:
        root.destroy()

    return selected or None


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    initial_dir = args[0] if args else None
    try:
        selected = _pick_directory(initial_dir)
    except Exception as exc:  # pragma: no cover - host GUI failure
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps({"path": selected}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    raise SystemExit(main())
