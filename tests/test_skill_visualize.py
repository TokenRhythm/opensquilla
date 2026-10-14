from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

import pytest

from opensquilla.skills.loader import SkillLoader
from opensquilla.tools.builtin import skill_tools
from opensquilla.tools.registry import get_default_registry

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "opensquilla" / "skills" / "bundled"


def _references(body: str) -> set[str]:
    return set(re.findall(r"\]\((references/[^)]+)\)", body))


def _check_provenance_and_resources(bundled: Path) -> None:
    skill = bundled / "visualize"
    manifest = json.loads((skill / "upstream-guidance.json").read_text(encoding="utf-8"))
    assert manifest["upstream"]["package"] == "generative-frame"
    assert manifest["upstream"]["version"] == "0.0.5"
    assert manifest["upstream"]["license"] == "MIT"
    assert set(manifest["modules"]) == {
        "diagram",
        "chart",
        "data_viz",
        "interactive",
        "mockup",
        "elicitation",
        "art",
    }
    generated = {record["file"] for record in manifest["outputs"]}
    expected = {
        "LICENSE.generative-frame.txt",
        "references/guidance-base.txt",
        *(f"references/guidance-{name}.txt" for name in manifest["modules"]),
    }
    assert generated == expected
    for record in manifest["outputs"]:
        text = (skill / record["file"]).read_text(encoding="utf-8")
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == record["sha256TextLF"]
    body = (skill / "SKILL.md").read_text(encoding="utf-8")
    assert generated - {"LICENSE.generative-frame.txt"} <= _references(body)
    assert "Apache-2.0 AND MIT" in body
    assert "Copyright (c) 2026 AgentbaseAI Inc." in (
        skill / "LICENSE.generative-frame.txt"
    ).read_text(encoding="utf-8")


async def _check_skill_resources(bundled: Path, tmp_path: Path) -> None:
    _check_provenance_and_resources(bundled)
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=tmp_path / "snapshot.json")
    previous = skill_tools._loader
    skill_tools.create_skill_tools(loader)
    try:
        view = get_default_registry().get("skill_view")
        assert view is not None
        body = await view.handler(name="visualize")
        references = _references(body)
        source_body = (BUNDLED / "visualize/SKILL.md").read_text(encoding="utf-8")
        assert references == _references(source_body)
        for reference in references:
            content = await view.handler(name="visualize", file_path=reference)
            expected = (BUNDLED / "visualize" / reference).read_text(encoding="utf-8")
            assert content == expected
    finally:
        skill_tools._loader = previous


async def test_visualize_examples_are_available_through_skill_view(tmp_path: Path) -> None:
    await _check_skill_resources(BUNDLED, tmp_path)


@pytest.mark.usefixtures("isolated_core_wheel")
async def test_wheel_preserves_readable_visualize_examples(
    isolated_core_wheel: Path,
    tmp_path: Path,
) -> None:
    prefix = "opensquilla/skills/bundled/visualize/"
    with zipfile.ZipFile(isolated_core_wheel) as archive:
        required = {"SKILL.md", "upstream-guidance.json", "LICENSE.generative-frame.txt"}
        required.update(_references((BUNDLED / "visualize/SKILL.md").read_text(encoding="utf-8")))
        assert {f"{prefix}{name}" for name in required} <= set(archive.namelist())
        for name in archive.namelist():
            if name.startswith(prefix) and not name.endswith("/"):
                relative = Path(name).relative_to("opensquilla/skills/bundled")
                destination = tmp_path / "bundled" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(name))
    await _check_skill_resources(tmp_path / "bundled", tmp_path)
