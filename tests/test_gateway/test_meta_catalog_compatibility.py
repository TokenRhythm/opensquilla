from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.gateway import rpc_meta_runs, rpc_skills  # noqa: F401 — register RPCs
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.skills import eligibility
from opensquilla.skills.catalog_policy import PUBLIC_BUNDLED_SKILLS, STABLE_META_SKILLS
from opensquilla.skills.hub.lockfile import LockEntry, Lockfile, compute_tree_sha256
from opensquilla.skills.loader import SkillLoader

BUNDLED = Path(__file__).resolve().parents[2] / "src/opensquilla/skills/bundled"


async def _call(ctx: RpcContext, method: str, params: dict | None = None):
    return await get_dispatcher().dispatch("catalog-test", method, params, ctx)


@pytest.fixture
def catalog_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RpcContext:
    monkeypatch.setenv("OPENSQUILLA_HOME", str(tmp_path))
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    return RpcContext(conn_id="catalog", skill_loader=loader)


@pytest.mark.asyncio
async def test_old_client_lists_and_reads_public_meta_roots(catalog_context: RpcContext) -> None:
    listed = await _call(catalog_context, "skills.list", {"includeLifecycle": True})
    assert listed.ok, listed.error
    assert [row["name"] for row in listed.payload["skills"]] == [
        *PUBLIC_BUNDLED_SKILLS,
        *STABLE_META_SKILLS,
    ]
    for row in listed.payload["skills"]:
        if row["kind"] != "meta":
            continue
        # This is the request shape sent by the pre-split WebUI.
        detail = await _call(
            catalog_context,
            "skills.get",
            {
                "name": row["name"],
                "instanceId": row["instance_id"],
                "includeLifecycle": True,
            },
        )
        assert detail.ok, detail.error
        assert detail.payload["kind"] == "meta"
        assert detail.payload["content"]
        assert detail.payload["sub_skills"]
    named = await _call(catalog_context, "skills.get", {"name": "meta-paper-write"})
    assert named.ok and named.payload["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["paper-section-author", "AwesomeWebpageMetaSkill", "meta-kid-project-planner"]
)
@pytest.mark.parametrize("exact", [False, True])
async def test_public_rpc_cannot_read_internal_or_retired_bodies(
    catalog_context: RpcContext,
    name: str,
    exact: bool,
) -> None:
    spec = catalog_context.skill_loader.get_by_name(name)
    assert spec is not None
    params = {"name": name}
    if exact:
        params["instanceId"] = spec.instance_id
    detail = await _call(catalog_context, "skills.get", params)
    assert not detail.ok
    assert detail.error.code == "NOT_FOUND"
    inspected = await _call(catalog_context, "meta.inspect", params)
    assert not inspected.ok
    assert inspected.error.code == "NOT_FOUND"


def _write_meta(root: Path, directory: str, description: str) -> Path:
    path = root / directory
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        "---\nname: shared-meta\nkind: meta\n"
        f"description: {description}\n"
        "composition:\n  steps:\n    - id: action\n      skill: missing-helper\n"
        "---\nRoot instructions.\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_overridden_stable_name_inspects_the_winners_own_dependencies(
    catalog_context: RpcContext,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    path = _write_meta(workspace, "user-paper", "User-owned paper workflow") / "SKILL.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("name: shared-meta", "name: meta-paper-write"),
        encoding="utf-8",
    )
    catalog_context.skill_loader = SkillLoader(
        bundled_dir=BUNDLED,
        workspace_dir=workspace,
        snapshot_path=tmp_path / "override.json",
    )
    listed = await _call(catalog_context, "meta.list")
    assert listed.ok, listed.error
    root = next(item for item in listed.payload["skills"] if item["name"] == "meta-paper-write")
    assert root["layer"] == "workspace"
    assert root["dependency_count"] == 1
    inspected = await _call(
        catalog_context,
        "meta.inspect",
        {
            "name": root["name"],
            "instanceId": root["instance_id"],
        },
    )
    assert inspected.ok, inspected.error
    assert [item["name"] for item in inspected.payload["dependencies"]] == ["missing-helper"]


def _managed_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, shadowed: bool
) -> RpcContext:
    monkeypatch.setenv("OPENSQUILLA_HOME", str(tmp_path))
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    managed = tmp_path / "managed"
    installed = _write_meta(managed, "managed-copy", "Installed workflow")
    workspace = tmp_path / "workspace"
    if shadowed:
        _write_meta(workspace, "project-copy", "Workspace workflow")
    lockfile = Lockfile()
    lockfile.add(
        "shared-meta",
        LockEntry(
            source="github",
            identifier="example/repo:managed-copy",
            path=str(installed),
            relative_path="managed-copy",
            directory_name="managed-copy",
            manifest_name="shared-meta",
            install_id="install-managed-meta",
            resolved_identifier=f"example/repo@{'a' * 40}:managed-copy/SKILL.md",
            resolved_revision="a" * 40,
            artifact_sha256="artifact",
            tree_sha256=compute_tree_sha256(installed),
            parser_version="community-strict-v1",
            dialect="instruction-first",
        ),
    )
    if shadowed:
        # Tracked Community content is instruction-only even if its raw
        # frontmatter claims to be a Meta workflow.
        lockfile.save(tmp_path / "skills-lock.json")
    loader = SkillLoader(
        managed_dir=managed,
        workspace_dir=workspace,
        snapshot_path=tmp_path / "snapshot.json",
    )
    return RpcContext(conn_id="managed-catalog", skill_loader=loader)


@pytest.mark.asyncio
@pytest.mark.parametrize("shadowed", [False, True])
async def test_meta_identity_matches_lifecycle_and_inspect_only_accepts_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shadowed: bool,
) -> None:
    ctx = _managed_context(tmp_path, monkeypatch, shadowed=shadowed)
    listed = await _call(ctx, "skills.list", {"includeLifecycle": True})
    metas = await _call(ctx, "meta.list")
    assert listed.ok, listed.error
    assert metas.ok, metas.error
    assert len(metas.payload["skills"]) == 1
    meta = metas.payload["skills"][0]
    active = next(row for row in listed.payload["skills"] if row["active"])
    assert meta["layer"] == ("workspace" if shadowed else "managed")
    for key in ("name", "layer", "instance_id", "install_id"):
        assert meta[key] == active[key]
    assert meta["instance_id"]
    assert meta["dependency_count"] == 1
    assert meta["install_id"] == ""
    inspected = await _call(
        ctx,
        "meta.inspect",
        {
            "name": meta["name"],
            "instanceId": meta["instance_id"],
            "installId": meta["install_id"],
        },
    )
    assert inspected.ok, inspected.error
    assert inspected.payload["instance_id"] == active["instance_id"]
    assert "content" not in inspected.payload
    assert all("content" not in item for item in inspected.payload["dependencies"])
    if shadowed:
        old = next(row for row in listed.payload["skills"] if row["layer"] == "managed")
        assert old["kind"] == "skill"
        rejected = await _call(
            ctx,
            "meta.inspect",
            {
                "name": old["name"],
                "instanceId": old["instance_id"],
                "installId": old["install_id"],
            },
        )
        assert not rejected.ok and rejected.error.code == "NOT_FOUND"
        # Existing management clients can still inspect their exact installed instructions.
        managed = await _call(
            ctx,
            "skills.get",
            {
                "name": old["name"],
                "instanceId": old["instance_id"],
                "installId": old["install_id"],
                "includeLifecycle": True,
            },
        )
        assert managed.ok and managed.payload["lifecycle"]["selection_state"] == "shadowed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"name": "meta-paper-write", "instanceId": "one", "instance_id": "two"},
        {"name": "meta-paper-write", "installId": "one", "install_id": "two"},
        {"name": "meta-paper-write", "instanceId": 7},
        {"name": "meta-paper-write", "installId": False},
        {"name": " "},
    ],
)
async def test_meta_inspect_rejects_invalid_identity(
    catalog_context: RpcContext, params: dict
) -> None:
    result = await _call(catalog_context, "meta.inspect", params)
    assert not result.ok
    assert result.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_disabled_meta_catalog_has_valid_wire_responses(catalog_context: RpcContext) -> None:
    catalog_context.config = {"meta_skill": {"enabled": False}}
    listed = await _call(catalog_context, "meta.list")
    inspected = await _call(catalog_context, "meta.inspect", {"name": "meta-paper-write"})
    assert listed.ok and listed.payload == {"skills": [], "disabled": True}
    assert inspected.ok and inspected.payload == {"disabled": True}
    legacy = await _call(catalog_context, "skills.list")
    assert legacy.ok
    assert all(row["kind"] != "meta" for row in legacy.payload["skills"])


@pytest.mark.asyncio
@pytest.mark.parametrize("live_gate", [False, True])
async def test_operator_disabled_meta_is_diagnostic_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_gate: bool,
) -> None:
    ctx = _managed_context(tmp_path, monkeypatch, shadowed=False)
    ctx.config = GatewayConfig()
    ctx.config.skills.disabled = ["shared-meta"]
    monkeypatch.setattr(
        eligibility,
        "_live_skills_cfg_getter",
        (lambda: ctx.config.skills) if live_gate else None,
    )
    listed = await _call(ctx, "meta.list")
    assert listed.ok and listed.payload["skills"] == []
    root = ctx.skill_loader.get_by_name("shared-meta")
    assert root is not None
    for params in (
        {"name": root.name},
        {"name": root.name, "instanceId": root.instance_id},
    ):
        detail = await _call(ctx, "meta.inspect", params)
        assert not detail.ok and detail.error.code == "NOT_FOUND"
    legacy = await _call(ctx, "skills.list", {"includeLifecycle": True})
    assert legacy.ok, legacy.error
    diagnostic = next(row for row in legacy.payload["skills"] if row["name"] == root.name)
    assert diagnostic["disabled"] and not diagnostic["eligible"]
    assert diagnostic["status"] == "needs_setup"
    if live_gate:
        # Existing disabled-content policy also applies to an exact identity.
        detail = await _call(ctx, "skills.get", {"instanceId": root.instance_id})
        assert not detail.ok and detail.error.code == "NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize("live_gate", [False, True])
async def test_meta_dependency_readiness_honors_operator_disabled_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_gate: bool,
) -> None:
    ctx = _managed_context(tmp_path, monkeypatch, shadowed=False)
    helper = tmp_path / "managed" / "helper"
    helper.mkdir()
    (helper / "SKILL.md").write_text(
        "---\nname: missing-helper\ndescription: Synthetic helper\n---\nInstructions.\n",
        encoding="utf-8",
    )
    ctx.config = GatewayConfig()
    ctx.config.skills.disabled = ["missing-helper"]
    monkeypatch.setattr(
        eligibility,
        "_live_skills_cfg_getter",
        (lambda: ctx.config.skills) if live_gate else None,
    )
    listed = await _call(ctx, "meta.list")
    inspected = await _call(ctx, "meta.inspect", {"name": "shared-meta"})
    assert listed.ok and inspected.ok
    for row in (listed.payload["skills"][0], inspected.payload):
        assert not row["ready"] and row["status"] == "needs_setup"
        assert any("missing-helper" in reason for reason in row["reasons"])


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["meta.list", "meta.inspect"])
async def test_meta_catalog_preserves_read_scope_and_guest_boundary(
    catalog_context: RpcContext,
    method: str,
) -> None:
    catalog_context.principal = Principal(
        role="operator",
        scopes=frozenset({"operator.read"}),
        is_owner=False,
        authenticated=True,
    )
    params = {"name": "meta-paper-write"} if method == "meta.inspect" else None
    allowed = await _call(catalog_context, method, params)
    assert allowed.ok, allowed.error
    catalog_context.principal = Principal(
        role="operator",
        scopes=frozenset({"operator.read"}),
        is_owner=False,
        authenticated=False,
    )
    denied = await _call(catalog_context, method, params)
    assert not denied.ok and denied.error.code == "UNAUTHORIZED"
