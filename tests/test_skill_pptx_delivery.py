"""pptx skill delivery contract."""

from __future__ import annotations

from pathlib import Path

from opensquilla.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "opensquilla" / "skills" / "bundled"
PPTXGENJS_REFERENCE = BUNDLED / "pptx" / "references" / "pptxgenjs.md"


def test_pptx_skill_instructs_artifact_delivery() -> None:
    spec = SkillLoader(bundled_dir=BUNDLED).get_by_name("pptx")

    assert spec is not None
    content = "\n".join(spec.content.splitlines())
    assert "publish_artifact" in content
    assert "file-authoring tools" in content
    assert "If none of those file-authoring tools are available" in content
    assert "If only `create_pptx` is available" in content
    assert "basic text-only deck" in content
    assert "Do not attempt to generate, save, or modify the `.pptx`" in content
    assert "Ignore the Path B, Path C, and Visual QA sections below" in content
    assert "Do not paste OOXML" in content
    assert "final `.pptx`" in content
    assert "Emoji, colored boxes,\n  and decorative lines do not satisfy" in content
    assert "Visual QA (when render tools are available)" in content
    assert "Do not pass `--range` for final QA" in content
    assert "If inspection\nfinds a defect" in content
    assert "without inventing an\nunnecessary edit" in content
    assert "A B1 text-only\nedit may still be published" in content
    assert "do not use a global npm\n  install" in content
    assert "npm install -g pptxgenjs" not in content
    assert "required before publishing paths B and C" not in content
    assert "at least one fix-and-rerender cycle" not in content
    assert "Do not declare clean unless one fix-and-reverify cycle" not in content
    assert "/tmp/opensquilla-pptxgenjs" not in content
    assert "mkdir -p" not in content

    reference = PPTXGENJS_REFERENCE.read_text(encoding="utf-8")
    assert 'tempfile.mkdtemp(prefix="opensquilla-pptxgenjs-")' in reference
    assert "working directory" in reference
    assert "Do not use `npm install -g`" in reference
    assert "npm install -g pptxgenjs" not in reference
    assert "/tmp/" not in reference
    assert "mkdir -p" not in reference
