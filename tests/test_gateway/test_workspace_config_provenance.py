from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import tomli_w

from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding.config_store import persist_config
from opensquilla.session.models import SessionNode


@pytest.fixture(autouse=True)
def isolated_workspace_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("OPENSQUILLA_WORKSPACE_DIR", raising=False)
    monkeypatch.setattr(
        "opensquilla.gateway.config.default_opensquilla_home", lambda: tmp_path / "profile"
    )


@pytest.mark.parametrize("loader", ["missing", "empty", "direct_toml"])
def test_default_remains_default_after_profile_path_resolution(
    tmp_path: Path, loader: str
) -> None:
    target = tmp_path / "config.toml"
    if loader != "missing":
        target.write_text("", encoding="utf-8")
    config = (
        GatewayConfig.load_from_toml(target)
        if loader == "direct_toml"
        else GatewayConfig.load(target, read_only=True)
    )

    assert config.workspace_dir == str(tmp_path / "profile" / "workspace")
    assert config.workspace_dir_source == "default"
    GatewayConfig._apply_profile_path_overrides(config, target)
    assert config.workspace_dir_source == "default"


@pytest.mark.parametrize("source", ["constructor", "toml"])
def test_explicit_workspace_equal_to_default_remains_configured(
    tmp_path: Path, source: str
) -> None:
    default_root = GatewayConfig().workspace_dir
    if source == "constructor":
        config = GatewayConfig(workspace_dir=default_root)
    else:
        target = tmp_path / "config.toml"
        target.write_text(tomli_w.dumps({"workspace_dir": default_root}), encoding="utf-8")
        config = GatewayConfig.load(target, read_only=True)

    assert config.workspace_dir_source == "configured"


@pytest.mark.parametrize(
    "env_name", ["OPENSQUILLA_GATEWAY_WORKSPACE_DIR", "OPENSQUILLA_WORKSPACE_DIR"]
)
def test_environment_workspace_override_remains_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_name: str
) -> None:
    monkeypatch.setenv(env_name, "shared")

    config = GatewayConfig.load(tmp_path / "config.toml", read_only=True)

    assert config.workspace_dir == str(tmp_path / "shared")
    assert config.workspace_dir_source == "configured"


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_loaded_config_allocates_isolated_tasks_unless_explicitly_shared(
    tmp_path: Path, configured: bool
) -> None:
    from opensquilla.execution_workspaces import PreparedExecutionWorkspace
    from opensquilla.gateway.execution_workspaces import build_execution_workspace_factory

    target = tmp_path / "config.toml"
    shared = tmp_path / "shared"
    shared.mkdir()
    target.write_text('workspace_dir = "shared"\n' if configured else "", encoding="utf-8")
    config = GatewayConfig.load(target, read_only=True)
    factory = build_execution_workspace_factory(config, profile_home=tmp_path)

    first = await factory(SessionNode(session_key="agent:main:webchat:first", session_id="one"))
    second = await factory(SessionNode(session_key="agent:main:webchat:second", session_id="two"))
    if isinstance(first, PreparedExecutionWorkspace):
        first = first.binding
    if isinstance(second, PreparedExecutionWorkspace):
        second = second.binding

    assert first is not None and second is not None
    assert first["kind"] == second["kind"] == ("configured" if configured else "managed")
    if configured:
        assert first["root"] == second["root"] == str(shared.resolve())
    else:
        assert first["root"] != second["root"]
        assert Path(first["root"]).parent == Path(second["root"]).parent == tmp_path / "tasks"


@pytest.mark.parametrize("configured", [False, True])
def test_copy_and_live_reload_preserve_workspace_provenance(
    tmp_path: Path, configured: bool
) -> None:
    target = tmp_path / "config.toml"
    target.write_text('workspace_dir = "shared"\n' if configured else "", encoding="utf-8")
    loaded = GatewayConfig.load(target, read_only=True)
    expected = "configured" if configured else "default"
    assert loaded.model_copy(deep=True).workspace_dir_source == expected

    # AppSettings reconstructs a full model, then restores the source model's
    # sparse-persistence provenance before applying explicit user mutations.
    candidate = GatewayConfig(**loaded.model_dump())
    candidate.inherit_persist_provenance(loaded)
    assert candidate.workspace_dir_source == expected

    live = GatewayConfig(workspace_dir="other") if not configured else GatewayConfig()
    for field_name in GatewayConfig.model_fields:
        setattr(live, field_name, getattr(loaded, field_name))
    live.reconcile_runtime_overrides(loaded)
    assert live.workspace_dir_source == expected


def test_unrelated_config_save_does_not_make_default_workspace_explicit(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    config = GatewayConfig.load(target, read_only=True)
    config.port += 1

    persist_config(config, path=target)

    assert "workspace_dir" not in tomllib.loads(target.read_text(encoding="utf-8"))
    assert config.workspace_dir_source == "default"
    assert GatewayConfig.load(target, read_only=True).workspace_dir_source == "default"


@pytest.mark.parametrize("same_as_default", [False, True])
def test_explicit_runtime_workspace_edit_is_configured_and_persisted(
    tmp_path: Path, same_as_default: bool
) -> None:
    target = tmp_path / "config.toml"
    live = GatewayConfig.load(target, read_only=True)
    candidate = GatewayConfig(**live.model_dump())
    candidate.inherit_persist_provenance(live)
    if not same_as_default:
        candidate.workspace_dir = str(tmp_path / "selected-workspace")
    candidate.clear_runtime_override("workspace_dir")
    candidate.mark_force_persist_segments(("workspace_dir",))

    assert candidate.workspace_dir_source == "configured"
    persist_config(candidate, path=target)
    live.reconcile_runtime_overrides(candidate)

    assert live.workspace_dir_source == "configured"
    assert GatewayConfig.load(target, read_only=True).workspace_dir_source == "configured"


def test_committed_full_config_workspace_change_is_configured(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    live = GatewayConfig.load(target, read_only=True)
    payload = live.model_dump()
    payload["workspace_dir"] = str(tmp_path / "selected-workspace")
    candidate = GatewayConfig(**payload)
    candidate.inherit_persist_provenance(live)

    # config.apply persists a replacement before swapping it into the live
    # config, without the force-persist markers used by config.set/patch.
    persist_config(candidate, path=target)
    live.reconcile_runtime_overrides(candidate)

    assert candidate.workspace_dir_source == "configured"
    assert live.workspace_dir_source == "configured"
