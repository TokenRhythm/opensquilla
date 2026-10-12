"""Lexical (FTS5 + substring fallback) ranking layer for skill retrieval.

Ported from src/opensquilla/skills/filter.py:_fts_search/_substring_search,
but reshaped to emit Hit records keyed by skill_id so that ranks can be
fused with the semantic layer in fusion.rrf.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass

import structlog

from opensquilla.skills.types import SkillSpec

log = structlog.get_logger(__name__)


def _skill_id(skill: SkillSpec) -> str:
    """Resolve the stable id used for fusion. Today every SkillSpec is
    keyed by `name`; the getattr matches the existing convention at
    engine/steps/skills_filter.py:79 in case `id` is added later."""
    return getattr(skill, "id", None) or skill.name


def _skill_cache_fingerprint(skill: SkillSpec) -> str:
    """Return a stable cache key for retrieval-relevant skill metadata.

    Fusion continues to address skills by name, but the indexes must also be
    rebuilt when an existing skill changes the text used for retrieval.
    """
    payload = "\0".join(
        (
            _stringify(skill.name),
            _stringify(skill.description),
            _stringify(skill.triggers),
        )
    ).encode("utf-8")
    return f"{_skill_id(skill)}:{hashlib.sha256(payload).hexdigest()}"


def _stringify(value: object) -> str:
    """Coerce skill metadata into a SQL-bindable string.

    SkillSpec is typed as `str` for name/description, but YAML frontmatter
    parsers can produce lists / dicts when the source uses flow-style
    syntax (e.g. ``description: [TODO: ...]``). Without coercion the
    SQLite binder raises ``Error binding parameter N: type 'list' is not
    supported``, taking the entire lexical path down. Stringify defensively
    so one malformed skill doesn't break retrieval for the whole turn.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(_stringify(v) for v in value)
    if isinstance(value, dict):
        return " ".join(f"{k} {_stringify(v)}" for k, v in value.items())
    return str(value)


@dataclass(frozen=True)
class Hit:
    skill_id: str
    rank: int  # 1-based
    score: float  # normalised to [0,1]; debug/observability only


class LexicalIndex:
    """In-memory FTS5 index over (name, description, triggers) with a
    substring fallback for queries that FTS drops (single-token CJK,
    very short strings)."""

    def __init__(self, skills: list[SkillSpec]) -> None:
        self._skills = list(skills)

    def rank(self, query: str, top_n: int = 20) -> list[Hit]:
        if not self._skills or not query or not query.strip():
            return []

        scored = self._fts_search(query)
        if not scored:
            scored = self._substring_search(query)

        scored.sort(key=lambda x: x[1], reverse=True)
        return [
            Hit(skill_id=_skill_id(s), rank=i + 1, score=score)
            for i, (s, score) in enumerate(scored[:top_n])
        ]

    def _fts_search(self, query: str) -> list[tuple[SkillSpec, float]]:
        try:
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE VIRTUAL TABLE skills_fts USING fts5(name, description, triggers)")
            for i, skill in enumerate(self._skills):
                triggers_text = _stringify(skill.triggers)
                conn.execute(
                    "INSERT INTO skills_fts(rowid, name, description, triggers)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        i,
                        _stringify(skill.name),
                        _stringify(skill.description),
                        triggers_text,
                    ),
                )

            tokens = re.findall(r"\w+", query.lower())
            if not tokens:
                conn.close()
                return []
            fts_query = " OR ".join(tokens)

            rows = conn.execute(
                "SELECT rowid, rank FROM skills_fts WHERE skills_fts MATCH ? ORDER BY rank",
                (fts_query,),
            ).fetchall()
            conn.close()

            # bm25 rank from FTS5 is a negative number (lower = better).
            # Negate and normalise to [0,1] across the result set for debug.
            if not rows:
                return []
            raw = [(self._skills[r[0]], -r[1]) for r in rows if r[0] < len(self._skills)]
            max_score = max(s for _, s in raw) if raw else 1.0
            if max_score <= 0:
                max_score = 1.0
            return [(s, score / max_score) for s, score in raw]
        except Exception as exc:
            log.debug("skills.retrieval.lexical_failed", error=str(exc))
            return []

    def _substring_search(self, query: str) -> list[tuple[SkillSpec, float]]:
        tokens = list(set(query.lower().split()))
        if not tokens:
            return []
        scored: list[tuple[SkillSpec, float]] = []
        for skill in self._skills:
            # Stringify defensively — same reason as _fts_search: a
            # malformed YAML frontmatter elsewhere shouldn't poison the
            # whole substring fallback.
            haystack = (
                f"{_stringify(skill.name)} "
                f"{_stringify(skill.description)} "
                f"{_stringify(skill.triggers)}"
            ).lower()
            hits = sum(1 for t in tokens if t in haystack)
            if hits > 0:
                scored.append((skill, hits / len(tokens)))
        return scored
