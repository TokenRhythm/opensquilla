"""Upgrade contracts for bundled retirement and independently owned Skills."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opensquilla.skills import catalog_policy
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.paths import default_bundled_skills_dir
from opensquilla.skills.types import SkillLayer


def _write_skill(root: Path, name: str, body: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: Synthetic upgrade fixture\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def bundled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "package" / "bundled"
    _write_skill(root, "github", "Current bundled instructions.")
    monkeypatch.setattr(catalog_policy, "packaged_bundled_root", lambda: root)
    return root


@pytest.mark.parametrize("leftover_file", [False, True], ids=["removed", "leftover"])
def test_legacy_snapshot_cannot_republish_retired_bundled_skills(
    bundled: Path,
    tmp_path: Path,
    leftover_file: bool,
) -> None:
    retired = _write_skill(bundled, "memory", "Instructions on disk.")
    cache = tmp_path / "snapshot.json"
    SkillLoader(bundled_dir=bundled, snapshot_path=cache).save_snapshot()
    data = json.loads(cache.read_text(encoding="utf-8"))
    data["version"] = 15
    for row in data["skills"]:
        if row["name"] == "memory":
            row.update(visibility="public", invocation="direct", content="Obsolete cached body.")
    cache.write_text(json.dumps(data), encoding="utf-8")
    if not leftover_file:
        retired.unlink()
        retired.parent.rmdir()

    restarted = SkillLoader(bundled_dir=bundled, snapshot_path=cache)
    assert restarted.load_snapshot() is None
    snapshot = restarted.snapshot_for_turn("upgrade")
    public = catalog_policy.project_public_catalog(
        snapshot.skills, coding_mode=False, include_stable_meta=False
    )
    assert [skill.name for skill in public] == ["github"]
    memory = next((skill for skill in snapshot.skills if skill.name == "memory"), None)
    if leftover_file:
        assert memory is not None
        assert memory.visibility == "internal"
        assert memory.content.strip() == "Instructions on disk."
        assert retired.exists()
    else:
        assert memory is None
    restarted.save_snapshot()
    assert json.loads(cache.read_text(encoding="utf-8"))["version"] == 16


@pytest.mark.parametrize("name", ["cron", "memory", "git-diff", "http-fetch"])
@pytest.mark.parametrize(
    ("directory_arg", "layer"),
    [
        ("personal_agents_dir", SkillLayer.PERSONAL),
        ("project_agents_dir", SkillLayer.PROJECT),
        ("workspace_dir", SkillLayer.WORKSPACE),
        ("extra_dirs", SkillLayer.EXTRA),
    ],
)
def test_retirement_preserves_independently_installed_same_name_skills(
    bundled: Path,
    tmp_path: Path,
    name: str,
    directory_arg: str,
    layer: SkillLayer,
) -> None:
    owned = tmp_path / "owned"
    path = _write_skill(owned, name, "Independent instructions.")
    original = path.read_bytes()
    directories = {directory_arg: [owned] if directory_arg == "extra_dirs" else owned}
    loader = SkillLoader(
        bundled_dir=bundled,
        snapshot_path=tmp_path / "snapshot.json",
        **directories,
    )
    public = catalog_policy.project_public_catalog(
        loader.snapshot_for_turn("upgrade").skills,
        coding_mode=False,
        include_stable_meta=False,
    )
    matching = [skill for skill in public if skill.name == name]
    assert len(matching) == 1
    assert matching[0].layer is layer
    assert Path(matching[0].file_path) == path
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "name",
    [
        "advanced-dubbing-studio", "cron", "git-diff", "html-to-pdf", "http-fetch",
        "latex-compile", "memory", "music-and-singing-studio", "nano-pdf",
        "paper-abstract-author", "paper-citation-planner", "paper-experiment-stub",
        "paper-outline-author", "paper-plot-stub", "paper-preference-planner",
        "paper-revision-author", "paper-source-curator", "skill-creator-linter",
        "skill-creator-proposals", "skill-creator-smoke-test", "stack-trace-generic-probe",
        "stack-trace-go-probe", "stack-trace-js-probe", "stack-trace-python-probe",
        "stack-trace-rust-probe", "summarize", "tmux", "voice-clone-lab",
        "voice-conversion-studio", "voiceover-studio", "weather",
    ],
)
def test_retired_wrappers_are_not_shipped(name: str) -> None:
    assert not (default_bundled_skills_dir() / name).exists()
