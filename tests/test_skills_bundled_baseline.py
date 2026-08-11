from __future__ import annotations

from pathlib import Path

from opensquilla.skills.loader import SkillLoader


def test_bundled_skill_visibility_baseline_is_stable(tmp_path: Path) -> None:
    bundled = Path(__file__).parents[1] / "src" / "opensquilla" / "skills" / "bundled"
    loader = SkillLoader(
        bundled_dir=bundled,
        snapshot_path=tmp_path / "bundled-snapshot.json",
    )

    skills = loader.load_all()

    assert loader.snapshot().errors == ()
    assert len(skills) == 75
    assert sum(skill.user_invocable for skill in skills) == 50
    assert sum(skill.disable_model_invocation for skill in skills) == 25
