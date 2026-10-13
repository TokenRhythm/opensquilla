from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.skills import dependency_summary
from opensquilla.skills.dependency_summary import build_dependency_summary
from opensquilla.skills.eligibility import EligibilityContext, EligibilityReport
from opensquilla.skills.loader import SkillLoader


def _write_skill(
    base: Path,
    name: str,
    skill_md: str,
    scripts: dict[str, str] | None = None,
) -> None:
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if not scripts:
        return
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in scripts.items():
        script_path = scripts_dir / rel_path
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(content, encoding="utf-8")


def test_summary_reports_declared_python_and_missing_env_any(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "declared-skill",
        """---
name: declared-skill
description: Declares a uv package and envAny.
metadata:
  opensquilla:
    requires:
      anyBins: [python]
      envAny: [OPENROUTER_API_KEY, ARK_API_KEY]
    install:
      - id: pillow
        kind: uv
        label: Install Pillow
        package: pillow
---

# body
""",
    )
    loader = SkillLoader(bundled_dir=tmp_path, snapshot_path=tmp_path / "snapshot.json")
    spec = loader.get_by_name("declared-skill")
    assert spec is not None

    summary = build_dependency_summary(
        spec,
        loader=loader,
        ctx=EligibilityContext(
            os_name="linux",
            has_bin_cache={"python": True},
            env_cache={"OPENROUTER_API_KEY": None, "ARK_API_KEY": None},
        ),
    )

    assert summary["declared"]["python_packages"] == [
        {
            "install_id": "pillow",
            "label": "Install Pillow",
            "package": "pillow",
            "module": "",
        }
    ]
    assert summary["declared"]["api_env"]["any"] == [
        "OPENROUTER_API_KEY",
        "ARK_API_KEY",
    ]
    assert summary["missing"]["api_env"]["any"] == [
        ["OPENROUTER_API_KEY", "ARK_API_KEY"]
    ]
    assert summary["missing"]["count"] == 1
    assert summary["declaration_quality"] == "declared"


def test_summary_reports_missing_any_bins_group(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "any-bin-skill",
        """---
name: any-bin-skill
description: Needs one binary from a missing set.
metadata:
  opensquilla:
    requires:
      anyBins: [missing-a, missing-b]
---

# body
""",
    )
    loader = SkillLoader(bundled_dir=tmp_path, snapshot_path=tmp_path / "snapshot.json")
    spec = loader.get_by_name("any-bin-skill")
    assert spec is not None

    summary = build_dependency_summary(
        spec,
        loader=loader,
        ctx=EligibilityContext(
            os_name="linux",
            has_bin_cache={"missing-a": False, "missing-b": False},
        ),
    )

    assert summary["missing"]["binaries"]["any"] == [["missing-a", "missing-b"]]
    assert summary["missing"]["count"] == 1


def test_summary_marks_undeclared_script_imports_as_not_enforced(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "plot-skill",
        """---
name: plot-skill
description: Bare skill for inferred imports.
---

# body
""",
        scripts={
            "plot.py": "import json\nimport matplotlib.pyplot as plt\n",
        },
    )
    loader = SkillLoader(bundled_dir=tmp_path, snapshot_path=tmp_path / "snapshot.json")
    spec = loader.get_by_name("plot-skill")
    assert spec is not None

    summary = build_dependency_summary(spec, loader=loader, ctx=EligibilityContext(os_name="linux"))

    assert summary["inferred"]["python_imports"] == [
        {
            "module": "matplotlib",
            "source": "scripts/plot.py",
            "not_enforced": True,
        }
    ]
    assert summary["declaration_quality"] == "undeclared_inferred"


def test_summary_does_not_flag_project_dependency_imports(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "project-deps-skill",
        """---
name: project-deps-skill
description: Imports packages already provided by the project.
---

# body
""",
        scripts={
            "plot.py": "import httpx\nfrom bs4 import BeautifulSoup\n",
        },
    )
    loader = SkillLoader(bundled_dir=tmp_path, snapshot_path=tmp_path / "snapshot.json")
    spec = loader.get_by_name("project-deps-skill")
    assert spec is not None

    summary = build_dependency_summary(spec, loader=loader, ctx=EligibilityContext(os_name="linux"))

    assert summary["inferred"]["python_imports"] == []


def test_summary_ignores_local_src_and_project_alias_imports(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "local-import-skill",
        """---
name: local-import-skill
description: Imports a local module and a project dependency alias.
---

# body
""",
        scripts={
            "runner.py": "from src.video_merger import VideoMerger\nimport nio\n",
            "src/video_merger.py": "class VideoMerger:\n    pass\n",
        },
    )
    loader = SkillLoader(bundled_dir=tmp_path, snapshot_path=tmp_path / "snapshot.json")
    spec = loader.get_by_name("local-import-skill")
    assert spec is not None

    summary = build_dependency_summary(spec, loader=loader, ctx=EligibilityContext(os_name="linux"))

    assert summary["inferred"]["python_imports"] == []




def test_summary_collects_scan_errors_without_crashing(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "broken-script-skill",
        """---
name: broken-script-skill
description: Has a syntax error in a script.
---

API docs mention EXAMPLE_API_KEY.
""",
        scripts={"broken.py": "def broken(:\n"},
    )
    loader = SkillLoader(bundled_dir=tmp_path, snapshot_path=tmp_path / "snapshot.json")
    spec = loader.get_by_name("broken-script-skill")
    assert spec is not None

    summary = build_dependency_summary(spec, loader=loader, ctx=EligibilityContext(os_name="linux"))

    assert summary["inferred"]["python_imports"] == []
    assert summary["inferred"]["scan_errors"]
    assert "scripts/broken.py" in summary["inferred"]["scan_errors"][0]


def test_summary_uses_precomputed_report_when_provided(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_skill(
        tmp_path,
        "precomputed-report-skill",
        """---
name: precomputed-report-skill
description: Uses a precomputed eligibility report.
metadata:
  opensquilla:
    requires:
      envAny: [OPENROUTER_API_KEY, ARK_API_KEY]
---

# body
""",
    )
    loader = SkillLoader(bundled_dir=tmp_path, snapshot_path=tmp_path / "snapshot.json")
    spec = loader.get_by_name("precomputed-report-skill")
    assert spec is not None

    monkeypatch.setattr(
        dependency_summary,
        "diagnose_eligibility",
        lambda _spec, _ctx: pytest.fail("diagnose_eligibility should not be called"),
    )
    report = EligibilityReport(
        eligible=False,
        missing_env_any=[["OPENROUTER_API_KEY", "ARK_API_KEY"]],
        declared=True,
    )

    summary = build_dependency_summary(
        spec,
        loader=loader,
        ctx=EligibilityContext(os_name="linux"),
        report=report,
    )

    assert summary["declared"]["api_env"]["any"] == [
        "OPENROUTER_API_KEY",
        "ARK_API_KEY",
    ]
    assert summary["missing"]["api_env"]["any"] == [
        ["OPENROUTER_API_KEY", "ARK_API_KEY"]
    ]
    assert summary["missing"]["count"] == 1
