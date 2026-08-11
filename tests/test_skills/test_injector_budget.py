"""SkillInjector budget control: per-skill truncation + graceful degradation.

These lock the Step-1 redesign: descriptions are truncated boundary- and
XML-entity-safely, and a tight budget degrades to *every* skill name (recall
preserved) rather than silently dropping the tail of the list.
"""

from __future__ import annotations

from pathlib import Path

from opensquilla.skills.filter import build_skills_prompt
from opensquilla.skills.injector import (
    DEFAULT_DESCRIPTION_LIMIT,
    SkillInjector,
    _truncate_text,
)
from opensquilla.skills.types import SkillLayer, SkillSpec


def _skill(
    name: str, description: str = "", *, always: bool = False, kind: str = "skill"
) -> SkillSpec:
    return SkillSpec(
        name=name,
        description=description or f"{name} description text",
        layer=SkillLayer.BUNDLED,
        always=always,
        triggers=[],
        content="",
        kind=kind,
    )


# ── truncation ───────────────────────────────────────────────────────────────


def test_truncate_leaves_short_text_untouched() -> None:
    text = "Short description that is well under the limit."
    assert _truncate_text(text, 240) == text


def test_truncate_disabled_with_zero_limit() -> None:
    text = "x" * 1000
    assert _truncate_text(text, 0) == text


def test_truncate_prefers_sentence_boundary() -> None:
    text = "First sentence is complete. " + "tail " * 100
    out = _truncate_text(text, 60)
    assert out == "First sentence is complete."


def test_truncate_falls_back_to_word_boundary_with_ellipsis() -> None:
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda"
    out = _truncate_text(text, 20)
    assert out.endswith("…")
    assert " " not in out[-2:]  # did not cut mid-word right before the ellipsis
    assert len(out) <= 21


def test_truncate_is_xml_entity_safe_after_escape() -> None:
    # Raw text is truncated BEFORE escaping, so an ampersand near the cut can
    # never become a dangling entity in the rendered prompt.
    raw = "Use A & B & C & D & E & F & G & H & I & J for a thing " + "z" * 200
    inj = SkillInjector()
    out = inj.inject_full("", [_skill("amp", raw)], desc_limit=30)
    desc = out.split("<description>")[1].split("</description>")[0]
    # No truncated/dangling entity like "&am" — every & starts a full entity.
    for idx, ch in enumerate(desc):
        if ch == "&":
            assert desc[idx:].startswith("&amp;"), desc


def test_inject_full_truncates_long_descriptions() -> None:
    long_desc = "This is a deliberately long description. " + "word " * 200
    inj = SkillInjector()
    out = inj.inject_full("", [_skill("verbose", long_desc)], desc_limit=80)
    desc = out.split("<description>")[1].split("</description>")[0]
    assert len(desc) <= 80
    # Default inject_full (no desc_limit) keeps the description verbatim.
    out_full = inj.inject_full("", [_skill("verbose", long_desc)])
    assert "word word word" in out_full


def test_prompt_renderers_hide_locations_by_default_with_explicit_opt_in() -> None:
    skill = _skill("portable")
    skill.path = Path("/synthetic/host/skills/portable/SKILL.md")
    skill.file_path = str(skill.path)

    injector = SkillInjector()
    assert "<location>" not in injector.inject_full("", [skill])
    assert "<location>" not in injector.inject_compact("", [skill])
    assert "<location>" not in build_skills_prompt([skill])

    expected = f"<location>{skill.path}</location>"
    assert expected in injector.inject_full("", [skill], include_location=True)
    assert expected in injector.inject_compact("", [skill], include_location=True)
    assert expected in build_skills_prompt([skill], include_location=True)


# ── graceful degradation ─────────────────────────────────────────────────────


def _names(prompt: str) -> int:
    return prompt.count("</name>")


def test_generous_budget_renders_full_with_descriptions() -> None:
    skills = [_skill(f"s{i}", f"description number {i}") for i in range(10)]
    out = SkillInjector().inject_skills("", skills, max_chars=100_000)
    assert "<description>" in out
    assert _names(out) == 10


def test_tight_budget_keeps_every_name_dropping_descriptions_and_locations() -> None:
    # Many skills with long descriptions: full overflows, but all NAMES fit once
    # descriptions + locations are dropped. Every skill must still be listed.
    skills = [_skill(f"skill-{i:02d}", "long " * 60) for i in range(30)]
    out = SkillInjector().inject_skills("", skills, max_chars=4000)
    assert "<description>" not in out
    assert "<location>" not in out
    assert _names(out) == 30  # no skill silently dropped


def test_pathological_budget_respects_ceiling_and_prioritizes_pinned() -> None:
    # Budget too small even for all names → emit the largest run that FITS the
    # hard ceiling, with the pinned prefix prioritized (dropped last).
    skills = [_skill(f"skill-name-{i:03d}") for i in range(40)]
    max_chars = 600
    out = SkillInjector().inject_skills("", skills, max_chars=max_chars, pinned_count=3)
    # max_chars is a HARD ceiling — the skills block never exceeds it.
    assert len(out) <= max_chars
    rendered = _names(out)
    assert 0 < rendered < 40  # genuinely trimmed, but not empty
    # The pinned prefix is kept (prioritized), not dropped.
    assert "<name>skill-name-000</name>" in out
    assert "<name>skill-name-002</name>" in out


def test_max_chars_is_a_hard_ceiling_even_with_many_pinned() -> None:
    # Even when pinned_count exceeds what fits, the output must not blow the
    # ceiling: pinned are prioritized but never forced past max_chars.
    skills = [_skill(f"pinned-skill-{i:03d}") for i in range(30)]
    max_chars = 700
    out = SkillInjector().inject_skills("", skills, max_chars=max_chars, pinned_count=30)
    assert len(out) <= max_chars
    assert 0 < _names(out) < 30


def test_default_description_limit_is_applied_by_inject_skills() -> None:
    over = "x" * (DEFAULT_DESCRIPTION_LIMIT + 500)
    out = SkillInjector().inject_skills("", [_skill("one", over)], max_chars=100_000)
    desc = out.split("<description>")[1].split("</description>")[0]
    assert len(desc) <= DEFAULT_DESCRIPTION_LIMIT


# ── meta-skill guarantee (auto-trigger reads the description inline) ──────────


def _meta_desc(prompt: str, name: str) -> str:
    block = prompt.split(f"<name>{name}</name>", 1)[1].split("</skill>", 1)[0]
    if "<description>" not in block:
        return ""
    return block.split("<description>", 1)[1].split("</description>", 1)[0]


def test_meta_keeps_full_untruncated_description_when_nonmeta_degrade() -> None:
    # Budget too tight for everyone's description, but a meta-skill MUST still
    # carry its full text (auto-trigger decides whether to meta_invoke from it).
    meta_desc = (
        "Use this meta-skill when the user asks to draft or compile an academic "
        "paper or LaTeX manuscript. "
        + "Filler clause about orchestration. " * 8
        + "Do not use it for slide decks or generic plotting."
    )
    skills = [_skill("meta-paper-write", meta_desc, kind="meta")]
    skills += [_skill(f"plain-{i:02d}", "plain " * 50) for i in range(20)]
    out = SkillInjector().inject_skills("", skills, max_chars=4000)

    # Every name listed, but only the meta carries a description...
    assert out.count("</name>") == 21
    assert _meta_desc(out, "plain-00") == ""
    # ...and that meta description is the FULL text, negative clause intact.
    rendered = _meta_desc(out, "meta-paper-write")
    assert rendered == meta_desc
    assert "Do not use it for slide decks" in rendered


def test_meta_descriptions_survive_tight_budget_by_trimming_nonmeta_names() -> None:
    # A valid-but-tight budget can't fit meta descriptions + ALL non-meta names.
    # Meta descriptions must still win: drop non-meta NAMES, never the meta text.
    metas = [_skill(f"meta-{i}", "M " * 60, kind="meta") for i in range(3)]
    nonmetas = [_skill(f"plain-{i:02d}") for i in range(40)]
    # 2500 fits the header + the three meta descriptions, but NOT all 40 names.
    out = SkillInjector().inject_skills("", metas + nonmetas, max_chars=2500)

    # Every meta keeps a (full) description...
    for m in metas:
        assert _meta_desc(out, m.name) != "", m.name
    # ...even though some non-meta names had to be dropped to make room.
    assert out.count("</name>") < len(metas) + len(nonmetas)
    assert len(out) <= 2500  # hard ceiling still respected


def test_meta_dropped_to_name_only_only_when_descriptions_alone_overflow() -> None:
    # When the meta descriptions ALONE cannot fit, fall back to names (B/C);
    # the budget ceiling wins over the meta-description guarantee in that case.
    metas = [_skill(f"meta-{i}", "M " * 400, kind="meta") for i in range(3)]
    out = SkillInjector().inject_skills("", metas, max_chars=800)
    assert len(out) <= 800
    assert _meta_desc(out, "meta-0") == ""  # degraded to name-only


def test_budget_gradient_meta_then_all_descriptions() -> None:
    skills = [_skill("meta-x", "M " * 80, kind="meta")]
    skills += [_skill(f"s{i:02d}", "S " * 80) for i in range(15)]
    inj = SkillInjector()

    tight = inj.inject_skills("", skills, max_chars=2500)
    roomy = inj.inject_skills("", skills, max_chars=100_000)

    # Tight: meta described, non-meta name-only.
    assert _meta_desc(tight, "meta-x") != ""
    assert _meta_desc(tight, "s00") == ""
    # Roomy: everyone described.
    assert _meta_desc(roomy, "s00") != ""
