"""Stage 3 of deep-research: synthesize the report from a completed plan."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from plan import Plan  # type: ignore[import-not-found]  # noqa: E402


def render(plan: Plan) -> str:
    lines: list[str] = []
    lines.append(f"# Research report — {plan.question}\n")
    lines.append("## Executive summary\n")
    lines.append(
        f"Investigation depth: **{plan.depth}**. "
        f"Sub-questions: {len(plan.subquestions)}. "
        f"Rounds: {plan.rounds}. "
        f"Overall coverage: {plan.overall_coverage():.0%}.\n"
    )

    lines.append("## Methodology\n")
    lines.append(
        f"This report was assembled across {plan.rounds} research rounds. "
        f"Each sub-question was investigated until reaching its target source "
        f"count or the iteration budget was exhausted. Source assessments "
        f"applied a five-axis filter: authority, recency, evidence, bias, and "
        f"corroboration. See `references/sources.md` for the rubric.\n"
    )

    citation_index = 0
    references: list[tuple[int, str, str, str]] = []

    lines.append("## Findings\n")
    for sq in plan.subquestions:
        question_text = sq.question or f"(sub-question {sq.id})"
        lines.append(f"### {question_text}\n")
        if not sq.sources:
            lines.append("_No sources collected — gap noted in coverage._\n")
            continue
        for src in sq.sources:
            citation_index += 1
            references.append((citation_index, src.url, src.title, src.fetched_at))
            excerpt = (src.excerpt or "").strip()
            if excerpt:
                lines.append(f"- {excerpt} [^{citation_index}]\n")
            else:
                lines.append(f"- {src.title or src.url} [^{citation_index}]\n")

    gaps = [sq for sq in plan.subquestions if sq.coverage() < 1.0]
    lines.append("## What this report does not cover\n")
    if not gaps:
        lines.append("All sub-questions reached target coverage.\n")
    else:
        for sq in gaps:
            lines.append(
                f"- **{sq.question or sq.id}** "
                f"({len(sq.sources)}/{sq.target_sources} sources collected)\n"
            )

    lines.append("## References\n")
    for idx, url, title, fetched in references:
        title_part = f" — {title}" if title else ""
        date_part = f" (fetched {fetched})" if fetched else ""
        lines.append(f"[^{idx}]: <{url}>{title_part}{date_part}\n")

    return "".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 3 of deep-research.")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.plan.is_file():
        print(f"error: plan {args.plan} not found", file=sys.stderr)
        return 2
    plan = Plan.model_validate_json(args.plan.read_text(encoding="utf-8"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(plan), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
