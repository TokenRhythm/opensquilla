"""Backwards-compat shim for opensquilla.skills.filter.

Two exports:

- build_skills_prompt: long-term export, used by the
  safety/injection_guard rendering path. NOT deprecated.
- SkillFilter: deprecated alias for HybridRetriever; warns on
  instantiation only.

The relevance-filtering logic itself lives in opensquilla.skills.retrieval.
This module is kept to preserve existing import sites (engine,
test_safety, test_skills/test_filter) and will be slimmed in the next
minor release.
"""

from __future__ import annotations

import warnings

from opensquilla.safety.injection_guard import xml_escape
from opensquilla.skills.retrieval import HybridRetriever
from opensquilla.skills.types import SkillSpec


def build_skills_prompt(
    skills: list[SkillSpec],
    *,
    include_location: bool = False,
) -> str:
    """Assemble the ``<available_skills>`` XML block.

    All skill-provided strings (``name``, ``description``, ``path``) are
    routed through :func:`opensquilla.safety.injection_guard.xml_escape` before
    being concatenated. This is the third injection-guard surface (after
    user messages and tool output) — skill
    metadata must not be able to break out of the ``<available_skills>``
    envelope via ``</available_skills>``, sibling ``<system>`` tags,
    ``<untrusted>`` fragments, or CDATA sequences.
    """
    if not skills:
        return ""
    lines = ["<available_skills>"]
    for skill in skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{xml_escape(skill.name)}</name>")
        lines.append(f"    <description>{xml_escape(skill.description)}</description>")
        if include_location and skill.path:
            lines.append(f"    <location>{xml_escape(str(skill.path))}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


# SkillFilter is a deprecated alias. The warning fires on
# instantiation only — not on module import or class attribute
# access — so callers that only need build_skills_prompt are
# unaffected.
class SkillFilter(HybridRetriever):
    """Deprecated. Use ``opensquilla.skills.retrieval.HybridRetriever``."""

    def __init__(self, *args, **kwargs) -> None:
        warnings.warn(
            "opensquilla.skills.filter.SkillFilter is deprecated; "
            "use opensquilla.skills.retrieval.HybridRetriever",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(*args, **kwargs)

    def filter(self, skills, query, top_k=5):
        """Legacy method name — forwards to HybridRetriever.retrieve."""
        return self.retrieve(skills, query, top_k=top_k)
