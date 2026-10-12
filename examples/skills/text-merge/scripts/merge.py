"""Merge explicitly bound text outputs; no model or external dependency."""

from __future__ import annotations

import json
import sys


def main() -> None:
    values = json.load(sys.stdin)
    upper = values["upper"]
    lower = values["lower"]
    if not isinstance(upper, str) or not isinstance(lower, str):
        raise ValueError("upper and lower must be text")
    print(f"UPPER: {upper.strip()}")
    print(f"lower: {lower.strip()}")


if __name__ == "__main__":
    main()
