from __future__ import annotations

import json
from pathlib import Path

from opensquilla.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "opensquilla" / "skills" / "bundled"
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
ORIGINALS = {
    "advanced-dubbing-studio",
    "cron",
    "AwesomeWebpageMetaSkill",
    "awesome-webpage-image-download",
    "awesome-webpage-research",
    "code-task",
    "deep-research",
    "swe-bench",
    "docx",
    "git-diff",
    "github",
    "history-explorer",
    "html-to-pdf",
    "http-fetch",
    "latex-compile",
    "memory",
    "meta-kid-project-planner",
    "meta-paper-write",
    "meta-short-drama",
    "meta-skill-creator",
    "multi-search-engine",
    "music-and-singing-studio",
    "nano-pdf",
    "openrouter-video-generator",
    "paper-abstract-author",
    "paper-artifact-runtime",
    "paper-citation-integrity-gate",
    "paper-citation-planner",
    "paper-delivery-summary",
    "paper-experiment-stub",
    "paper-latex-sanitizer",
    "paper-length-gate",
    "paper-outline-author",
    "paper-plot-stub",
    "paper-preference-planner",
    "paper-quality-gate",
    "paper-refbib-stub",
    "paper-revision-author",
    "paper-section-author",
    "paper-source-readiness-gate",
    "paper-source-curator",
    "pdf-toolkit",
    "pptx",
    "skill-creator",
    "skill-creator-linter",
    "skill-creator-proposals",
    "skill-creator-smoke-test",
    "short-drama-delivery-audit",
    "short-drama-review-normalizer",
    "stack-trace-generic-probe",
    "stack-trace-go-probe",
    "stack-trace-js-probe",
    "stack-trace-python-probe",
    "stack-trace-rust-probe",
    "sub-agent",
    "srt-from-script",
    "subtitle-burner",
    "summarize",
    "text-file-read",
    "title-card-image",
    "tmux",
    "video-still-animator",
    "voice-clone-lab",
    "voice-conversion-studio",
    "voiceover-studio",
    "weather",
    "xlsx",
}


def test_all_bundled_skills_have_complete_provenance(tmp_path: Path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    skills = sorted(loader.load_all(), key=lambda skill: skill.name)
    skill_dirs = [
        path for path in BUNDLED.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()
    ]

    assert len(skills) == len(skill_dirs)
    for skill in skills:
        provenance = skill.provenance
        assert provenance.origin in {
            "opensquilla-original",
            "bundled-derived",
            "openclaw-derived",
            "clawhub-mit",
            "clawhub-mit0",
        }, skill.name
        assert provenance.maintained_by == "OpenSquilla", skill.name
        if provenance.origin == "bundled-derived":
            assert provenance.upstream_url == "https://github.com/bundled/bundled"
            assert provenance.license == "MIT", skill.name
        elif provenance.origin == "openclaw-derived":
            assert provenance.upstream_url == "https://github.com/openclaw/openclaw", skill.name
            assert provenance.license == "MIT", skill.name
        elif provenance.origin == "clawhub-mit0":
            assert provenance.upstream_url.startswith("https://clawhub.ai/"), skill.name
            assert provenance.license == "MIT-0", skill.name
        elif provenance.origin == "clawhub-mit":
            assert provenance.upstream_url.startswith("https://clawhub.ai/"), skill.name
            assert provenance.license == "MIT", skill.name
        else:
            assert skill.name in ORIGINALS
            assert provenance.license == "Apache-2.0", skill.name


def test_third_party_notices_match_bundled_provenance(tmp_path: Path) -> None:
    text = NOTICES.read_text(encoding="utf-8")
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    skills = {skill.name: skill.provenance.origin for skill in loader.load_all()}
    derived = sorted(name for name, origin in skills.items() if origin == "bundled-derived")
    originals = sorted(name for name, origin in skills.items() if origin == "opensquilla-original")
    openclaw_derived = sorted(
        name for name, origin in skills.items() if origin == "openclaw-derived"
    )
    clawhub_mit = sorted(name for name, origin in skills.items() if origin == "clawhub-mit")
    clawhub_derived = sorted(name for name, origin in skills.items() if origin == "clawhub-mit0")

    assert "## OpenClaw-derived bundled skill descriptors" in text
    assert "## OpenSquilla-original bundled skills" in text
    if clawhub_derived:
        assert "## ClawHub-derived bundled skill descriptors" in text
    if clawhub_mit:
        assert "## ClawHub MIT bundled skill descriptors" in text
    for name in derived:
        assert f"- `{name}`" in text
    for name in originals:
        assert f"- `{name}`" in text
    for name in openclaw_derived:
        assert f"- `{name}`" in text
    for name in clawhub_mit:
        assert f"- `{name}`" in text
    for name in clawhub_derived:
        assert f"- `{name}`" in text

    listed = {
        line.strip()[3:-1]
        for line in text.splitlines()
        if line.strip().startswith("- `") and line.strip().endswith("`")
    }
    assert listed == set(skills)

    if "filesystem" in clawhub_mit:
        assert "Copyright (c) 2026 Clawdbot Community" in text
        assert "clawdbot-filesystem" in text


def test_filesystem_skill_records_mit_notice_provenance(tmp_path: Path) -> None:
    text = NOTICES.read_text(encoding="utf-8")
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    filesystem = loader.get_by_name("filesystem")

    assert filesystem is not None
    assert filesystem.provenance.origin == "clawhub-mit"
    assert filesystem.provenance.license == "MIT"
    assert "## ClawHub MIT bundled skill descriptors" in text
    assert "- `filesystem`" in text
    assert "Copyright (c) 2026 Clawdbot Community" in text


def test_frontend_static_assets_are_covered_by_third_party_notices() -> None:
    text = NOTICES.read_text(encoding="utf-8")

    for expected in [
        "## Web UI dependencies and bundled fonts",
        "Vue.js",
        "Pinia",
        "Vue Router",
        "Vue I18n",
        "html-to-image",
        "KaTeX",
        "highlight.js",
        "marked",
        "Copyright (c) 2004, John Gruber",
        "DOMPurify",
        "IBM Plex Sans",
        "IBM Plex Mono",
        "Space Grotesk",
        "Fraunces",
        "Newsreader",
        "SIL OPEN FONT LICENSE Version 1.1",
        "## npm and Python dependency packaging strategy",
    ]:
        assert expected in text

    package = json.loads(
        (ROOT / "opensquilla-webui" / "package.json").read_text(encoding="utf-8")
    )
    bundled_direct_dependencies = {
        name for name in package["dependencies"] if not name.startswith("@types/")
    }
    for dependency in bundled_direct_dependencies:
        assert f"`{dependency}`" in text

    for removed in [
        "PrismJS",
        "src/opensquilla/gateway/static/vendor/",
        "src/opensquilla/gateway/static/fonts/",
        "Inter-Variable.woff2",
        "JetBrainsMono-Variable.woff2",
    ]:
        assert removed not in text

    for path in [
        ROOT / "opensquilla-webui" / "package.json",
        ROOT / "opensquilla-webui" / "package-lock.json",
        ROOT / "opensquilla-webui" / "src" / "composables" / "chat" / "useChatTextRendering.ts",
        ROOT / "opensquilla-webui" / "src" / "assets" / "fonts" / "ibm-plex-sans-400.woff2",
        ROOT / "opensquilla-webui" / "src" / "assets" / "fonts" / "space-grotesk-400.woff2",
        ROOT
        / "opensquilla-webui"
        / "src"
        / "themes"
        / "out-of-register"
        / "fonts"
        / "fraunces-400.woff2",
        ROOT
        / "opensquilla-webui"
        / "src"
        / "themes"
        / "out-of-register"
        / "fonts"
        / "newsreader-400.woff2",
    ]:
        assert path.is_file(), path


def test_tokenjuice_backend_has_third_party_provenance() -> None:
    text = NOTICES.read_text(encoding="utf-8")
    package_dir = ROOT / "src" / "opensquilla" / "plugins" / "tokenjuice"
    provenance = package_dir / "PROVENANCE.md"
    license_file = package_dir / "LICENSE.tokenjuice"

    assert provenance.is_file()
    assert license_file.is_file()

    provenance_text = provenance.read_text(encoding="utf-8")
    license_text = license_file.read_text(encoding="utf-8")

    assert "## tokenjuice adapted reduction rules" in text
    assert "https://github.com/vincentkoc/tokenjuice" in text
    assert "License: MIT" in text
    assert "Copyright (c) 2026 Vincent Koc" in text
    assert "adaptation" in text
    assert "LICENSE.tokenjuice" in text

    assert "https://github.com/vincentkoc/tokenjuice" in provenance_text
    assert "bundled JSON reduction rules are derived" in provenance_text
    assert "MIT License" in license_text
    assert "Copyright (c) 2026 Vincent Koc" in license_text
