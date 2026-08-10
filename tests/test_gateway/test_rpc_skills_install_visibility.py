from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opensquilla.gateway import rpc_skills
from opensquilla.gateway.rpc import RpcContext
from opensquilla.skills.hub.deps import DepResult
from opensquilla.skills.hub.installer import InstallResult
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.types import SkillLayer, SkillPlatformMeta, SkillRequires, SkillSpec


def _write_skill(dir_path: Path, name: str, body: str) -> None:
    skill_dir = dir_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def _write_needs_key_skill(dir_path: Path) -> None:
    _write_skill(
        dir_path,
        "needs-key",
        """---
name: needs-key
description: Needs one of two API keys.
metadata:
  opensquilla:
    requires:
      envAny: [OPENROUTER_API_KEY, ARK_API_KEY]
    install:
      - id: helper
        kind: uv
        label: Install helper
        package: helper-pkg
---

# body
""",
    )


def test_rpc_skill_install_uses_loader_managed_dir_and_list_sees_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        managed_dir = tmp_path / "managed"
        loader = SkillLoader(
            managed_dir=managed_dir,
            snapshot_path=tmp_path / "snapshot.json",
        )
        ctx = RpcContext(conn_id="test", skill_loader=loader)
        captured: dict[str, Path | None] = {}

        class FakeInstaller:
            def __init__(self, managed_dir: Path) -> None:
                self.managed_dir = managed_dir

            async def install(
                self,
                identifier: str,
                source_id: str,
                force: bool = False,
            ) -> InstallResult:
                skill_dir = self.managed_dir / identifier
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    "---\n"
                    f"name: {identifier}\n"
                    "description: Installed from chat\n"
                    "---\n"
                    "Installed body.\n",
                    encoding="utf-8",
                )
                return InstallResult(
                    success=True,
                    name=identifier,
                    message="installed",
                    path=str(skill_dir),
                )

        def fake_builder(*, managed_dir: Path | None = None) -> FakeInstaller:
            assert managed_dir is not None
            captured["managed_dir"] = managed_dir
            return FakeInstaller(managed_dir)

        monkeypatch.setattr(rpc_skills, "build_default_skill_installer", fake_builder)
        assert await rpc_skills._handle_skills_list(None, ctx) == {"skills": []}
        assert loader._cached is not None
        rebuilds = 0
        original_build = loader._build_catalog

        def counted_build(*args, **kwargs):
            nonlocal rebuilds
            rebuilds += 1
            return original_build(*args, **kwargs)

        monkeypatch.setattr(loader, "_build_catalog", counted_build)

        installed = await rpc_skills._handle_skills_install(
            {"identifier": "plotter", "source": "clawhub"},
            ctx,
        )
        listed = await rpc_skills._handle_skills_list(None, ctx)

        assert captured["managed_dir"] == managed_dir
        assert installed["success"] is True
        assert Path(installed["path"]).name == "plotter"
        row = next(skill for skill in listed["skills"] if skill["name"] == "plotter")
        assert row["layer"] == "managed"
        assert row["description"] == "Installed from chat"
        assert rebuilds == 1

    asyncio.run(run())


@pytest.mark.asyncio
async def test_rpc_skills_list_exposes_dependency_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    managed_dir = tmp_path / "managed"
    _write_needs_key_skill(managed_dir)
    loader = SkillLoader(managed_dir=managed_dir, snapshot_path=tmp_path / "snapshot.json")
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    listed = await rpc_skills._handle_skills_list(None, ctx)

    row = next(skill for skill in listed["skills"] if skill["name"] == "needs-key")
    assert row["status"] == "needs_setup"
    assert row["eligible"] is False
    assert row["dependency_summary"]["declared"]["api_env"]["any"] == [
        "OPENROUTER_API_KEY",
        "ARK_API_KEY",
    ]
    assert row["dependency_summary"]["missing"]["api_env"]["any"] == [
        ["OPENROUTER_API_KEY", "ARK_API_KEY"]
    ]
    assert row["missing_env_any"] == [["OPENROUTER_API_KEY", "ARK_API_KEY"]]
    assert "OPENROUTER_API_KEY or ARK_API_KEY" in row["status_detail"]


@pytest.mark.asyncio
async def test_rpc_skills_status_exposes_dependency_summary_and_legacy_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    managed_dir = tmp_path / "managed"
    _write_needs_key_skill(managed_dir)
    loader = SkillLoader(managed_dir=managed_dir, snapshot_path=tmp_path / "snapshot.json")
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    status_rows = await rpc_skills._handle_skills_status(None, ctx)

    row = next(skill for skill in status_rows if skill["name"] == "needs-key")
    assert row["status"] == "needs_setup"
    assert row["install"] == [
        {
            "id": "helper",
            "kind": "uv",
            "label": "Install helper",
            "bins": [],
        }
    ]
    assert row["dependency_summary"]["declared"]["api_env"]["any"] == [
        "OPENROUTER_API_KEY",
        "ARK_API_KEY",
    ]
    assert row["dependency_summary"]["missing"]["api_env"]["any"] == [
        ["OPENROUTER_API_KEY", "ARK_API_KEY"]
    ]
    assert row["missing_env_any"] == [["OPENROUTER_API_KEY", "ARK_API_KEY"]]
    assert row["missing_env"] == []
    assert row["missing_bins"] == []


@pytest.mark.asyncio
async def test_rpc_skills_get_exposes_dependency_summary_and_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    managed_dir = tmp_path / "managed"
    _write_needs_key_skill(managed_dir)
    loader = SkillLoader(managed_dir=managed_dir, snapshot_path=tmp_path / "snapshot.json")
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    result = await rpc_skills._handle_skills_get({"name": "needs-key"}, ctx)

    assert result["name"] == "needs-key"
    assert result["status"] == "needs_setup"
    assert result["install"] == [
        {
            "id": "helper",
            "kind": "uv",
            "label": "Install helper",
            "bins": [],
        }
    ]
    assert result["dependency_summary"]["declared"]["api_env"]["any"] == [
        "OPENROUTER_API_KEY",
        "ARK_API_KEY",
    ]
    assert result["dependency_summary"]["missing"]["api_env"]["any"] == [
        ["OPENROUTER_API_KEY", "ARK_API_KEY"]
    ]
    assert result["missing_env_any"] == [["OPENROUTER_API_KEY", "ARK_API_KEY"]]
    assert result["content"] == "# body"
    assert Path(result["file_path"]).name == "SKILL.md"
    assert Path(result["base_dir"]).name == "needs-key"


@pytest.mark.asyncio
async def test_rpc_skills_deps_install_reports_env_any_missing_still(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    _write_skill(
        tmp_path,
        "env-any-install",
        """---
name: env-any-install
description: Has install metadata but still needs one API key.
metadata:
  opensquilla:
    requires:
      envAny: [OPENROUTER_API_KEY, ARK_API_KEY]
    install:
      - id: helper
        kind: uv
        label: Install helper
        package: helper-pkg
---

# body
""",
    )
    loader = SkillLoader(bundled_dir=tmp_path, snapshot_path=tmp_path / "snapshot.json")
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    from opensquilla.engine.steps import skills_filter

    skills_filter._elig_ctx.has_bin_cache["helper"] = False
    skills_filter._elig_ctx.env_cache["HELPER_TOKEN"] = None

    async def fake_install_deps(_specs: list[object]) -> list[DepResult]:
        return [DepResult(kind="uv", identifier="helper", success=True, message="Installed")]

    monkeypatch.setattr(rpc_skills, "install_deps", fake_install_deps)

    result = await rpc_skills._handle_skills_deps_install(
        {"name": "env-any-install", "install_id": "helper"},
        ctx,
    )

    assert result["success"] is True
    assert result["missing_still"]["env_any"] == [["OPENROUTER_API_KEY", "ARK_API_KEY"]]
    assert loader._dirty is False
    assert skills_filter._elig_ctx.has_bin_cache == {}
    assert skills_filter._elig_ctx.env_cache == {}


def test_skill_payload_rolls_up_meta_subskill_requirements() -> None:
    python_skill = SkillSpec(
        name="docx",
        description="Docx export",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="",
        metadata=SkillPlatformMeta(requires=SkillRequires(any_bins=["python", "python3"])),
    )
    ffmpeg_skill = SkillSpec(
        name="video-merger",
        description="Video merge",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="",
        metadata=SkillPlatformMeta(requires=SkillRequires(bins=["ffmpeg", "ffprobe"])),
    )
    meta_skill = SkillSpec(
        name="meta-demo",
        description="Meta demo",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="",
        kind="meta",
        composition_raw={
            "steps": [
                {"id": "export", "kind": "skill_exec", "skill": "docx"},
                {"id": "merge", "kind": "skill_exec", "skill": "video-merger"},
            ]
        },
    )

    ctx = rpc_skills.EligibilityContext.auto()
    ctx.has_bin_cache.update(
        {"python": True, "python3": True, "ffmpeg": False, "ffprobe": False}
    )
    skill_index = {s.name: s for s in (meta_skill, python_skill, ffmpeg_skill)}
    payload = rpc_skills._skill_to_dict(
        meta_skill,
        rpc_skills.diagnose_eligibility(meta_skill, ctx),
        ctx.os_name,
        skill_index=skill_index,
        eligibility_ctx=ctx,
    )

    assert payload["requirements"]["summary"] == "needs_setup"
    assert payload["requirements"]["items"] == [
        {
            "name": "docx",
            "source": "sub_skill",
            "status": "ready",
            "requires_bins": [],
            "requires_any_bins": ["python", "python3"],
            "requires_env": [],
            "missing_bins": [],
            "missing_env": [],
        },
        {
            "name": "video-merger",
            "source": "sub_skill",
            "status": "needs_setup",
            "requires_bins": ["ffmpeg", "ffprobe"],
            "requires_any_bins": [],
            "requires_env": [],
            "missing_bins": ["ffmpeg", "ffprobe"],
            "missing_env": [],
        },
    ]


def test_meta_paper_write_declares_pdf_compile_binaries() -> None:
    bundled = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "opensquilla"
        / "skills"
        / "bundled"
    )
    loader = SkillLoader(bundled_dir=bundled)
    skills = loader.load_all()
    skill_index = {skill.name: skill for skill in skills}
    ctx = rpc_skills.EligibilityContext.auto()
    spec = skill_index["meta-paper-write"]
    payload = rpc_skills._skill_to_dict(
        spec,
        rpc_skills.diagnose_eligibility(spec, ctx),
        ctx.os_name,
        skill_index=skill_index,
        eligibility_ctx=ctx,
    )

    own_requirements = next(
        item for item in payload["requirements"]["items"] if item["source"] == "self"
    )
    assert own_requirements["requires_bins"] == ["xelatex", "bibtex"]


def test_meta_payload_marks_only_trusted_provider_backed_plans_for_launch_check() -> None:
    bundled = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "opensquilla"
        / "skills"
        / "bundled"
    )
    loader = SkillLoader(bundled_dir=bundled)
    skills = loader.load_all()
    skill_index = {skill.name: skill for skill in skills}
    ctx = rpc_skills.EligibilityContext.auto()

    def payload(name: str) -> dict[str, object]:
        spec = skill_index[name]
        return rpc_skills._skill_to_dict(
            spec,
            rpc_skills.diagnose_eligibility(spec, ctx),
            ctx.os_name,
            skill_index=skill_index,
            eligibility_ctx=ctx,
        )

    assert payload("meta-short-drama")["provider_check_at_launch"] is True
    assert payload("AwesomeWebpageMetaSkill")["provider_check_at_launch"] is True
    assert payload("meta-paper-write")["provider_check_at_launch"] is False
    assert payload("meta-skill-creator")["provider_check_at_launch"] is False


@pytest.mark.asyncio
async def test_rpc_skills_list_exposes_meta_skill_dependency_rollup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    managed_dir = tmp_path / "managed"
    _write_skill(
        managed_dir,
        "child-needs-bin",
        """---
name: child-needs-bin
description: Child skill requiring a missing binary.
metadata:
  opensquilla:
    requires:
      bins: [missing-child-tool]
---

# body
""",
    )
    _write_skill(
        managed_dir,
        "parent-meta",
        """---
name: parent-meta
description: Meta skill referencing a child.
kind: meta
composition:
  steps:
    - id: child
      skill: child-needs-bin
---

# body
""",
    )
    loader = SkillLoader(managed_dir=managed_dir, snapshot_path=tmp_path / "snapshot.json")
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    listed = await rpc_skills._handle_skills_list(None, ctx)

    row = next(skill for skill in listed["skills"] if skill["name"] == "parent-meta")
    assert row["dependency_summary"]["sub_skill_dependencies"]["missing_count"] == 1
