#!/usr/bin/env python3
"""Validate primary webpage source output before fallback authoring."""

from __future__ import annotations

import json
import sys

from .webpage_source import (
    WebpageSourceError,
    load_source_payload,
    missing_required_keys,
)


def _print_record(label: str, payload: dict[str, object]) -> None:
    print(f"{label}: {json.dumps(payload, ensure_ascii=True, separators=(',', ':'))}")


def main() -> int:
    raw_payload_source = "" if sys.stdin.isatty() else sys.stdin.read()
    try:
        data = load_source_payload(raw_payload_source)
        missing = missing_required_keys(data)
        if missing:
            raise WebpageSourceError("missing keys " + ",".join(missing))
    except WebpageSourceError as exc:
        _print_record(
            "WEBPAGE_SOURCE_INVALID",
            {
                "reason": str(exc)[:500],
            },
        )
        return 0

    _print_record(
        "WEBPAGE_SOURCE_OK",
        {
            "keys": sorted(data),
            "summary": str(data.get("summary", ""))[:500],
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
