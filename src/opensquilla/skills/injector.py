"""Injects active skill content into system prompts — full/compact modes."""

from __future__ import annotations

from collections.abc import Callable

from opensquilla.skills.types import SkillSpec

# Soft per-skill description budget for the inline index. Long descriptions are
# truncated to this many characters so one verbose entry can't crowd the whole
# <available_skills> block. 0 (or negative) disables truncation. kind="meta"
# entries are NEVER truncated (see _entry_lines): under auto-trigger the model
# decides whether to meta_invoke straight from this description, so its full
# positive AND negative ("do not use when…") triggers must survive intact.
DEFAULT_DESCRIPTION_LIMIT = 240

# Header preambles, kept as constants so every render path (full / compact /
# budgeted) emits byte-identical guidance and the meta instructions can't drift.
_FULL_META_LINE = (
    'Meta-skills (kind="meta"): When a kind="meta" entry clearly matches '
    "and the task benefits from multi-skill orchestration, prefer "
    '`meta_invoke(name="<name>")` over answering directly. Do not call '
    '`skill_view` for kind="meta" entries; call `meta_invoke` directly '
    "without preamble. The framework drives the multi-step DAG; do NOT "
    "call skill_view for sub-skills inside. On success the meta-skill's "
    "deliverable IS the assistant's reply for this turn — no further "
    "commentary needed."
)
_COMPACT_META_LINE = (
    'For kind="meta" entries: When a kind="meta" entry clearly matches '
    "and the task benefits from multi-skill orchestration, prefer "
    '`meta_invoke(name="<name>")` over answering directly. Do not call '
    '`skill_view` for kind="meta" entries; call `meta_invoke` directly '
    "without preamble. "
    "The framework runs the DAG and the deliverable is the turn reply."
)


def _escape_xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _truncate_text(text: str, limit: int) -> str:
    """Boundary-safe truncation of a RAW (pre-escape) description.

    Truncating the raw string and escaping afterwards guarantees we never sever
    an XML entity (``&amp;`` → ``&am``). We prefer a sentence boundary, fall
    back to a word boundary, and only then hard-cut.
    """
    text = " ".join(text.split())
    if limit <= 0 or len(text) <= limit:
        return text
    floor = max(1, limit // 4)
    # Latest sentence-ending punctuation within the window (ASCII + CJK). A clean
    # sentence end needs no ellipsis, so it may extend to the full limit.
    cut = -1
    for i, ch in enumerate(text[:limit]):
        if ch in ".。!！?？\n":
            cut = i + 1
    if cut >= floor:
        return text[:cut].strip()
    # Word / hard cut adds a one-char ellipsis, so leave room to stay <= limit.
    window = text[: limit - 1]
    space = window.rfind(" ")
    if space >= floor:
        return text[:space].rstrip() + "…"
    return window.rstrip() + "…"


def _skill_location(skill: SkillSpec) -> str:
    if skill.file_path:
        return skill.file_path
    if skill.path is not None:
        return str(skill.path)
    return ""


def _is_meta(skill: SkillSpec) -> bool:
    return getattr(skill, "kind", "skill") == "meta"


class SkillInjector:
    """Injects skill content into system prompts with budget control."""

    # ── shared rendering primitives ──────────────────────────────────────────

    def _header_lines(self, has_meta: bool, *, full: bool) -> list[str]:
        if full:
            lines = [
                "\n\n## Skills",
                "Skills are optional task playbooks. Use them only when a listed entry "
                "clearly matches the user's current request.",
                "Skill names are identifiers for `skill_view`; they are not callable tools.",
                "Review <available_skills> before answering.",
                'When one entry is clearly relevant, call skill_view(name="<skill_name>") '
                "to load that skill's instructions, then use only the tools available "
                "in this session.",
            ]
            if has_meta:
                lines.append(_FULL_META_LINE)
            lines.append("When no entry is relevant, answer without loading a skill.")
        else:
            lines = [
                "\n\nSkills are optional task playbooks for specific request types.",
                "Skill names are identifiers for `skill_view`; they are not callable tools.",
                'Call skill_view(name="<skill_name>") only when the current request '
                "matches a listed entry.",
            ]
            if has_meta:
                lines.append(_COMPACT_META_LINE)
        lines.append("")
        return lines

    def _entry_lines(
        self,
        skill: SkillSpec,
        *,
        with_desc: bool,
        desc_limit: int,
        with_location: bool,
    ) -> list[str]:
        kind = _escape_xml(getattr(skill, "kind", "skill"))
        lines = [f'  <skill kind="{kind}">', f"    <name>{_escape_xml(skill.name)}</name>"]
        if with_desc:
            # Never truncate meta descriptions — they drive the auto-trigger choice.
            limit = 0 if _is_meta(skill) else desc_limit
            description = _truncate_text(skill.description, limit)
            lines.append(f"    <description>{_escape_xml(description)}</description>")
        if with_location:
            location = _skill_location(skill)
            if location:
                lines.append(f"    <location>{_escape_xml(location)}</location>")
        lines.append("  </skill>")
        return lines

    def _render(
        self,
        system_prompt: str,
        skills: list[SkillSpec],
        *,
        with_desc: Callable[[SkillSpec], bool],
        desc_limit: int,
        with_location: bool,
    ) -> str:
        visible = [s for s in skills if not s.disable_model_invocation]
        if not visible:
            return system_prompt
        has_meta = any(_is_meta(s) for s in visible)
        any_desc = any(with_desc(s) for s in visible)
        lines = self._header_lines(has_meta, full=any_desc)
        lines.append("<available_skills>")
        for s in visible:
            lines.extend(
                self._entry_lines(
                    s,
                    with_desc=with_desc(s),
                    desc_limit=desc_limit,
                    with_location=with_location,
                )
            )
        lines.append("</available_skills>")
        return system_prompt + "\n".join(lines)

    # ── public modes ─────────────────────────────────────────────────────────

    def inject_full(
        self,
        system_prompt: str,
        skills: list[SkillSpec],
        *,
        desc_limit: int = 0,
        include_location: bool = False,
    ) -> str:
        """Full mode: name + description, without host paths by default.

        ``include_location=True`` remains an explicit compatibility/debug
        opt-in. Production prompt assembly must keep the default so absolute
        host paths are disclosed only when ``skill_view`` loads the body.
        """
        return self._render(
            system_prompt,
            skills,
            with_desc=lambda _s: True,
            desc_limit=desc_limit,
            with_location=include_location,
        )

    def inject_compact(
        self,
        system_prompt: str,
        skills: list[SkillSpec],
        *,
        include_location: bool = False,
    ) -> str:
        """Compact name-only mode, without host paths by default."""
        return self._render(
            system_prompt,
            skills,
            with_desc=lambda _s: False,
            desc_limit=0,
            with_location=include_location,
        )

    def inject_skills(
        self,
        system_prompt: str,
        skills: list[SkillSpec],
        max_chars: int = 30_000,
        *,
        desc_limit: int = DEFAULT_DESCRIPTION_LIMIT,
        pinned_count: int = 0,
    ) -> str:
        """Fit skills into ``max_chars`` with meta-preserving, graded degradation.

        Degradation order, each step cheaper than the last. kind="meta" entries
        keep their FULL (untruncated) description at every level above C, because
        under auto-trigger the model decides whether to ``meta_invoke`` straight
        from that text — dropping it to a bare name would blind the decision.

          A.   full — every skill described (meta untruncated / others truncated).
          A'.  meta-priority — meta keep full descriptions; non-meta drop to name-only.
          A''. meta-preserving — keep EVERY meta description, trim the non-meta name
               tail so a tight budget still carries the meta text auto-trigger reads.
          B.   name-only — every skill's NAME (only when meta descriptions alone
               overflow the budget, or there are no meta skills).
          C.   name-only, capped — even all names overflow: emit the largest run that
               FITS, pinned + meta reordered to the front so they drop last.

        ``max_chars`` is a HARD ceiling at every level (an overflowing skills block
        is worse than a dropped name), so nothing is ever forced past it. This
        replaces the old binary-prefix search, which under a tight budget silently
        dropped the alphabetical tail (names AND descriptions) and never guaranteed
        meta visibility. Pinned/``always`` skills lead the list. ``<location>`` is
        omitted throughout — skill_view keys on the name, so the path is pure
        overhead here and that budget is better spent on descriptions.
        """
        if not skills:
            return system_prompt
        visible = [s for s in skills if not s.disable_model_invocation]
        if not visible:
            return system_prompt

        def fits(rendered: str) -> bool:
            return len(rendered) - len(system_prompt) <= max_chars

        # A. everyone described.
        full = self._render(
            system_prompt,
            visible,
            with_desc=lambda _s: True,
            desc_limit=desc_limit,
            with_location=False,
        )
        if fits(full):
            return full

        # A'. meta keep full descriptions; everyone else becomes name-only.
        meta_priority = self._render(
            system_prompt, visible, with_desc=_is_meta, desc_limit=desc_limit, with_location=False
        )
        if fits(meta_priority):
            return meta_priority

        # A''. budget too tight for all non-meta names alongside the meta
        # descriptions — keep EVERY meta description and trim the non-meta name
        # tail instead. Priority order is: meta descriptions > all names > non-meta
        # descriptions, so a valid-but-tight budget (e.g. a small-window model's
        # floor) still carries the meta text the auto-trigger decision reads. Meta
        # descriptions are only dropped (→ B) when they alone exceed the budget.
        metas = [s for s in visible if _is_meta(s)]
        if metas:
            nonmetas = [s for s in visible if not _is_meta(s)]
            lo, hi, best = 0, len(nonmetas), 0
            while lo <= hi:
                mid = (lo + hi) // 2
                if fits(
                    self._render(
                        system_prompt,
                        metas + nonmetas[:mid],
                        with_desc=_is_meta,
                        desc_limit=desc_limit,
                        with_location=False,
                    )
                ):
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            meta_kept = self._render(
                system_prompt,
                metas + nonmetas[:best],
                with_desc=_is_meta,
                desc_limit=desc_limit,
                with_location=False,
            )
            if fits(meta_kept):
                return meta_kept

        # B. no descriptions at all (reached only if meta descriptions alone
        # overflow, or there are no meta skills) — every name still listed.
        names_only = self._render(
            system_prompt,
            visible,
            with_desc=lambda _s: False,
            desc_limit=desc_limit,
            with_location=False,
        )
        if fits(names_only):
            return names_only

        # C. budget too small even for all names — emit the largest name-only run
        # that FITS the hard ceiling. Pinned + meta entries are reordered to the
        # front so they are the last to be dropped; nothing is forced past
        # max_chars (best may be 0 → the section is omitted). At realistic budgets
        # A'/B already returned, so this only bites pathologically small budgets.
        priority = list(range(min(max(pinned_count, 0), len(visible))))
        priority += [i for i, s in enumerate(visible) if _is_meta(s) and i not in priority]
        rest = [i for i in range(len(visible)) if i not in priority]
        ordered = [visible[i] for i in (*priority, *rest)]
        lo, hi, best = 1, len(ordered), 0
        while lo <= hi:
            mid = (lo + hi) // 2
            test = self._render(
                system_prompt,
                ordered[:mid],
                with_desc=lambda _s: False,
                desc_limit=desc_limit,
                with_location=False,
            )
            if fits(test):
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return self._render(
            system_prompt,
            ordered[:best],
            with_desc=lambda _s: False,
            desc_limit=desc_limit,
            with_location=False,
        )
