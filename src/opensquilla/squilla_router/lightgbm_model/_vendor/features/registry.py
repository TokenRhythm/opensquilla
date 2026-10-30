"""The immutable ``numeric88.v1`` feature registry.

The registry is executable metadata: extraction tests use it to verify that
names, offsets, defaults, and block boundaries cannot drift independently.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path


SCHEMA_VERSION = "numeric88.registry.v1"
FEATURE_VERSION = "numeric88.v1"
DTYPE = "float32"


@dataclass(frozen=True, slots=True)
class NumericFeature:
    name: str
    offset: int
    dtype: str
    missing: str
    default: float
    source: str
    block: str


RULE_NAMES = (
    "char_count",
    "word_count",
    "line_count",
    "avg_chars_per_line",
    "zh_char_ratio",
    "latin_char_ratio",
    "code_char_ratio",
    "mixed_zh_en",
    "has_code_block",
    "code_block_count",
    "code_block_chars",
    "has_json",
    "has_yaml",
    "has_csv",
    "has_table",
    "question_mark_count",
    "exclamation_mark_count",
    "bullet_count",
    "numbered_list_count",
    "debug_keyword_count",
    "research_keyword_count",
    "architecture_keyword_count",
    "compare_keyword_count",
    "planning_keyword_count",
    "strict_format_keyword_count",
    "high_risk_keyword_count",
    "production_keyword_count",
    "customer_keyword_count",
    "delete_keyword_count",
    "formal_keyword_count",
    "has_file_path",
    "has_url",
    "has_log",
    "has_shell",
    "has_traceback",
    "constraint_keyword_count",
    "quoted_text_ratio",
    "word_diversity",
    "teaching_keyword_count",
    "implementation_keyword_count",
    "file_count_zero",
    "file_count_one_or_two",
    "file_count_three_plus",
    "code_without_debug",
    "length_under_200",
    "length_200_to_1000",
    "length_over_1000",
    "low_keyword_density",
)

ROUTE_HISTORY_NAMES = (
    "previous_tier_index",
    "previous_difficulty",
    "previous_margin",
    "highest_tier_index",
    "turn_index",
    "route_history_length",
    "dominant_tier_index",
    "route_switch_count",
    "trajectory_cold_start",
    "trajectory_stable_low",
    "trajectory_stable_high",
    "trajectory_escalating",
    "trajectory_descalating",
    "trajectory_oscillating",
    "trajectory_unclear",
    "trajectory_mixed",
)

PREVIOUS_SIGNAL_NAMES = (
    "has_previous_answer",
    "previous_answer_clarifies",
    "previous_answer_refuses",
    "previous_answer_self_doubt",
    "previous_answer_has_code",
    "previous_answer_has_steps",
    "previous_output_tokens_log",
    "previous_reasoning_tokens_log",
    "previous_duration_log",
    "answer_to_request_length_ratio",
    "previous_answer_zh_ratio",
    "previous_cache_ratio",
    "short_continue_cue",
    "previous_output_tokens_log_repeat",
    "reasoning_cue",
    "question_density",
    "request_length_log",
    "previous_reasoning_tokens_log_repeat",
    "previous_duration_log_repeat",
)

CONTEXT_NAMES = (
    "turn_index_normalized",
    "context_tokens_normalized",
    "has_code_block",
    "is_deep_conversation",
    "is_heavy_context",
)


def _make_registry() -> tuple[NumericFeature, ...]:
    features: list[NumericFeature] = []

    def add_block(
        block: str,
        names: tuple[str, ...],
        source: str,
        *,
        defaults: dict[str, float] | None = None,
        missing: str = "zero",
    ) -> None:
        block_defaults = defaults or {}
        for short_name in names:
            features.append(
                NumericFeature(
                    name=f"{block}.{short_name}",
                    offset=len(features),
                    dtype=DTYPE,
                    missing=missing,
                    default=float(block_defaults.get(short_name, 0.0)),
                    source=source,
                    block=block,
                )
            )

    add_block("current.rule", RULE_NAMES, "input.current_request")
    add_block(
        "history.route",
        ROUTE_HISTORY_NAMES,
        "input.route_history",
        defaults={
            "previous_tier_index": -1.0,
            "highest_tier_index": -1.0,
            "dominant_tier_index": -1.0,
            "trajectory_cold_start": 1.0,
        },
        missing="documented_default",
    )
    add_block(
        "previous.signal",
        PREVIOUS_SIGNAL_NAMES,
        "input.previous_answer + input.previous_usage + input.current_request",
    )
    add_block(
        "context",
        CONTEXT_NAMES,
        "input.current_request + input.history_user + input.previous_answer + input.route_history + input.context",
    )
    if len(features) != 88:
        raise AssertionError(f"numeric88 registry has {len(features)} entries")
    return tuple(features)


NUMERIC88_REGISTRY = _make_registry()


def numeric88_contract() -> dict[str, object]:
    blocks = []
    for name, offset, dimension in (
        ("current.rule", 0, 48),
        ("history.route", 48, 16),
        ("previous.signal", 64, 19),
        ("context", 83, 5),
    ):
        blocks.append({"name": name, "offset": offset, "dimension": dimension})
    return {
        "schema_version": SCHEMA_VERSION,
        "feature_version": FEATURE_VERSION,
        "dimension": 88,
        "dtype": DTYPE,
        "blocks": blocks,
        "features": [asdict(feature) for feature in NUMERIC88_REGISTRY],
    }


def write_contract(path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(numeric88_contract(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_contract(args.output)


if __name__ == "__main__":
    main()
