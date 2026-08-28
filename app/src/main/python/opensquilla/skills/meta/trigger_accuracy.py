"""Deterministic trigger-accuracy harness for meta-skill soft activation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opensquilla.engine.steps.meta_resolution import _trigger_matches
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.meta.parser import MetaPlanError, parse_meta_plan


@dataclass(frozen=True)
class TriggerCase:
    """One trigger fixture.

    ``expected_meta_skill=None`` means the prompt should not produce a
    substring-trigger candidate. This measures the deterministic hint layer;
    live LLM ``meta_invoke`` choice should be measured separately.
    """

    name: str
    user_message: str
    expected_meta_skill: str | None


def _candidate_rows(loader: SkillLoader, user_message: str) -> list[dict[str, Any]]:
    message_lower = (user_message or "").lower()
    if not message_lower:
        return []
    rows: list[dict[str, Any]] = []
    for spec in loader.load_all():
        if getattr(spec, "kind", "skill") != "meta":
            continue
        triggers = getattr(spec, "triggers", None) or []
        trigger = next(
            (
                t for t in triggers
                if isinstance(t, str) and t and _trigger_matches(t, message_lower)
            ),
            "",
        )
        if not trigger:
            continue
        try:
            plan = parse_meta_plan(spec)
        except MetaPlanError as exc:
            rows.append({
                "name": spec.name,
                "trigger": trigger,
                "priority": 0,
                "valid": False,
                "error": str(exc),
            })
            continue
        if plan is None:
            continue
        rows.append({
            "name": plan.name,
            "trigger": trigger,
            "priority": plan.priority,
            "valid": True,
        })
    rows.sort(key=lambda r: (-int(r.get("priority", 0)), str(r.get("name", ""))))
    return rows


def evaluate_trigger_cases(
    loader: SkillLoader,
    cases: list[TriggerCase],
) -> dict[str, Any]:
    """Evaluate deterministic meta trigger matching against fixtures."""
    case_rows: list[dict[str, Any]] = []
    passed = 0
    false_positives = 0
    false_negatives = 0
    wrong_skill = 0
    for case in cases:
        candidates = _candidate_rows(loader, case.user_message)
        predicted = candidates[0]["name"] if candidates else None
        ok = predicted == case.expected_meta_skill
        if ok:
            passed += 1
        elif case.expected_meta_skill is None and predicted is not None:
            false_positives += 1
        elif case.expected_meta_skill is not None and predicted is None:
            false_negatives += 1
        else:
            wrong_skill += 1
        case_rows.append({
            "name": case.name,
            "user_message": case.user_message,
            "expected_meta_skill": case.expected_meta_skill,
            "predicted_meta_skill": predicted,
            "passed": ok,
            "candidates": candidates,
        })
    total = len(cases)
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "accuracy": (passed / total) if total else 1.0,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "wrong_skill": wrong_skill,
        "cases": case_rows,
    }


__all__ = ["TriggerCase", "evaluate_trigger_cases"]
