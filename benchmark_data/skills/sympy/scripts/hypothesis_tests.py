#!/usr/bin/env python3
"""Structured statistical calculator for the experimental sympy Skill."""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
import sys
from typing import Any

try:
    import sympy as sp
    from scipy import stats
except ImportError as exc:  # pragma: no cover - environment-dependent
    print(json.dumps({"error": f"missing dependency: {exc.name}"}), file=sys.stderr)
    raise SystemExit(2) from exc


def _numbers(value: Any, field: str) -> list[float]:
    if not isinstance(value, list) or len(value) < 2:
        raise ValueError(f"{field} must be a list containing at least two numbers")
    result = [float(item) for item in value]
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{field} contains a non-finite number")
    return result


def _mean(values: list[float]) -> float:
    # Use SymPy for the exact sum before converting the result to JSON float.
    return float(sp.Add(*[sp.Float(value) for value in values]) / len(values))


def _t_test(payload: dict[str, Any]) -> dict[str, Any]:
    group_a = _numbers(payload.get("group_a"), "group_a")
    group_b = _numbers(payload.get("group_b"), "group_b")
    alternative = payload.get("alternative", "two-sided")
    if alternative not in {"two-sided", "less", "greater"}:
        raise ValueError("alternative must be two-sided, less, or greater")
    equal_var = bool(payload.get("equal_var", True))
    result = stats.ttest_ind(
        group_a,
        group_b,
        equal_var=equal_var,
        alternative=alternative,
    )
    mean_a, mean_b = _mean(group_a), _mean(group_b)
    pooled_variance = (
        ((len(group_a) - 1) * stats.tvar(group_a))
        + ((len(group_b) - 1) * stats.tvar(group_b))
    ) / (len(group_a) + len(group_b) - 2)
    cohens_d = (mean_a - mean_b) / math.sqrt(float(pooled_variance))
    return {
        "test": "independent_sample_t_test",
        "statistic": float(result.statistic),
        "p_value": float(result.pvalue),
        "alternative": alternative,
        "equal_var": equal_var,
        "group_a_mean": mean_a,
        "group_b_mean": mean_b,
        "effect_size_cohens_d": float(cohens_d),
    }


def _chi_square(payload: dict[str, Any]) -> dict[str, Any]:
    raw_table = payload.get("table")
    if not isinstance(raw_table, list) or len(raw_table) < 2:
        raise ValueError("table must contain at least two rows")
    table = [_numbers(row, f"table[{index}]") for index, row in enumerate(raw_table)]
    widths = {len(row) for row in table}
    if len(widths) != 1:
        raise ValueError("all table rows must have the same length")
    if any(value < 0 for row in table for value in row):
        raise ValueError("table counts must be non-negative")
    correction = bool(payload.get("correction", False))
    statistic, p_value, df, expected = stats.chi2_contingency(
        table, correction=correction
    )
    total = sum(sum(row) for row in table)
    rows, columns = len(table), len(table[0])
    cramers_v = math.sqrt(float(statistic) / (total * min(rows - 1, columns - 1)))
    return {
        "test": "chi_square_independence",
        "statistic": float(statistic),
        "p_value": float(p_value),
        "df": int(df),
        "correction": correction,
        "effect_size_cramers_v": float(cramers_v),
        "expected_frequencies": expected.tolist(),
    }


def _anova(payload: dict[str, Any]) -> dict[str, Any]:
    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, list) or len(raw_groups) < 2:
        raise ValueError("groups must contain at least two groups")
    groups = [_numbers(group, f"groups[{index}]") for index, group in enumerate(raw_groups)]
    statistic, p_value = stats.f_oneway(*groups)
    all_values = [value for group in groups for value in group]
    grand_mean = _mean(all_values)
    ss_between = sum(len(group) * (_mean(group) - grand_mean) ** 2 for group in groups)
    ss_total = sum((value - grand_mean) ** 2 for value in all_values)
    eta_squared = ss_between / ss_total if ss_total else 0.0
    return {
        "test": "one_way_anova",
        "statistic": float(statistic),
        "p_value": float(p_value),
        "group_means": [_mean(group) for group in groups],
        "effect_size_eta_squared": float(eta_squared),
    }


def _json_array(text: str, pattern: str, field: str) -> Any:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        raise ValueError(f"could not parse {field} from natural-language task")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} is not a valid JSON-style array") from exc


def _payload_from_task(text: str) -> dict[str, Any]:
    """Parse a generic three-test assignment produced by MetaSkill input."""
    control = _json_array(text, r"Control\s+group\s*(\[[^\]]+\])", "control group")
    treatment = _json_array(
        text, r"Treatment\s+group\s*(\[[^\]]+\])", "treatment group"
    )
    table = _json_array(
        text,
        r"Contingency\s+table.*?(\[\s*\[[^;]+?\]\s*\])",
        "contingency table",
    )
    drug_a = _json_array(text, r"Drug\s+A\s*(\[[^\]]+\])", "Drug A")
    drug_b = _json_array(text, r"Drug\s+B\s*(\[[^\]]+\])", "Drug B")
    drug_c = _json_array(text, r"Drug\s+C\s*(\[[^\]]+\])", "Drug C")
    filename_match = re.search(r"([A-Za-z0-9_.-]+\.json)\b", text, flags=re.IGNORECASE)
    output_json = filename_match.group(1) if filename_match else "hypothesis_tests.json"
    return {
        "test": "suite",
        "output_json": output_json,
        "t_test": {
            "group_a": treatment,
            "group_b": control,
            "alternative": "greater",
            "equal_var": True,
        },
        "chi_square": {"table": table, "correction": False},
        "anova": {"groups": [drug_a, drug_b, drug_c]},
        "raw_data": {
            "control": control,
            "treatment": treatment,
            "chi_square_table": table,
            "drug_a": drug_a,
            "drug_b": drug_b,
            "drug_c": drug_c,
        },
    }


def _suite(payload: dict[str, Any]) -> dict[str, Any]:
    t_payload = {"test": "t_test", **dict(payload.get("t_test") or {})}
    chi_payload = {"test": "chi_square", **dict(payload.get("chi_square") or {})}
    anova_payload = {"test": "anova", **dict(payload.get("anova") or {})}
    t_result = _t_test(t_payload)
    chi_result = _chi_square(chi_payload)
    anova_result = _anova(anova_payload)
    alpha = float(payload.get("alpha", 0.05))
    results = {
        "t_test": {
            "test_name": "Independent two-sample t-test (one-tailed, equal variance)",
            "statistic": t_result["statistic"],
            "p_value": t_result["p_value"],
            "conclusion": "reject" if t_result["p_value"] < alpha else "fail_to_reject",
            "effect_size": t_result["effect_size_cohens_d"],
            "control_mean": t_result["group_b_mean"],
            "treatment_mean": t_result["group_a_mean"],
            "raw_data": {
                "control": t_payload["group_b"],
                "treatment": t_payload["group_a"],
            },
        },
        "chi_square": {
            "test_name": "Chi-square test of independence",
            "statistic": chi_result["statistic"],
            "p_value": chi_result["p_value"],
            "conclusion": "reject" if chi_result["p_value"] < alpha else "fail_to_reject",
            "effect_size": chi_result["effect_size_cramers_v"],
            "df": chi_result["df"],
            "expected_frequencies": chi_result["expected_frequencies"],
            "raw_data": chi_payload["table"],
        },
        "anova": {
            "test_name": "One-way ANOVA",
            "statistic": anova_result["statistic"],
            "p_value": anova_result["p_value"],
            "conclusion": "reject" if anova_result["p_value"] < alpha else "fail_to_reject",
            "effect_size": anova_result["effect_size_eta_squared"],
            "drug_a_mean": anova_result["group_means"][0],
            "drug_b_mean": anova_result["group_means"][1],
            "drug_c_mean": anova_result["group_means"][2],
            "raw_data": anova_payload["groups"],
        },
    }
    output_path = Path(str(payload.get("output_json") or "hypothesis_tests.json"))
    output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"results": results, "json_path": str(output_path.resolve())}


def main() -> int:
    try:
        raw_input = sys.stdin.read()
        try:
            payload = json.loads(raw_input)
        except json.JSONDecodeError:
            payload = _payload_from_task(raw_input)
        if not isinstance(payload, dict):
            raise ValueError("input must be a JSON object")
        test_name = payload.get("test")
        handlers = {
            "t_test": _t_test,
            "chi_square": _chi_square,
            "anova": _anova,
            "suite": _suite,
        }
        if test_name not in handlers:
            raise ValueError("test must be t_test, chi_square, or anova")
        print(json.dumps(handlers[test_name](payload), ensure_ascii=False))
        return 0
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
