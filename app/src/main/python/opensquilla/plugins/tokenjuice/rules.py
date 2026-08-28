from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

from .types import Rule


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_tuple_of_dicts(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _load_rule(path: resources.abc.Traversable) -> Rule | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not rule_id:
        return None
    family = raw.get("family")
    on_empty = raw.get("onEmpty")
    counter_source = raw.get("counterSource")
    return Rule(
        id=rule_id,
        family=family if isinstance(family, str) else "generic",
        match=_as_dict(raw.get("match")),
        transforms=_as_dict(raw.get("transforms")),
        filters=_as_dict(raw.get("filters")),
        summarize=_as_dict(raw.get("summarize")),
        failure=_as_dict(raw.get("failure")),
        counters=_as_tuple_of_dicts(raw.get("counters")),
        output_matches=_as_tuple_of_dicts(raw.get("outputMatches")),
        on_empty=on_empty if isinstance(on_empty, str) else None,
        counter_source=counter_source if isinstance(counter_source, str) else "postKeep",
        priority=int(raw.get("priority") or 0),
    )


def _iter_json_files(root: resources.abc.Traversable):
    for child in root.iterdir():
        if child.name == "fixtures":
            continue
        if child.is_dir():
            yield from _iter_json_files(child)
        elif child.name.endswith(".json"):
            yield child


@lru_cache(maxsize=1)
def load_rules() -> tuple[Rule, ...]:
    root = resources.files("opensquilla.plugins.tokenjuice").joinpath("rules")
    rules = [rule for path in _iter_json_files(root) if (rule := _load_rule(path)) is not None]
    return tuple(
        sorted(
            rules,
            key=lambda rule: (
                rule.id == "generic/fallback",
                -rule.priority,
                rule.id,
            ),
        )
    )
