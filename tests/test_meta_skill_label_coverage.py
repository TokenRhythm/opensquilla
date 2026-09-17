"""Bundled meta-skill UX metadata coverage."""

from pathlib import Path

import pytest
import yaml

BUNDLED_META_SKILLS = [
    "meta-paper-write",
    "meta-short-drama",
    "meta-skill-creator",
]


def _extract_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), f"{path}: missing YAML frontmatter"
    end = text.index("\n---", 3)
    return yaml.safe_load(text[3:end])


@pytest.mark.parametrize("name", BUNDLED_META_SKILLS)
def test_bundled_meta_skill_steps_have_labels(name: str) -> None:
    path = Path(f"src/opensquilla/skills/bundled/{name}/SKILL.md")
    fm = _extract_frontmatter(path)
    steps = fm["composition"]["steps"]

    missing = [s["id"] for s in steps if not s.get("label")]

    assert not missing, f"{name}: steps missing label: {missing}"


@pytest.mark.parametrize("name", BUNDLED_META_SKILLS)
def test_bundled_meta_skill_has_request_template(name: str) -> None:
    path = Path(f"src/opensquilla/skills/bundled/{name}/SKILL.md")
    fm = _extract_frontmatter(path)
    template = fm.get("request_template")

    assert isinstance(template, dict), f"{name}: missing request_template"
    assert template.get("outcome"), f"{name}: request_template missing outcome"
    fields = template.get("fields")
    assert isinstance(fields, list) and fields, f"{name}: request_template missing fields"
    field_names = {str(field.get("name", "")) for field in fields if isinstance(field, dict)}
    assert "audience" in field_names, f"{name}: request_template missing audience field"
    assert "language" in field_names, f"{name}: request_template missing language field"


@pytest.mark.parametrize("name", BUNDLED_META_SKILLS)
def test_bundled_meta_skill_has_output_contract(name: str) -> None:
    path = Path(f"src/opensquilla/skills/bundled/{name}/SKILL.md")
    fm = _extract_frontmatter(path)
    contract = fm.get("output_contract")

    assert isinstance(contract, dict), f"{name}: missing output_contract"
    sections = contract.get("required_sections")
    assert isinstance(sections, list) and sections, (
        f"{name}: output_contract missing required_sections"
    )


@pytest.mark.parametrize("name", BUNDLED_META_SKILLS)
def test_bundled_meta_skill_has_eval_baseline(name: str) -> None:
    path = Path(f"src/opensquilla/skills/bundled/{name}/SKILL.md")
    fm = _extract_frontmatter(path)
    eval_prompts = fm.get("eval_prompts")

    assert isinstance(eval_prompts, list) and eval_prompts, (
        f"{name}: missing eval_prompts"
    )
    first = eval_prompts[0]
    assert isinstance(first, dict), f"{name}: eval prompt must be mapping"
    assert first.get("name"), f"{name}: eval prompt missing name"
    assert first.get("prompt"), f"{name}: eval prompt missing prompt"
    assert isinstance(first.get("rubric"), list) and first["rubric"], (
        f"{name}: eval prompt missing rubric"
    )


@pytest.mark.parametrize("name", BUNDLED_META_SKILLS)
def test_bundled_meta_skill_declares_policy_or_preference_metadata(name: str) -> None:
    path = Path(f"src/opensquilla/skills/bundled/{name}/SKILL.md")
    fm = _extract_frontmatter(path)

    assert isinstance(fm.get("preference_keys"), list), (
        f"{name}: missing preference_keys"
    )
    assert isinstance(fm.get("policy_tags"), list) and fm["policy_tags"], (
        f"{name}: missing policy_tags"
    )
