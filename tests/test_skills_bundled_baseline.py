from __future__ import annotations

from collections import Counter
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
    assert len(skills) == 9
    assert Counter(skill.visibility.value for skill in skills) == {
        "public": 8,
        "internal": 1,
    }
