"""A deterministic Skill entrypoint using only the Python standard library."""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("upper", "lower"))
    args = parser.parse_args()
    text = sys.stdin.read()
    print(text.upper() if args.mode == "upper" else text.lower())


if __name__ == "__main__":
    main()
