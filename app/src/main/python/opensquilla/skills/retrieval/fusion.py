"""Reciprocal Rank Fusion for skill retrieval ranking lists.

Pure function — no dependency on SkillSpec, only on the Hit dataclass
that lexical/semantic emit.
"""

from __future__ import annotations

from opensquilla.skills.retrieval.lexical import Hit


def rrf(rankings: list[list[Hit]], k: int = 60) -> list[tuple[str, float]]:
    """Fuse multiple ranked Hit lists.

    fused(id) = Σ over rank lists  1 / (k + rank_in_list)

    Skills absent from a list contribute 0 from that list (i.e. the
    sum just skips them).
    """
    scores: dict[str, float] = {}
    for ranked in rankings:
        for hit in ranked:
            scores[hit.skill_id] = scores.get(hit.skill_id, 0.0) + 1.0 / (k + hit.rank)

    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
