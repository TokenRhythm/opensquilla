"""Freeze the shipped migration inventory during wheel and Desktop builds."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def freeze_registry(directory: Path, output: Path) -> None:
    migrations = {
        path.stem: {
            "ledger_hash": hashlib.sha256(path.stem.encode("utf-8")).hexdigest(),
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(directory.glob("V*.py"))
        if path.is_file()
    }
    if not migrations:
        raise ValueError("Cannot freeze an empty migration inventory")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"version": 1, "migrations": migrations}, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    freeze_registry(options.migrations, options.output)
