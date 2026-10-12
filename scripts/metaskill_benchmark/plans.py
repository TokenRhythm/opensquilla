"""Render the frozen MetaSkill graphs using the experiment's C semantics."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import yaml


def meta_name(task_id: str) -> str:
    return f"formal-{task_id.replace('_', '-')}-shared"


def render_plan(task: dict[str, Any], canonical: dict[str, Any],
                runtime_names: dict[str, str]) -> str:
    """Return a SKILL.md template; the runner fills ``__WORKSPACE__`` per run.

    Each dependency contributes its predecessor text, truncated to 6,000
    characters. This deliberately preserves the original renderer, which
    populated dependencies even when a canonical input lacked ``outputs.*``.
    """
    if task["task_id"] != canonical["task_id"]:
        raise ValueError("Task and canonical plan IDs differ")
    nodes = canonical["nodes"]
    ids = [node["id"] for node in nodes]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("A canonical plan needs unique nonempty node IDs")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in ids):
        raise ValueError("Invalid node ID")
    graph = {node["id"]: node.get("depends_on", []) for node in nodes}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name not in graph:
            raise ValueError(f"Unknown dependency: {name}")
        if name in visiting:
            raise ValueError("Canonical plan contains a cycle")
        if name in visited:
            return
        visiting.add(name)
        for predecessor in graph[name]:
            visit(predecessor)
        visiting.remove(name)
        visited.add(name)

    for name in ids:
        visit(name)
    plan_hash = hashlib.sha256(
        (json.dumps(canonical, ensure_ascii=False, indent=2) + "\n").encode()
    ).hexdigest()
    steps = []
    for node in nodes:
        logical = node["skill"].split(",")[0].strip()
        if logical not in runtime_names:
            raise ValueError(f"Unknown designated Skill: {logical}")
        task_text = (
            node["objective"] + " Required outputs: "
            + ", ".join(node.get("required_outputs", []))
            + ". Exact workspace: __WORKSPACE__. Keep all final files inside that workspace. "
            + "Full benchmark request: {{ inputs.user_message | xml_escape | truncate(12000) }}"
        )
        step: dict[str, Any] = {
            "id": node["id"], "kind": "agent", "skill": runtime_names[logical],
            "read_only": not node.get("write_access", True),
            "write_access": node.get("write_access", True),
            "network_access": node.get("network_access", False),
            "with": {"task": task_text},
        }
        if node.get("depends_on"):
            step["depends_on"] = node["depends_on"]
            step["with"]["prior_outputs"] = {
                predecessor: "{{ outputs." + predecessor + " | truncate(6000) }}"
                for predecessor in node["depends_on"]
            }
        steps.append(step)
    frontmatter = {
        "name": meta_name(task["task_id"]),
        "description": f"Execute the frozen canonical plan for {task['task_id']}.",
        "kind": "meta", "meta_priority": 50, "always": False,
        "triggers": [f"run formal shared canonical {task['task_id']}"],
        "metadata": {
            "opensquilla": {
                "risk": "medium", "read_only": False, "write_access": True,
                "network_access": any(node.get("network_access", False) for node in nodes),
            },
            "benchmark": {"task_id": task["task_id"], "canonical_plan_sha256": plan_hash},
        },
        "composition": {"steps": steps},
    }
    return (
        "---\n" + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, width=100000)
        + "---\n\n# MetaSkill benchmark plan\n\n"
        + f"Task: `{task['task_id']}`\n"
    )
