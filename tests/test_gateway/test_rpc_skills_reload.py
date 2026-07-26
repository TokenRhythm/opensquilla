from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.gateway import rpc_skills
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.scopes import ADMIN_SCOPE, METHOD_SCOPES
from opensquilla.skills.loader import SkillLoader


def _write_skill(root, name: str, description: str = "Demo") -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_skills_reload_forces_running_loader_and_returns_stable_diff(tmp_path) -> None:
    managed_dir = tmp_path / "managed"
    loader = SkillLoader(managed_dir=managed_dir, snapshot_path=tmp_path / "snapshot.json")
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    await rpc_skills._handle_skills_list(None, ctx)
    old_generation = loader.snapshot().generation
    _write_skill(managed_dir, "plotter")

    payload = await rpc_skills._handle_skills_reload(None, ctx)

    assert payload == {
        "success": True,
        "changed": True,
        "partial": False,
        "generation": old_generation + 1,
        "added": ["plotter"],
        "removed": [],
        "modified": [],
        "errors": [],
    }


@pytest.mark.asyncio
async def test_skills_reload_no_change_keeps_generation(tmp_path) -> None:
    from opensquilla.engine.steps import skills_filter

    managed_dir = tmp_path / "managed"
    _write_skill(managed_dir, "plotter")
    loader = SkillLoader(managed_dir=managed_dir, snapshot_path=tmp_path / "snapshot.json")
    ctx = RpcContext(conn_id="test", skill_loader=loader)
    await rpc_skills._handle_skills_list(None, ctx)
    generation = loader.snapshot().generation
    skills_filter._elig_ctx.has_bin_cache["newly-installed-tool"] = False
    skills_filter._elig_ctx.env_cache["UPDATED_TOKEN"] = None

    payload = await rpc_skills._handle_skills_reload(None, ctx)

    assert payload["success"] is True
    assert payload["changed"] is False
    assert payload["generation"] == generation
    assert payload["added"] == []
    assert payload["removed"] == []
    assert payload["modified"] == []
    assert payload["errors"] == []
    assert skills_filter._elig_ctx.has_bin_cache == {}
    assert skills_filter._elig_ctx.env_cache == {}


@pytest.mark.asyncio
async def test_skills_reload_partial_keeps_previous_valid_skill(tmp_path) -> None:
    managed_dir = tmp_path / "managed"
    _write_skill(managed_dir, "plotter", "Valid description")
    loader = SkillLoader(managed_dir=managed_dir, snapshot_path=tmp_path / "snapshot.json")
    ctx = RpcContext(conn_id="test", skill_loader=loader)
    await rpc_skills._handle_skills_list(None, ctx)
    skill_file = managed_dir / "plotter" / "SKILL.md"
    skill_file.write_text("not valid frontmatter\n", encoding="utf-8")

    payload = await rpc_skills._handle_skills_reload(None, ctx)

    assert payload["success"] is True
    assert payload["partial"] is True
    assert payload["modified"] == ["plotter"]
    assert payload["errors"][0]["name"] == "plotter"
    assert payload["errors"][0]["kept_previous"] is True
    assert loader.snapshot().skills[0].description == "Valid description"


@pytest.mark.asyncio
async def test_skills_list_keeps_previous_when_frontmatter_name_is_not_string(tmp_path) -> None:
    managed_dir = tmp_path / "managed"
    _write_skill(managed_dir, "plotter", "Valid description")
    loader = SkillLoader(managed_dir=managed_dir, snapshot_path=tmp_path / "snapshot.json")
    ctx = RpcContext(conn_id="test", skill_loader=loader)
    await rpc_skills._handle_skills_list(None, ctx)
    (managed_dir / "plotter" / "SKILL.md").write_text(
        "---\nname: [not, hashable]\ndescription: invalid\n---\nbody\n",
        encoding="utf-8",
    )

    payload = await rpc_skills._handle_skills_list(None, ctx)

    assert [skill["name"] for skill in payload["skills"]] == ["plotter"]
    assert loader.snapshot().errors[0].kept_previous is True


@pytest.mark.asyncio
async def test_skills_reload_scan_failure_keeps_old_generation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_dir = tmp_path / "managed"
    _write_skill(managed_dir, "plotter")
    loader = SkillLoader(managed_dir=managed_dir, snapshot_path=tmp_path / "snapshot.json")
    ctx = RpcContext(conn_id="test", skill_loader=loader)
    await rpc_skills._handle_skills_list(None, ctx)
    generation = loader.snapshot().generation

    def fail_scan():
        raise OSError("catalog unavailable")

    monkeypatch.setattr(loader, "_build_manifest", fail_scan)
    payload = await rpc_skills._handle_skills_reload(None, ctx)

    assert payload["success"] is False
    assert payload["changed"] is False
    assert payload["partial"] is False
    assert payload["generation"] == generation
    assert payload["errors"][0]["message"] == "catalog unavailable"
    assert payload["errors"][0]["kept_previous"] is True


@pytest.mark.asyncio
async def test_skills_reload_without_loader_has_stable_failure_shape() -> None:
    payload = await rpc_skills._handle_skills_reload(None, RpcContext(conn_id="test"))

    assert payload["success"] is False
    assert payload["changed"] is False
    assert payload["partial"] is False
    assert payload["generation"] == 0
    assert payload["added"] == []
    assert payload["removed"] == []
    assert payload["modified"] == []
    assert payload["errors"][0]["message"] == "No skill loader configured"


@pytest.mark.asyncio
async def test_skills_list_refreshes_once_and_reads_one_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = SimpleNamespace(name="demo", user_invocable=True)

    class FakeLoader:
        def __init__(self) -> None:
            self.refresh_reasons: list[str] = []
            self.snapshot_calls = 0

        def refresh_if_changed(self, reason: str):
            self.refresh_reasons.append(reason)

        def snapshot(self):
            self.snapshot_calls += 1
            return SimpleNamespace(skills=(spec,))

        def load_all(self):
            raise AssertionError("catalog RPC must use its pinned snapshot")

    loader = FakeLoader()
    ctx = RpcContext(conn_id="test", skill_loader=loader)
    monkeypatch.setattr(rpc_skills, "is_skill_available_live", lambda _name: True)
    monkeypatch.setattr(rpc_skills, "diagnose_eligibility", lambda *_args: object())
    monkeypatch.setattr(
        rpc_skills,
        "_skill_to_dict",
        lambda skill, *_args, **_kwargs: {"name": skill.name},
    )

    payload = await rpc_skills._handle_skills_list(None, ctx)

    assert payload == {"skills": [{"name": "demo"}]}
    assert loader.refresh_reasons == ["rpc.skills.list"]
    assert loader.snapshot_calls == 1


def test_skills_reload_is_admin_scoped() -> None:
    assert METHOD_SCOPES["skills.reload"] == ADMIN_SCOPE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "success"),
    [
        ("install", True),
        ("install", False),
        ("update", True),
        ("update", False),
        ("uninstall", True),
        ("uninstall", False),
    ],
)
async def test_catalog_mutations_dirty_only_after_success(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    success: bool,
) -> None:
    loader = SkillLoader(
        managed_dir=tmp_path / "managed",
        snapshot_path=tmp_path / "snapshot.json",
    )
    loader.load_all()

    class _Installer:
        async def install(self, *_args, **_kwargs):
            return SimpleNamespace(
                success=success,
                name="demo",
                message="done",
                path=None,
                scan=None,
            )

        async def update(self, *_args, **_kwargs):
            return [SimpleNamespace(success=success, name="demo", message="done")]

        async def uninstall(self, *_args, **_kwargs):
            return SimpleNamespace(success=success, name="demo", message="done")

    monkeypatch.setattr(rpc_skills, "_get_default_installer", lambda **_kwargs: _Installer())
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    if operation == "install":
        await rpc_skills._handle_skills_install({"identifier": "demo"}, ctx)
    elif operation == "update":
        await rpc_skills._handle_skills_update({"name": "demo"}, ctx)
    else:
        await rpc_skills._handle_skills_uninstall({"name": "demo"}, ctx)

    assert loader._dirty is success
