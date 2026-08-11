"""Shared DRACO input and result artifact indexing helpers.

These helpers intentionally stay independent from run/resume orchestration so
both entry points bind task identity and result coverage with one contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "load_tasks",
    "parse_maybe_json",
    "result_key_coverage",
]


def parse_maybe_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def load_tasks(path: Path, *, max_tasks: int = 0) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    task_id_lines: dict[str, int] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        payload = json.loads(line)
        task_id = str(payload.get("id") or payload.get("task_id") or "").strip()
        prompt = str(payload.get("prompt") or payload.get("problem") or "").strip()
        if not task_id or not prompt:
            raise ValueError(f"{path}:{lineno} requires non-empty id/task_id and prompt/problem")
        prior_lineno = task_id_lines.get(task_id)
        if prior_lineno is not None:
            raise ValueError(
                f"{path}:{lineno} duplicate task id {task_id!r}; "
                f"first declared on line {prior_lineno}"
            )
        task_id_lines[task_id] = lineno
        payload["id"] = task_id
        payload["prompt"] = prompt
        if "rubric" in payload:
            payload["rubric"] = parse_maybe_json(payload["rubric"])
        elif "answer" in payload:
            payload["rubric"] = parse_maybe_json(payload["answer"])
        tasks.append(payload)
        if max_tasks and len(tasks) >= max_tasks:
            break
    return tasks


def result_key_coverage(
    rows: list[dict[str, Any]],
    *,
    expected_keys: set[tuple[str, str]],
) -> dict[str, Any]:
    """Audit exact one-row coverage for every normalized group/task key."""

    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (
            str(row.get("group") or "").strip().upper(),
            str(row.get("task_id") or "").strip(),
        )
        counts[key] = counts.get(key, 0) + 1
    actual_keys = set(counts)
    missing = sorted(expected_keys - actual_keys)
    unexpected = sorted(actual_keys - expected_keys)
    duplicates = sorted(key for key, count in counts.items() if count > 1)
    row = {
        "pass": not missing and not unexpected and not duplicates,
        "expected_row_count": len(expected_keys),
        "actual_row_count": len(rows),
        "actual_unique_key_count": len(actual_keys),
        "missing_keys": [list(key) for key in missing],
        "unexpected_keys": [list(key) for key in unexpected],
        "duplicate_keys": [{"key": list(key), "count": counts[key]} for key in duplicates],
    }
    return row
