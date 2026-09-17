"""Deterministically compare citation-map statistics with the paper contract."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

_TARGET_RE = re.compile(
    r"^\s*CITATION_TARGET\s*:\s*(?:>=|≥|at\s+least\s+)?(\d+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SUMMARY_RE = re.compile(r"^\s*SUMMARY\s*:\s*([^\n]+?)\s*$", re.MULTILINE)
_REQUIRED_FIELDS = (
    "total_cite_keys",
    "strong",
    "ok",
    "weak",
    "invalid",
    "unused",
)


def _target(payload: dict[str, Any]) -> int | None:
    for key in ("paper_contract", "paper_preferences"):
        match = _TARGET_RE.search(str(payload.get(key) or ""))
        if match:
            return int(match.group(1))
    return None


def _summary(citation_map: str) -> tuple[dict[str, int], list[str]]:
    blockers: list[str] = []
    matches = _SUMMARY_RE.findall(citation_map)
    if len(matches) != 1:
        blockers.append(
            "citation_map must contain exactly one machine-readable SUMMARY line"
        )
        return {}, blockers
    raw = matches[0]
    fields: dict[str, int] = {}
    for name, value in re.findall(r"([a-z_]+)\s*=\s*([^,\s]+)", raw, re.IGNORECASE):
        normalized = name.lower()
        if normalized in fields:
            blockers.append(f"citation_map SUMMARY repeats field {normalized}")
            continue
        if not value.isdigit():
            blockers.append(
                f"citation_map SUMMARY field {normalized} is not a non-negative integer"
            )
            continue
        fields[normalized] = int(value)
    for name in _REQUIRED_FIELDS:
        if name not in fields:
            blockers.append(f"citation_map SUMMARY is missing {name}")
    return fields, blockers


def audit(payload: dict[str, Any]) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    target = _target(payload)
    if target is None:
        blockers.append("CITATION_TARGET must be a machine-readable integer")
        target_value = 0
    else:
        target_value = target
        if not 1 <= target <= 500:
            blockers.append(f"CITATION_TARGET must be between 1 and 500; got {target}")

    fields, summary_blockers = _summary(str(payload.get("citation_map") or ""))
    blockers.extend(summary_blockers)
    total = fields.get("total_cite_keys", 0)
    strong = fields.get("strong", 0)
    ok_count = fields.get("ok", 0)
    weak = fields.get("weak", 0)
    invalid = fields.get("invalid", 0)
    unused = fields.get("unused", 0)

    if fields:
        classified = strong + ok_count + weak + invalid
        if classified != total:
            blockers.append(
                f"citation_map SUMMARY classifications total {classified}, not {total} cited keys"
            )
        if target_value > 0 and total < target_value:
            blockers.append(
                f"citation coverage insufficient: found {total}/{target_value} cited keys"
            )
        if invalid:
            blockers.append(f"citation_map contains {invalid} invalid cited keys")
        if weak:
            blockers.append(f"citation_map contains {weak} weak cited keys")
        if unused:
            warnings.append(f"bibliography contains {unused} unused entries")

    return {
        "verdict": "block" if blockers else "pass",
        "target": target_value,
        "total": total,
        "strong": strong,
        "ok": ok_count,
        "weak": weak,
        "invalid": invalid,
        "unused": unused,
        "blockers": blockers,
        "warnings": warnings,
    }


def _render(result: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"INTEGRITY: {result['verdict']}",
            f"CITATION_TARGET: {result['target']}",
            f"TOTAL_CITE_KEYS: {result['total']}",
            f"STRONG_COUNT: {result['strong']}",
            f"OK_COUNT: {result['ok']}",
            f"INVALID_COUNT: {result['invalid']}",
            f"WEAK_PRIMARY_COUNT: {result['weak']}",
            f"UNUSED_COUNT: {result['unused']}",
            "BLOCKERS:",
            *(f"- {item}" for item in result["blockers"] or ["none"]),
            "WARNINGS:",
            *(f"- {item}" for item in result["warnings"] or ["none"]),
        ]
    )


def _failure(message: str) -> int:
    result = {
        "verdict": "block",
        "target": 0,
        "total": 0,
        "strong": 0,
        "ok": 0,
        "weak": 0,
        "invalid": 0,
        "unused": 0,
        "blockers": [message],
        "warnings": [],
    }
    rendered = _render(result)
    print(rendered)
    print(rendered, file=sys.stderr)
    return 2


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        return _failure(f"invalid JSON input: {exc}")
    if not isinstance(payload, dict):
        return _failure("input must be a JSON object")

    result = audit(payload)
    rendered = _render(result)
    print(rendered)
    if result["verdict"] == "block":
        print(rendered, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
