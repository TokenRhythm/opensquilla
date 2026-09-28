"""Injects active skill content into system prompts — full/compact modes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from opensquilla.skills.types import SkillSpec

DEFAULT_DESCRIPTION_LIMIT = 240


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


def _skill_source(skill: SkillSpec, generation: int) -> str:
    return f"skill://{skill.layer.value}/{skill.name}#g{generation}"




@dataclass(frozen=True)
class SkillRenderReport:
    total: int = 0
    rendered: int = 0
    omitted: int = 0


class SkillInjector:
    """Injects skill content into system prompts with budget control."""

    def __init__(self) -> None:
        self.last_render_report = SkillRenderReport()

    # ── shared rendering primitives ──────────────────────────────────────────

    def _header_lines(self, *, full: bool) -> list[str]:
        if full:
            lines = [
                "\n\n## Skills",
                "Skills are optional task playbooks. Use them only when a listed entry "
                "clearly matches the user's current request.",
                "Skill names are identifiers for `skill_view`; they are not callable tools.",
                "Review <available_skills> before answering.",
                'When one entry is clearly relevant, call skill_view(name="SKILL_NAME") '
                "to load that skill's instructions, then use only the tools available "
                "in this session.",
            ]
            lines.append("When no entry is relevant, answer without loading a skill.")
        else:
            lines = [
                "\n\n## Skills",
                'Call skill_view(name="SKILL_NAME") only for a matching listed entry.',
            ]
        lines.append("")
        return lines

    def _entry_lines(
        self,
        skill: SkillSpec,
        *,
        with_desc: bool,
        desc_limit: int,
        with_location: bool,
        generation: int = 0,
    ) -> list[str]:
        kind = _escape_xml(getattr(skill, "kind", "skill"))
        lines = [f'  <skill kind="{kind}">', f"    <name>{_escape_xml(skill.name)}</name>"]
        lines.append(f"    <source>{_escape_xml(_skill_source(skill, generation))}</source>")
        if with_desc:
            description = _truncate_text(skill.description, desc_limit)
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
        generation: int = 0,
        omitted_count: int = 0,
    ) -> str:
        visible = [s for s in skills if s.kind == "skill" and not s.disable_model_invocation]
        if not visible:
            return system_prompt
        any_desc = any(with_desc(s) for s in visible)
        lines = self._header_lines(full=any_desc)
        lines.append("<available_skills>")
        for s in visible:
            lines.extend(
                self._entry_lines(
                    s,
                    with_desc=with_desc(s),
                    desc_limit=desc_limit,
                    with_location=with_location,
                    generation=generation,
                )
            )
        if omitted_count:
            lines.append(f'  <omitted count="{omitted_count}" reason="metadata_budget" />')
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
        generation: int = 0,
    ) -> str:
        """Full mode: name + description, without host paths by default.

        ``include_location=True`` remains an explicit compatibility/debug
        opt-in. Production prompt assembly must keep the default so absolute
        host paths are disclosed only when ``skill_view`` loads the body.
        """
        rendered = self._render(
            system_prompt,
            skills,
            with_desc=lambda _s: True,
            desc_limit=desc_limit,
            with_location=include_location,
            generation=generation,
        )
        count = rendered.count("</name>")
        self.last_render_report = SkillRenderReport(len(skills), count, len(skills) - count)
        return rendered

    def inject_compact(
        self,
        system_prompt: str,
        skills: list[SkillSpec],
        *,
        include_location: bool = False,
        generation: int = 0,
    ) -> str:
        """Compact name-only mode, without host paths by default."""
        rendered = self._render(
            system_prompt,
            skills,
            with_desc=lambda _s: False,
            desc_limit=0,
            with_location=include_location,
            generation=generation,
        )
        count = rendered.count("</name>")
        self.last_render_report = SkillRenderReport(len(skills), count, len(skills) - count)
        return rendered

    def inject_skills(
        self,
        system_prompt: str,
        skills: list[SkillSpec],
        max_chars: int = 30_000,
        *,
        desc_limit: int = DEFAULT_DESCRIPTION_LIMIT,
        pinned_count: int = 0,
        generation: int = 0,
    ) -> str:
        """Fit descriptions, then names, into a hard metadata budget.

        Pinned skills remain first when the name-only catalog must be shortened.
        """
        if not skills:
            self.last_render_report = SkillRenderReport()
            return system_prompt
        visible = [s for s in skills if s.kind == "skill" and not s.disable_model_invocation]
        if not visible:
            self.last_render_report = SkillRenderReport(total=len(skills))
            return system_prompt

        def finish(rendered: str) -> str:
            count = rendered.count("</name>")
            self.last_render_report = SkillRenderReport(
                total=len(visible),
                rendered=count,
                omitted=max(len(visible) - count, 0),
            )
            return rendered

        def fits(rendered: str) -> bool:
            return len(rendered) - len(system_prompt) <= max_chars

        # A. everyone described.
        full = self._render(
            system_prompt,
            visible,
            with_desc=lambda _s: True,
            desc_limit=desc_limit,
            with_location=False,
            generation=generation,
        )
        if fits(full):
            return finish(full)

        # A1. Divide the remaining description budget evenly instead of
        # allowing early entries to crowd out the tail.
        lo_limit, hi_limit, best_fair = 16, max(desc_limit, 16), None
        while lo_limit <= hi_limit:
            mid = (lo_limit + hi_limit) // 2
            candidate = self._render(
                system_prompt,
                visible,
                with_desc=lambda _s: True,
                desc_limit=mid,
                with_location=False,
                generation=generation,
            )
            if fits(candidate):
                best_fair = candidate
                lo_limit = mid + 1
            else:
                hi_limit = mid - 1
        if best_fair is not None:
            return finish(best_fair)

        # Fall back to name-only discovery.
        names_only = self._render(
            system_prompt,
            visible,
            with_desc=lambda _s: False,
            desc_limit=desc_limit,
            with_location=False,
            generation=generation,
        )
        if fits(names_only):
            return finish(names_only)

        # Emit the largest name-only prefix fitting the remaining budget.
        priority = list(range(min(max(pinned_count, 0), len(visible))))
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
                generation=generation,
                omitted_count=len(ordered) - mid,
            )
            if fits(test):
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return finish(
            self._render(
                system_prompt,
                ordered[:best],
                with_desc=lambda _s: False,
                desc_limit=desc_limit,
                with_location=False,
                generation=generation,
                omitted_count=len(ordered) - best,
            )
        )
