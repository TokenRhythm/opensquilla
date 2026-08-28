"""Stage 1 of deep-research: scope a question into a research plan."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

DEPTHS: dict[str, tuple[int, int, int]] = {
    # depth → (min_subquestions, max_subquestions, target_sources_per_sq)
    "overview": (3, 5, 1),
    "thorough": (6, 10, 3),
    "exhaustive": (12, 20, 5),
}


class Source(BaseModel):
    url: str
    title: str = ""
    excerpt: str = ""
    relevance: float = 0.0
    fetched_at: str = ""


class SubQuestion(BaseModel):
    id: str
    question: str
    target_sources: int = 1
    sources: list[Source] = Field(default_factory=list)

    def coverage(self) -> float:
        if self.target_sources <= 0:
            return 1.0
        return min(1.0, len(self.sources) / float(self.target_sources))


class Plan(BaseModel):
    question: str
    depth: Literal["overview", "thorough", "exhaustive"]
    created_at: str
    subquestions: list[SubQuestion] = Field(default_factory=list)
    rounds: int = 0
    done: bool = False

    def overall_coverage(self) -> float:
        if not self.subquestions:
            return 0.0
        return sum(s.coverage() for s in self.subquestions) / len(self.subquestions)


def make_subquestions(question: str, depth: str) -> list[SubQuestion]:
    """Build subquestion stubs.

    The actual decomposition is performed by the host LLM after this skill
    runs — `plan.py` produces stub IDs and the host fills in `question`
    text. The script here keeps things deterministic and machine-checkable;
    creativity belongs in the host.
    """
    min_n, max_n, target = DEPTHS[depth]
    return [
        SubQuestion(id=f"sq-{idx + 1:03d}", question="", target_sources=target)
        for idx in range(min_n)
    ]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 1 of deep-research.")
    parser.add_argument("--question", required=True)
    parser.add_argument("--depth", choices=list(DEPTHS), default="thorough")
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    plan = Plan(
        question=args.question,
        depth=args.depth,
        created_at=datetime.now(UTC).isoformat(),
        subquestions=make_subquestions(args.question, args.depth),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    sys.stdout.write(
        json.dumps(
            {
                "plan_path": str(args.out),
                "subquestions": len(plan.subquestions),
                "depth": plan.depth,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
