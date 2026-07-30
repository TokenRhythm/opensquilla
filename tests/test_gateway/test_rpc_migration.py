from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.scopes import ADMIN_SCOPE, METHOD_SCOPES
from opensquilla.migration.legacy_detect import LegacyHomeCandidate


def _make_home(path: Path, *, running: bool = False) -> Path:
    path.mkdir(parents=True)
    (path / "config.toml").write_text("port = 18791\n", encoding="utf-8")
    (path / "state").mkdir()
    if running:
        (path / "state" / "gateway.pid").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "start_ts": "2026-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
    return path


def _ctx(
    target: Path,
    *,
    conn_id: str = "migration-test",
    scopes: frozenset[str] = frozenset({"operator.admin"}),
) -> RpcContext:
    return RpcContext(
        conn_id=conn_id,
        principal=Principal(
            role="operator",
            scopes=scopes,
            is_owner=True,
            authenticated=True,
        ),
        config=GatewayConfig(
            state_dir=str(target / "state"),
            config_path=str(target / "config.toml"),
        ),
    )


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key
            for nested in value.values()
            for key in _all_keys(nested)
        }
    if isinstance(value, list):
        return {key for nested in value for key in _all_keys(nested)}
    return set()


@pytest.fixture(autouse=True)
def _clear_candidate_cache() -> None:
    from opensquilla.gateway import rpc_migration

    rpc_migration._candidate_cache.clear()
    yield
    rpc_migration._candidate_cache.clear()


def _patch_one_candidate(
    monkeypatch: pytest.MonkeyPatch,
    source: Path,
    *,
    kind: str = "cli-home",
) -> None:
    from opensquilla.migration import legacy_detect

    monkeypatch.setattr(
        legacy_detect,
        "detect_legacy_homes",
        lambda target, *, limit=12: [LegacyHomeCandidate(path=source, kind=kind)],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["operator.read", "operator.write"])
async def test_migration_rpc_scope_and_method_contract(
    tmp_path: Path,
    scope: str,
) -> None:
    target = _make_home(tmp_path / "target")
    assert METHOD_SCOPES["migration.sources.list"] == ADMIN_SCOPE
    assert METHOD_SCOPES["migration.sources.preview"] == ADMIN_SCOPE
    assert "migration.apply" not in get_dispatcher().methods()

    response = await get_dispatcher().dispatch(
        "req-read",
        "migration.sources.list",
        {},
        _ctx(target, scopes=frozenset({scope})),
    )

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_list_is_settings_only_privacy_narrow_and_uses_actual_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _make_home(tmp_path / "private-user-name" / "legacy-source")
    (source / "install-receipt.json").write_text(
        json.dumps({"version": str(source / "version-path-leak")}),
        encoding="utf-8",
    )
    target = _make_home(tmp_path / "active-target")
    _patch_one_candidate(monkeypatch, source)
    seen_targets: list[Path] = []
    from opensquilla.migration import legacy_detect

    original = legacy_detect.detect_legacy_homes

    def discover(target_arg: Path, *, limit: int = 12) -> list[LegacyHomeCandidate]:
        seen_targets.append(target_arg)
        return original(target_arg, limit=limit)

    monkeypatch.setattr(legacy_detect, "detect_legacy_homes", discover)

    response = await get_dispatcher().dispatch(
        "req-list",
        "migration.sources.list",
        {},
        _ctx(target),
    )

    assert response.ok is True
    payload = response.payload
    assert payload["schemaVersion"] == 1
    assert payload["mode"] == "preview_only"
    assert payload["capabilities"] == {
        "discover": True,
        "preview": True,
        "apply": False,
        "manualSource": False,
    }
    assert len(payload["candidates"]) == 1
    candidate = payload["candidates"][0]
    assert set(candidate) == {
        "candidateId",
        "sourceKind",
        "version",
        "estimatedActivityAt",
        "sessionCount",
        "sizeBytes",
        "previouslyImported",
    }
    assert candidate["sourceKind"] == "cli-home"
    assert candidate["candidateId"]
    assert candidate["version"] is None
    rendered = json.dumps(payload)
    assert str(source) not in rendered
    assert str(target) not in rendered
    assert "private-user-name" not in rendered
    assert seen_targets == [target.absolute()]


@pytest.mark.asyncio
async def test_list_returns_only_a_conservative_public_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _make_home(tmp_path / "source")
    target = _make_home(tmp_path / "target")
    (source / "install-receipt.json").write_text(
        json.dumps({"version": "0.5.0rc4+desktop.1"}),
        encoding="utf-8",
    )
    _patch_one_candidate(monkeypatch, source)

    response = await get_dispatcher().dispatch(
        "req-version",
        "migration.sources.list",
        {},
        _ctx(target),
    )

    assert response.ok is True
    assert response.payload["candidates"][0]["version"] == "0.5.0rc4+desktop.1"


@pytest.mark.asyncio
async def test_list_caps_untrusted_detector_output_at_twelve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = [_make_home(tmp_path / f"source-{index:02d}") for index in range(20)]
    target = _make_home(tmp_path / "target")
    from opensquilla.migration import legacy_detect

    monkeypatch.setattr(
        legacy_detect,
        "detect_legacy_homes",
        lambda target_arg, *, limit=12: [
            LegacyHomeCandidate(path=source, kind="cli-home") for source in sources
        ],
    )

    response = await get_dispatcher().dispatch(
        "req-list",
        "migration.sources.list",
        {},
        _ctx(target),
    )

    assert response.ok is True
    assert len(response.payload["candidates"]) == 12


@pytest.mark.asyncio
async def test_missing_gateway_config_returns_unavailable_without_scanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import rpc_migration

    monkeypatch.setattr(
        rpc_migration,
        "_discover_sync",
        lambda target: (_ for _ in ()).throw(AssertionError("unexpected scan")),
    )
    response = await get_dispatcher().dispatch(
        "req-unavailable",
        "migration.sources.list",
        {},
        RpcContext(conn_id="missing-config"),
    )

    assert response.ok is True
    assert response.payload == {
        "schemaVersion": 1,
        "mode": "preview_only",
        "capabilities": {
            "discover": False,
            "preview": False,
            "apply": False,
            "manualSource": False,
        },
        "candidates": [],
    }


@pytest.mark.asyncio
async def test_preview_allows_running_target_and_never_mutates_either_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _make_home(tmp_path / "sensitive-source")
    (source / "workspace").mkdir()
    (source / "workspace" / "MEMORY.md").write_text("private", encoding="utf-8")
    target = _make_home(tmp_path / "sensitive-target", running=True)
    _patch_one_candidate(monkeypatch, source)
    source_before = _files(source)
    target_before = _files(target)
    ctx = _ctx(target)

    listed = await get_dispatcher().dispatch(
        "req-list",
        "migration.sources.list",
        {},
        ctx,
    )
    candidate_id = listed.payload["candidates"][0]["candidateId"]
    previewed = await get_dispatcher().dispatch(
        "req-preview",
        "migration.sources.preview",
        {"candidateId": candidate_id},
        ctx,
    )

    assert previewed.ok is True
    payload = previewed.payload
    assert payload["previewStatus"] == "available"
    assert payload["targetAction"] == "replace"
    assert payload["blockers"] == []
    assert payload["notices"] == ["whole_profile_replacement"]
    assert payload["execution"] == {
        "canApply": False,
        "supportedBy": ["desktop", "host_cli"],
    }
    assert set(payload["summary"]["itemCounts"]) == {
        "planned",
        "skipped",
        "error",
    }
    rendered = json.dumps(payload)
    assert str(source) not in rendered
    assert str(target) not in rendered
    assert "private" not in rendered
    assert _all_keys(payload).isdisjoint(
        {
            "source",
            "target",
            "path",
            "command",
            "reason",
            "notes",
            "output_dir",
            "outputDir",
            "config_transforms",
            "secret_relocations",
        }
    )
    assert _files(source) == source_before
    assert _files(target) == target_before


@pytest.mark.asyncio
async def test_preview_reports_live_source_with_stable_blocker_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _make_home(tmp_path / "live-source", running=True)
    target = _make_home(tmp_path / "target", running=True)
    _patch_one_candidate(monkeypatch, source)
    ctx = _ctx(target)
    listed = await get_dispatcher().dispatch(
        "req-list",
        "migration.sources.list",
        {},
        ctx,
    )

    previewed = await get_dispatcher().dispatch(
        "req-preview",
        "migration.sources.preview",
        {"candidateId": listed.payload["candidates"][0]["candidateId"]},
        ctx,
    )

    assert previewed.ok is True
    assert previewed.payload["previewStatus"] == "blocked"
    assert previewed.payload["blockers"] == ["source_in_use"]
    assert "gateway.pid" not in json.dumps(previewed.payload)


@pytest.mark.asyncio
async def test_candidate_is_connection_bound_and_expiring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import rpc_migration

    source = _make_home(tmp_path / "source")
    target = _make_home(tmp_path / "target")
    _patch_one_candidate(monkeypatch, source)
    clock = [100.0]
    monkeypatch.setattr(rpc_migration.time, "monotonic", lambda: clock[0])
    listed = await get_dispatcher().dispatch(
        "req-list",
        "migration.sources.list",
        {},
        _ctx(target, conn_id="connection-a"),
    )
    candidate_id = listed.payload["candidates"][0]["candidateId"]

    cross_connection = await get_dispatcher().dispatch(
        "req-cross",
        "migration.sources.preview",
        {"candidateId": candidate_id},
        _ctx(target, conn_id="connection-b"),
    )
    assert cross_connection.ok is False
    assert cross_connection.error is not None
    assert cross_connection.error.code == "migration.candidate_unavailable"

    clock[0] = 701.0
    expired = await get_dispatcher().dispatch(
        "req-expired",
        "migration.sources.preview",
        {"candidateId": candidate_id},
        _ctx(target, conn_id="connection-a"),
    )
    assert expired.ok is False
    assert expired.error is not None
    assert expired.error.code == "migration.candidate_unavailable"


@pytest.mark.asyncio
async def test_candidate_rejects_replaced_source_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _make_home(tmp_path / "source")
    target = _make_home(tmp_path / "target")
    _patch_one_candidate(monkeypatch, source)
    ctx = _ctx(target)
    listed = await get_dispatcher().dispatch(
        "req-list",
        "migration.sources.list",
        {},
        ctx,
    )
    candidate_id = listed.payload["candidates"][0]["candidateId"]
    source.rename(tmp_path / "old-source")
    _make_home(source)

    response = await get_dispatcher().dispatch(
        "req-preview",
        "migration.sources.preview",
        {"candidateId": candidate_id},
        ctx,
    )

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "migration.candidate_unavailable"
    assert str(source) not in response.error.message


@pytest.mark.asyncio
async def test_candidate_rejects_changed_target_path_and_replaced_target_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _make_home(tmp_path / "source")
    target = _make_home(tmp_path / "target")
    other_target = _make_home(tmp_path / "other-target")
    _patch_one_candidate(monkeypatch, source)
    ctx = _ctx(target, conn_id="same-connection")
    listed = await get_dispatcher().dispatch(
        "req-list",
        "migration.sources.list",
        {},
        ctx,
    )
    candidate_id = listed.payload["candidates"][0]["candidateId"]

    changed_path = await get_dispatcher().dispatch(
        "req-other-target",
        "migration.sources.preview",
        {"candidateId": candidate_id},
        _ctx(other_target, conn_id="same-connection"),
    )
    assert changed_path.ok is False
    assert changed_path.error is not None
    assert changed_path.error.code == "migration.candidate_unavailable"

    target.rename(tmp_path / "old-target")
    _make_home(target)
    replaced_object = await get_dispatcher().dispatch(
        "req-replaced-target",
        "migration.sources.preview",
        {"candidateId": candidate_id},
        ctx,
    )
    assert replaced_object.ok is False
    assert replaced_object.error is not None
    assert replaced_object.error.code == "migration.candidate_unavailable"


@pytest.mark.asyncio
async def test_non_plain_target_returns_capability_unavailable_without_scanning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import rpc_migration

    target = tmp_path / "target-is-a-file"
    target.write_text("not a profile directory", encoding="utf-8")
    monkeypatch.setattr(
        rpc_migration,
        "_discover_sync",
        lambda target_arg: (_ for _ in ()).throw(AssertionError("unexpected scan")),
    )

    response = await get_dispatcher().dispatch(
        "req-list",
        "migration.sources.list",
        {},
        _ctx(target),
    )

    assert response.ok is True
    assert response.payload["capabilities"]["discover"] is False
    assert response.payload["candidates"] == []


@pytest.mark.asyncio
async def test_unknown_discovery_and_preview_errors_do_not_leak_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import rpc_migration

    source = _make_home(tmp_path / "private-source")
    target = _make_home(tmp_path / "private-target")
    ctx = _ctx(target)
    original_discover = rpc_migration._discover_sync
    monkeypatch.setattr(
        rpc_migration,
        "_discover_sync",
        lambda target_arg: (_ for _ in ()).throw(OSError(str(source))),
    )
    failed_list = await get_dispatcher().dispatch(
        "req-list-failed",
        "migration.sources.list",
        {},
        ctx,
    )
    assert failed_list.ok is False
    assert failed_list.error is not None
    assert failed_list.error.code == "migration.unavailable"
    assert str(source) not in failed_list.error.message
    assert str(target) not in failed_list.error.message

    _patch_one_candidate(monkeypatch, source)
    monkeypatch.setattr(rpc_migration, "_discover_sync", original_discover)
    listed = await get_dispatcher().dispatch(
        "req-list",
        "migration.sources.list",
        {},
        ctx,
    )
    candidate_id = listed.payload["candidates"][0]["candidateId"]
    monkeypatch.setattr(
        rpc_migration,
        "_preview_sync",
        lambda record, *, target: (_ for _ in ()).throw(OSError(str(target))),
    )
    failed_preview = await get_dispatcher().dispatch(
        "req-preview-failed",
        "migration.sources.preview",
        {"candidateId": candidate_id},
        ctx,
    )
    assert failed_preview.ok is False
    assert failed_preview.error is not None
    assert failed_preview.error.code == "migration.preview_unavailable"
    assert str(source) not in failed_preview.error.message
    assert str(target) not in failed_preview.error.message


def test_candidate_cache_is_globally_bounded_and_lru(
    tmp_path: Path,
) -> None:
    from opensquilla.gateway import rpc_migration

    target = _make_home(tmp_path / "target")
    ctx = _ctx(target)
    target_identity = rpc_migration._directory_identity(target)
    assert target_identity is not None
    discovered = []
    for index in range(128):
        source = tmp_path / f"source-{index:03d}"
        source.mkdir()
        source_identity = rpc_migration._directory_identity(source)
        assert source_identity is not None
        discovered.append(
            (
                source,
                "cli-home",
                source_identity,
                {
                    "sourceKind": "cli-home",
                    "version": None,
                    "estimatedActivityAt": None,
                    "sessionCount": None,
                    "sizeBytes": None,
                    "previouslyImported": False,
                },
            )
        )
    payloads = rpc_migration._store_candidates(
        ctx=ctx,
        target=target,
        target_identity=target_identity,
        discovered=discovered,
    )
    first_id = payloads[0]["candidateId"]
    second_id = payloads[1]["candidateId"]
    rpc_migration._resolve_candidate(first_id, ctx=ctx, target=target)

    extra_source = tmp_path / "source-extra"
    extra_source.mkdir()
    extra_identity = rpc_migration._directory_identity(extra_source)
    assert extra_identity is not None
    rpc_migration._store_candidates(
        ctx=ctx,
        target=target,
        target_identity=target_identity,
        discovered=[
            (
                extra_source,
                "cli-home",
                extra_identity,
                {
                    "sourceKind": "cli-home",
                    "version": None,
                    "estimatedActivityAt": None,
                    "sessionCount": None,
                    "sizeBytes": None,
                    "previouslyImported": False,
                },
            )
        ],
    )

    assert len(rpc_migration._candidate_cache) == 128
    assert first_id in rpc_migration._candidate_cache
    assert second_id not in rpc_migration._candidate_cache


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,params",
    [
        ("migration.sources.list", None),
        ("migration.sources.list", {"path": "/tmp/private"}),
        ("migration.sources.preview", {}),
        ("migration.sources.preview", {"candidateId": "x", "path": "/tmp/private"}),
    ],
)
async def test_migration_rpc_params_are_strict(
    tmp_path: Path,
    method: str,
    params: Any,
) -> None:
    target = _make_home(tmp_path / "target")

    response = await get_dispatcher().dispatch(
        "req-invalid",
        method,
        params,
        _ctx(target),
    )

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "migration.invalid_params"
