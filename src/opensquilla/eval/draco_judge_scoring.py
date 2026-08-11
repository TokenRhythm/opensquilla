"""Pure scoring helpers shared by DRACO run and resume workflows."""

from __future__ import annotations

from typing import Any


def coerce_weight(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def clamp_percent(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def score_criterion_judgments(
    *,
    rubric_id: str,
    judgments: list[dict[str, Any]],
    judge_model: str,
    judge_repeats: int = 1,
) -> dict[str, Any]:
    valid_judgments = [item for item in judgments if isinstance(item.get("met"), bool)]
    invalid_count = len(judgments) - len(valid_judgments)
    score_status = "partial" if invalid_count else "complete"
    positive_weight_total = sum(max(0, coerce_weight(item.get("weight"))) for item in judgments)
    raw_score = sum(
        coerce_weight(item.get("weight"))
        for item in valid_judgments
        if item.get("met") is True
    )
    valid_positive_weight_total = sum(
        max(0, coerce_weight(item.get("weight"))) for item in valid_judgments
    )
    valid_normalized = (
        clamp_percent((raw_score / valid_positive_weight_total) * 100.0)
        if valid_positive_weight_total > 0
        else None
    )
    normalized = (
        clamp_percent((raw_score / positive_weight_total) * 100.0)
        if positive_weight_total > 0
        else None
    )

    def _passed(item: dict[str, Any]) -> bool:
        weight = coerce_weight(item.get("weight"))
        met = item.get("met")
        return bool(met) if weight >= 0 else met is False

    valid_passed = [_passed(item) for item in valid_judgments]
    valid_pass_rate = (
        sum(1 for item in valid_passed if item) / len(valid_passed) * 100.0
        if valid_passed
        else None
    )
    section_scores: dict[str, dict[str, Any]] = {}
    for item in judgments:
        section_id = str(item.get("section_id") or "rubric")
        section = section_scores.setdefault(
            section_id,
            {
                "title": item.get("section_title") or section_id,
                "criteria_count": 0,
                "valid_criteria_count": 0,
                "invalid_criteria_count": 0,
                "raw_score": 0,
                "positive_weight_total": 0,
                "valid_positive_weight_total": 0,
                "passed_count": 0,
            },
        )
        weight = coerce_weight(item.get("weight"))
        met = item.get("met")
        section["criteria_count"] += 1
        section["positive_weight_total"] += max(0, weight)
        if isinstance(met, bool):
            section["valid_criteria_count"] += 1
            section["valid_positive_weight_total"] += max(0, weight)
        else:
            section["invalid_criteria_count"] += 1
            continue
        if met is True:
            section["raw_score"] += weight
        if (met is True and weight >= 0) or (met is False and weight < 0):
            section["passed_count"] += 1
    for section in section_scores.values():
        total = section["positive_weight_total"]
        valid_total = section["valid_positive_weight_total"]
        valid_section_normalized = (
            clamp_percent((section["raw_score"] / valid_total) * 100.0)
            if valid_total > 0
            else None
        )
        valid_section_pass_rate = (
            section["passed_count"] / section["valid_criteria_count"] * 100.0
            if section["valid_criteria_count"]
            else None
        )
        section["score_status"] = (
            "partial" if section["invalid_criteria_count"] else "complete"
        )
        section["valid_normalized_score"] = valid_section_normalized
        section["valid_pass_rate"] = valid_section_pass_rate
        section["normalized_score"] = (
            clamp_percent((section["raw_score"] / total) * 100.0)
            if total > 0 and not section["invalid_criteria_count"]
            else None
        )
        section["pass_rate"] = (
            valid_section_pass_rate if not section["invalid_criteria_count"] else None
        )
    judge_error_count = sum(
        1 for item in judgments if item.get("error") or not isinstance(item.get("met"), bool)
    )
    return {
        "mode": "draco_criterion_judgments",
        "rubric_id": rubric_id,
        "judge_model": judge_model,
        "judge_repeats": judge_repeats,
        "rubric_criteria_count": (len(judgments) // max(1, judge_repeats) if judgments else 0),
        "criteria_count": len(judgments),
        "valid_criteria_count": len(valid_judgments),
        "invalid_criteria_count": invalid_count,
        "score_status": score_status,
        "raw_score": raw_score,
        "positive_weight_total": positive_weight_total,
        "valid_positive_weight_total": valid_positive_weight_total,
        "valid_normalized_score": valid_normalized,
        "valid_pass_rate": valid_pass_rate,
        "normalized_score": normalized if score_status == "complete" else None,
        "pass_rate": valid_pass_rate if score_status == "complete" else None,
        "section_scores": section_scores,
        "criterion_judgments": judgments,
        "judge_error_count": judge_error_count,
        "total": normalized if score_status == "complete" else None,
    }
